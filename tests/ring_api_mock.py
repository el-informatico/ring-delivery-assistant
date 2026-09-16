"""In-process mock of Ring's documented Events API — a transport, not a server.

Plugs straight into ``RingApiClient(transport=...)`` (same injected-
transport pattern as the LLM tests): no sockets, no threads, works in
this dev sandbox where loopback connections are blocked anyway.

Every response reproduces the PUBLISHED shape from
https://developer.amazon.com/docs/ring/api-documentation.html:

* ``GET /v1/users/me``            → Users section example
* ``GET /v1/devices``             → Devices section example
* ``GET /v1/history/devices/{id}/events``
                                  → Event History: JSON:API
                                    ``history-events`` items, newest
                                    first, ``links.next`` cursor; the
                                    ``event_types`` comma filter is
                                    applied SERVER-side against a
                                    subtype the response never echoes
                                    (the documented state of affairs —
                                    modeled with a private ``_sub_type``
                                    key that is stripped on emit)
* ``POST /v1/devices/{id}/media/image/download``
                                  → Image Snapshots: ``303`` +
                                    pre-signed ``Location``
* ``GET <pre-signed URL>``        → media bytes + ``Content-Type`` +
                                    ``X-Media-Timestamp`` +
                                    ``X-Media-Origin``
* errors                          → JSON:API ``{"errors": [...]}``
                                    with the documented codes

Configure state via ``add_history`` / ``add_snapshot``; inspect
``requests`` (method, path+query, body) for request-shape assertions.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

from ring_assistant.events_api import HttpResponse
from ring_assistant.imaging import render_scene_bytes

MOCK_BASE_URL = "https://api.mock.ring"
MOCK_TOKEN = "mock-access-token"
DEFAULT_PAGE_SIZE = 100  # effectively "one page" unless tests shrink it
SNAPSHOT_MATCH_WINDOW_MS = 60_000  # nearest registration wins within this


def _iso_z(timestamp_ms: int) -> str:
    seconds, millis = divmod(int(timestamp_ms), 1000)
    from datetime import datetime, timezone

    moment = datetime.fromtimestamp(seconds, tz=timezone.utc).replace(
        microsecond=millis * 1000
    )
    return moment.isoformat().replace("+00:00", "Z")


class MockRingApi:
    """Callable transport implementing the documented API surface offline."""

    def __init__(self, *, access_token: str = MOCK_TOKEN, base_url: str = MOCK_BASE_URL):
        self.token = access_token
        self.base_url = base_url.rstrip("/")
        self.requests: list[tuple[str, str, bytes | None]] = []
        self.page_size = DEFAULT_PAGE_SIZE
        # documented-example defaults (Devices / Users sections)
        self.account_id_value = "ava1.ring.account.XXXYYY"
        self.profile = {"first_name": "John", "last_name": "Doe", "email": "johndoe@example.com"}
        self.device_list = [
            {"type": "devices", "id": "ava1.ring.device.XXXYYY", "attributes": {"name": "Front Door Camera"}}
        ]
        # history items hold a private _sub_type the filter uses and the
        # response NEVER emits (documented: subtype is a query filter only)
        self._history: list[dict] = []
        # (device_id, timestamp_ms) -> {"data", "content_type"}
        self._snapshots: dict[tuple[str, int], dict] = {}
        # request_id -> (data, content_type, timestamp_ms)
        self._presigned: dict[str, tuple[bytes, str, int]] = {}
        # (status, code, detail) forced onto the NEXT snapshot POST
        self.media_error: tuple[int, str, str] | None = None
        self._error_seq = 0
        self._request_seq = 0

    # -- configuration -------------------------------------------------------

    def add_history(
        self,
        device_id: str,
        event_type: str,
        start_ms: int,
        *,
        sub_type: str | None = None,
        end_ms: int | None = None,
        reviewed: bool = False,
        event_id: str | None = None,
    ) -> str:
        item = {
            "type": "history-events",
            "id": event_id or f"hist-{len(self._history) + 1:03d}",
            "attributes": {
                "event_type": event_type,
                "is_third_party_reviewed": reviewed,
                "start": start_ms,
                "end": end_ms if end_ms is not None else start_ms + 15_000,
            },
            "relationships": {"source": {"data": {"type": "devices", "id": device_id}}},
        }
        if sub_type is not None:
            item["_sub_type"] = sub_type
        self._history.append(item)
        return item["id"]

    def add_snapshot(self, device_id: str, timestamp_ms: int, scene: str) -> None:
        """Register a rendered fixture scene as the media at that moment."""
        self._snapshots[(device_id, int(timestamp_ms))] = {
            "data": render_scene_bytes(scene),
            "content_type": "image/png",
        }

    def add_snapshot_bytes(
        self, device_id: str, timestamp_ms: int, data: bytes, content_type: str
    ) -> None:
        self._snapshots[(device_id, int(timestamp_ms))] = {
            "data": data,
            "content_type": content_type,
        }

    # -- transport -----------------------------------------------------------

    def __call__(self, method: str, url: str, body, headers, timeout) -> HttpResponse:
        split = urlsplit(url)
        path = split.path
        query = parse_qs(split.query)
        self.requests.append((method.upper(), path + (f"?{split.query}" if split.query else ""), body))

        if path.startswith("/v1/media/download"):
            return self._serve_presigned(query)

        auth = dict(headers or {}).get("Authorization", "")
        if auth != f"Bearer {self.token}":
            return self._error_response(401, "UNAUTHORIZED", "missing or invalid bearer token")

        if method.upper() == "GET" and path == "/v1/users/me":
            return self._json_response(
                200,
                {"data": {"type": "users", "id": self.account_id_value, "attributes": dict(self.profile)}},
            )
        if method.upper() == "GET" and path == "/v1/devices":
            return self._json_response(
                200, {"meta": {"time": _iso_z(1_770_000_000_000)}, "data": [dict(d) for d in self.device_list]}
            )
        if method.upper() == "GET" and path.startswith("/v1/history/devices/"):
            device_id = path[len("/v1/history/devices/") :].removesuffix("/events")
            return self._history_response(device_id, query)
        if method.upper() == "POST" and path.startswith("/v1/devices/") and path.endswith(
            "/media/image/download"
        ):
            device_id = path.split("/")[3]
            return self._snapshot_redirect(device_id, body)
        return self._error_response(404, "NOT_FOUND", f"no mock route for {method} {path}")

    # -- route handlers ------------------------------------------------------

    def _history_response(self, device_id: str, query: dict) -> HttpResponse:
        filters = query.get("event_types", [""])[0]
        wanted = [f for f in filters.split(",") if f] if filters else []
        matching = [
            item
            for item in self._history
            if item.get("relationships", {}).get("source", {}).get("data", {}).get("id")
            == device_id
            and self._matches(item, wanted)
        ]
        matching.sort(key=lambda item: item.get("attributes", {}).get("start", 0), reverse=True)  # newest first

        size = max(1, self.page_size)
        pages = [matching[i : i + size] for i in range(0, len(matching), size)] or [[]]
        page_key = query.get("page[key]", [None])[0]
        index = 0
        if page_key is not None:
            markers = [_iso_z(page[0]["attributes"]["start"]) for page in pages[1:]]
            index = markers.index(page_key) + 1 if page_key in markers else 0

        page = pages[index]
        links: dict = {}
        if index + 1 < len(pages):
            next_key = _iso_z(pages[index + 1][0]["attributes"]["start"])
            suffix = f"event_types={filters}&" if filters else ""
            links["next"] = f"/v1/history/devices/{device_id}/events?{suffix}page[key]={next_key}"
        return self._json_response(
            200, {"data": [self._emit(item) for item in page], **({"links": links} if links else {})}
        )

    @staticmethod
    def _matches(item: dict, wanted: list[str]) -> bool:
        if not wanted:
            return True
        base, _, suffix = item["attributes"]["event_type"].partition(".")
        for value in wanted:
            v_base, _, v_suffix = value.partition(".")
            if v_base != base:
                continue
            if not v_suffix:  # plain type matches the whole class
                return True
            if item.get("_sub_type") == v_suffix:  # dotted filter matches the class+subtype
                return True
        return False

    @staticmethod
    def _emit(item: dict) -> dict:
        # strip every private key: the documented response never carries a subtype
        return {k: v for k, v in item.items() if not k.startswith("_")}

    def _snapshot_redirect(self, device_id: str, body) -> HttpResponse:
        try:
            request = json.loads((body or b"").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._error_response(400, "INVALID_PAYLOAD", "body is not JSON")
        if request.get("type") != "at_timestamp" or "timestamp" not in request:
            return self._error_response(
                400, "INVALID_PAYLOAD", "mock only implements type=at_timestamp with a timestamp"
            )
        if self.media_error is not None:
            status, code, detail = self.media_error
            return self._error_response(status, code, detail)

        wanted_ms = int(request["timestamp"])
        candidates = [ms for (dev, ms) in self._snapshots if dev == device_id]
        if not candidates:
            known = any(d.get("id") == device_id for d in self.device_list)
            if not known:
                return self._error_response(
                    404, "DEVICE_NOT_FOUND", f"device {device_id} is unknown to this account"
                )
            return self._error_response(
                416, "MEDIA_NOT_FOUND", f"no media registered for device {device_id}"
            )
        nearest = min(candidates, key=lambda ms: abs(ms - wanted_ms))
        if abs(nearest - wanted_ms) > SNAPSHOT_MATCH_WINDOW_MS:
            return self._error_response(
                416, "MEDIA_NOT_FOUND", f"nearest media is {abs(nearest - wanted_ms)}ms away"
            )
        registered = self._snapshots[(device_id, nearest)]
        self._request_seq += 1
        request_id = f"mock-media-{self._request_seq:04d}"
        self._presigned[request_id] = (registered["data"], registered["content_type"], nearest)
        location = f"{self.base_url}/v1/media/download?request_id={request_id}"
        return HttpResponse(
            303, {"location": location, "x-request-id": request_id, "content-length": "0"}, b""
        )

    def _serve_presigned(self, query: dict) -> HttpResponse:
        request_id = query.get("request_id", [""])[0]
        entry = self._presigned.get(request_id)
        if entry is None:
            return self._error_response(404, "NOT_FOUND", "unknown or expired pre-signed URL")
        data, content_type, timestamp_ms = entry
        return HttpResponse(
            200,
            {
                "content-type": content_type,
                "x-media-timestamp": str(timestamp_ms),
                "x-media-origin": "recording",
                "content-length": str(len(data)),
            },
            data,
        )

    # -- response helpers ----------------------------------------------------

    def _json_response(self, status: int, payload: dict) -> HttpResponse:
        return HttpResponse(
            status, {"content-type": "application/json"}, json.dumps(payload).encode("utf-8")
        )

    def _error_response(self, status: int, code: str, detail: str) -> HttpResponse:
        self._error_seq += 1
        payload = {
            "errors": [
                {
                    "id": f"mock-err-{self._error_seq:04d}",
                    "status": str(status),
                    "code": code,
                    "detail": detail,
                    "source": {"pointer": ""},
                    "meta": {},
                }
            ]
        }
        return self._json_response(status, payload)
