"""Events API adapter — Ring's documented polling + snapshot endpoints.

Client for the REST side of the Partner API (the webhook is the other
side). Contract pinned to the published documentation
(https://developer.amazon.com/docs/ring/api-documentation.html):

* Event History — ``GET /v1/history/devices/{device_id}/events``
  with an ``event_types`` comma filter (OR, dotted composite syntax)
  and ``page[key]`` cursor pagination; JSON:API response, newest first.
  **Documented finding: responses carry NO ``sub_type``** — subtype
  exists only as a query filter, so a polled event's object class must
  come from its snapshot, not from the history record.
* Image Snapshots — two-step and NOT JSON:API:
  ``POST /v1/devices/{device_id}/media/image/download`` (body selects
  ``at_timestamp`` or ``latest_in_range`` plus image options) answers
  ``303 See Other`` with a pre-signed ``Location``; the plain ``GET``
  of that URL returns the bytes with ``Content-Type``,
  ``X-Media-Timestamp`` (epoch ms) and ``X-Media-Origin``
  (``recording`` | ``snapshot``).
* Users — ``GET /v1/users/me`` (account id + profile attributes).
* Devices — ``GET /v1/devices`` (id + name attributes).

Auth is a Bearer access token (obtained via the documented OAuth token
exchange once registration lands; until then the token is an input,
never guessed). All HTTP goes through an injected transport — the same
pattern as ``llm.py`` — so tests and the offline mock run in-process
(no sockets; this dev sandbox blocks loopback connections anyway).

``adapt_history_event`` lands a polled event in the SAME internal flat
contract ``wire.py`` produces for webhooks, so the pipeline downstream
of the edge is shared by both ingress paths.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Mapping
from urllib.parse import parse_qs, quote, urlsplit

from .schema import EventKind, SubType
from .wire import UnsupportedEvent

DOCUMENTED_BASE_URL = "https://api.amazonvision.com"
USERS_ME_PATH = "/v1/users/me"
DEVICES_PATH = "/v1/devices"
HISTORY_PATH = "/v1/history/devices/{device_id}/events"
SNAPSHOT_DOWNLOAD_PATH = "/v1/devices/{device_id}/media/image/download"

# Documented error codes (Image Snapshots section) that mean "no media
# for this event" rather than "your request is wrong".
MEDIA_UNAVAILABLE_CODES = frozenset(
    {"MEDIA_NOT_FOUND", "RECORDING_NOT_READY", "CORRUPT_RECORDING"}
)


class EventsApiError(RuntimeError):
    """An Events API call failed (transport, HTTP error, or bad payload)."""

    def __init__(self, message: str, *, status: int | None = None, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


class EventsApiAuthError(EventsApiError):
    """HTTP 401 — the access token is missing, expired, or invalid."""


@dataclass(frozen=True)
class HttpResponse:
    """One HTTP response, header names lowercased (case-insensitive lookup)."""

    status: int
    headers: Mapping[str, str]
    body: bytes

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


HttpTransport = Callable[[str, str, bytes | None, Mapping[str, str], float], HttpResponse]
"""``transport(method, url, body, headers, timeout) -> HttpResponse``.

Injected for the same reason ``llm.py`` injects its transport: the core
must be testable in-process. The default is urllib with redirect
following DISABLED so the documented two-step snapshot download stays
explicit (303 must surface, not be silently chased).
"""


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _urllib_transport(
    method: str, url: str, body: bytes | None, headers: Mapping[str, str], timeout: float
) -> HttpResponse:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    opener = urllib.request.build_opener(_NoRedirects)
    try:
        with opener.open(request, timeout=timeout) as response:  # noqa: S310
            return HttpResponse(
                response.status,
                {k.lower(): v for k, v in response.headers.items()},
                response.read(),
            )
    except urllib.error.HTTPError as exc:
        # Non-2xx INCLUDING captured redirects (HTTPError is the response)
        return HttpResponse(
            exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()
        )


@dataclass(frozen=True)
class RingDevice:
    """One entry of ``GET /v1/devices``."""

    device_id: str
    name: str
    raw: dict


@dataclass(frozen=True)
class HistoryEvent:
    """One ``history-events`` resource; ``start``/``end`` are UTC datetimes."""

    event_id: str
    event_type: str  # documented values: motion | ding | on_demand
    device_id: str
    start: datetime
    end: datetime | None
    reviewed: bool
    raw: dict


@dataclass(frozen=True)
class HistoryPage:
    """One page of event history plus the next cursor (None = last page)."""

    events: list[HistoryEvent]
    next_cursor: str | None


@dataclass(frozen=True)
class ApiSnapshot:
    """Bytes from the two-step snapshot download, with their media headers."""

    data: bytes
    content_type: str  # image/png | image/jpeg
    media_timestamp: datetime | None  # X-Media-Timestamp (epoch ms) when present
    origin: str  # X-Media-Origin: recording | snapshot


def _error_from_response(response: HttpResponse, context: str) -> EventsApiError:
    """Build the exception from a JSON:API error body when one is present."""
    status, body = response.status, response.body
    code, detail = "", f"{context}: HTTP {status}"
    try:
        errors = json.loads(body.decode("utf-8")).get("errors")
        first = errors[0] if isinstance(errors, list) and errors else None
        if isinstance(first, dict):
            code = str(first.get("code", ""))
            detail = f"{context}: HTTP {status} {code}"
            if first.get("detail"):
                detail += f" — {first['detail']}"
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        pass  # non-JSON error body: keep the plain status line
    if status == 401:
        return EventsApiAuthError(detail, status=status, code=code or "UNAUTHORIZED")
    return EventsApiError(detail, status=status, code=code)


def _epoch_ms_to_utc(value: object, field: str) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EventsApiError(f"malformed history payload: '{field}' must be epoch ms")
    seconds, millis = divmod(int(value), 1000)
    return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(microsecond=millis * 1000)


def _history_event_from(item: object) -> HistoryEvent:
    if not isinstance(item, Mapping) or not isinstance(item.get("attributes"), Mapping):
        raise EventsApiError("malformed history payload: missing 'attributes'")
    event_id = item.get("id")
    event_type = item["attributes"].get("event_type")
    if not isinstance(event_id, str) or not event_id:
        raise EventsApiError("malformed history payload: 'id' must be a non-empty string")
    if not isinstance(event_type, str) or not event_type:
        raise EventsApiError("malformed history payload: 'event_type' must be a string")
    relationships = item.get("relationships")
    source = (
        relationships.get("source", {}) if isinstance(relationships, Mapping) else {}
    )
    source_data = source.get("data") if isinstance(source, Mapping) else None
    device_id = source_data.get("id") if isinstance(source_data, Mapping) else None
    if not isinstance(device_id, str) or not device_id:
        raise EventsApiError(
            "malformed history payload: missing relationships.source.data.id"
        )
    attributes = dict(item["attributes"])
    end_raw = attributes.get("end")
    return HistoryEvent(
        event_id=event_id,
        event_type=event_type,
        device_id=device_id,
        start=_epoch_ms_to_utc(attributes.get("start"), "attributes.start"),
        end=_epoch_ms_to_utc(end_raw, "attributes.end") if end_raw is not None else None,
        reviewed=bool(attributes.get("is_third_party_reviewed", False)),
        raw=dict(item),
    )


@dataclass
class RingApiClient:
    """Bearer-authed client for the documented Events API surface.

    Fail-closed like the rest of the pipeline: an empty access token is
    a construction error, never a silent anonymous request.
    """

    access_token: str
    base_url: str = DOCUMENTED_BASE_URL
    timeout_s: float = 10.0
    transport: HttpTransport = _urllib_transport

    def __post_init__(self) -> None:
        if not self.access_token:
            raise EventsApiAuthError(
                "RING_API_TOKEN is empty; refusing an unauthenticated Events API client"
            )

    # -- plumbing -----------------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        auth: bool = True,
    ) -> HttpResponse:
        full_url = url if url.startswith("http") else self.base_url.rstrip("/") + url
        headers: dict[str, str] = {"Accept": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self.access_token}"
        if content_type:
            headers["Content-Type"] = content_type
        try:
            response = self.transport(method, full_url, body, headers, self.timeout_s)
        except urllib.error.URLError as exc:
            raise EventsApiError(f"Events API transport failure: {exc}") from exc
        if response.status >= 400:
            raise _error_from_response(response, f"{method} {urlsplit(full_url).path}")
        return response

    @staticmethod
    def _json(response: HttpResponse) -> dict:
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EventsApiError(f"Events API returned non-JSON body: {exc}") from exc
        if not isinstance(payload, dict):
            raise EventsApiError("Events API returned a non-object JSON body")
        return payload

    # -- documented endpoints ----------------------------------------------

    def account_id(self) -> str:
        """``GET /v1/users/me`` → the ``ava1.ring.account.*`` id."""
        payload = self._json(self._request("GET", USERS_ME_PATH))
        data = payload.get("data")
        if not isinstance(data, Mapping) or not isinstance(data.get("id"), str):
            raise EventsApiError("malformed /v1/users/me payload: missing data.id")
        return data["id"]

    def devices(self) -> list[RingDevice]:
        """``GET /v1/devices`` → the account's devices."""
        payload = self._json(self._request("GET", DEVICES_PATH))
        items = payload.get("data")
        if not isinstance(items, list):
            raise EventsApiError("malformed /v1/devices payload: 'data' must be a list")
        devices = []
        for item in items:
            if not isinstance(item, Mapping):
                raise EventsApiError("malformed /v1/devices payload: non-object item")
            device_id = item.get("id")
            if not isinstance(device_id, str) or not device_id:
                raise EventsApiError("malformed /v1/devices payload: missing item.id")
            attributes = item.get("attributes")
            name = attributes.get("name") if isinstance(attributes, Mapping) else None
            devices.append(
                RingDevice(device_id=device_id, name=name or device_id, raw=dict(item))
            )
        return devices

    def history_events(
        self,
        device_id: str,
        *,
        event_types: Iterable[str] | None = None,
        page_key: str | None = None,
    ) -> HistoryPage:
        """One page of ``GET /v1/history/devices/{id}/events``.

        ``event_types`` uses the documented OR-comma filter (dotted
        composite syntax allowed: ``motion.human``); ``page_key`` is the
        cursor taken from a previous page's ``links.next``.
        """
        query = ""
        if event_types is not None:
            values = list(event_types)
            for value in values:
                if not value or not all(c.isalnum() or c in "._" for c in value):
                    raise EventsApiError(f"invalid event_types filter value: {value!r}")
            # commas stay literal (as documented); the values themselves
            # are already restricted to a safe charset above
            query += ("&" if query else "?") + "event_types=" + ",".join(values)
        if page_key is not None:
            query += ("&" if query else "?") + "page[key]=" + quote(page_key, safe="")

        response = self._request("GET", HISTORY_PATH.format(device_id=device_id) + query)
        payload = self._json(response)
        items = payload.get("data")
        if not isinstance(items, list):
            raise EventsApiError("malformed history payload: 'data' must be a list")
        links = payload.get("links")
        next_cursor = None
        if isinstance(links, Mapping) and links.get("next"):
            # links.next is a relative URL; the cursor is its page[key] value
            next_query = parse_qs(urlsplit(str(links["next"])).query)
            values = next_query.get("page[key]", [])
            next_cursor = values[0] if values else str(links["next"])
        return HistoryPage(
            events=[_history_event_from(item) for item in items], next_cursor=next_cursor
        )

    def iter_history_events(
        self,
        device_id: str,
        *,
        event_types: Iterable[str] | None = None,
        max_pages: int = 100,
    ) -> "Iterable[HistoryEvent]":
        """Follow ``links.next`` until it is absent (documented end-of-pages)."""
        page_key: str | None = None
        for _ in range(max_pages):  # loop guard; absent next ends iteration anyway
            page = self.history_events(device_id, event_types=event_types, page_key=page_key)
            yield from page.events
            if page.next_cursor is None:
                return
            page_key = page.next_cursor

    def snapshot(
        self,
        device_id: str,
        timestamp_ms: int,
        *,
        image_format: str = "png",
        component_id: str | None = None,
    ) -> ApiSnapshot:
        """Two-step documented snapshot download at ``timestamp_ms``.

        Step 1 ``POST .../media/image/download`` (``at_timestamp`` mode)
        → ``303`` + pre-signed ``Location``; step 2 plain ``GET`` of that
        URL (no Bearer — the URL is pre-signed) → bytes + media headers.
        """
        request_payload: dict = {
            "type": "at_timestamp",
            "timestamp": int(timestamp_ms),
            "image_options": {"format": image_format},
        }
        if component_id is not None:
            # documented: exactly one entry, multi-camera devices
            request_payload["components"] = [{"component_id": component_id}]
        body = json.dumps(request_payload).encode("utf-8")
        response = self._request(
            "POST",
            SNAPSHOT_DOWNLOAD_PATH.format(device_id=device_id),
            body=body,
            content_type="application/json",
        )
        if response.status != 303:
            raise EventsApiError(
                f"snapshot download expected 303 See Other, got HTTP {response.status}"
            )
        location = response.header("location")
        if not location:
            raise EventsApiError("snapshot download 303 carried no Location header")
        media = self._request("GET", location, auth=False)

        media_ts_header = media.header("x-media-timestamp")
        media_timestamp = None
        if media_ts_header:
            try:
                media_timestamp = _epoch_ms_to_utc(int(media_ts_header), "X-Media-Timestamp")
            except (TypeError, ValueError) as exc:
                raise EventsApiError(f"bad X-Media-Timestamp: {media_ts_header!r}") from exc
        return ApiSnapshot(
            data=media.body,
            content_type=media.header("content-type") or "application/octet-stream",
            media_timestamp=media_timestamp,
            origin=media.header("x-media-origin") or "",
        )


# documented response event_type -> (kind, sub_type). The dotted filter
# syntax is documented for the QUERY; responses show the plain three.
# Dotted values here are tolerated defensively: base maps the kind, the
# suffix maps when known, unknown suffixes degrade to None (noise).
_HISTORY_SUB_TYPES: dict[str, SubType] = {
    "human": SubType.HUMAN,
    "vehicle": SubType.VEHICLE,
}


def adapt_history_event(event: HistoryEvent) -> dict:
    """History event → the SAME internal flat payload ``wire.py`` produces.

    Raises ``UnsupportedEvent`` for ``on_demand`` (and anything else not
    intent-bearing), exactly like the webhook adapter — one semantic,
    two ingress paths. The original JSON:API item rides along under the
    ``history`` key, mirroring ``raw["wire"]``.
    """
    base, _, suffix = event.event_type.partition(".")
    sub_type: SubType | None = None
    if base == "ding":
        kind = EventKind.DING
    elif base == "motion":
        kind = EventKind.MOTION
        sub_type = _HISTORY_SUB_TYPES.get(suffix)
    else:
        raise UnsupportedEvent(event.event_type)
    return {
        "id": event.event_id,
        "type": kind.value,
        "sub_type": sub_type.value if sub_type is not None else None,
        "device_id": event.device_id,
        "occurred_at": event.start.isoformat(),
        "history": event.raw,
    }
