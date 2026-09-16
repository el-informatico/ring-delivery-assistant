"""Events API client against the documented-contract mock (in-process).

No sockets: ``MockRingApi`` is an injected transport that answers with
the documented response shapes, so these tests pin the client's request
construction and response parsing without a network.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from ring_api_mock import MOCK_TOKEN, MockRingApi

from ring_assistant.events_api import (
    EventsApiAuthError,
    EventsApiError,
    HttpResponse,
    RingApiClient,
    adapt_history_event,
)
from ring_assistant.schema import parse_event_payload
from ring_assistant.wire import UnsupportedEvent

DEV = "ava1.ring.device.door001"
BASE_MS = 1_789_000_000_000  # arbitrary fixed epoch ms, 2026


def make_client(mock: MockRingApi) -> RingApiClient:
    return RingApiClient(access_token=MOCK_TOKEN, transport=mock)


@pytest.fixture
def mock() -> MockRingApi:
    return MockRingApi()


# -- construction -----------------------------------------------------------


def test_empty_token_is_a_construction_error():
    with pytest.raises(EventsApiAuthError, match="RING_API_TOKEN"):
        RingApiClient(access_token="")


def test_transport_failure_becomes_events_api_error():
    def broken(method, url, body, headers, timeout):
        import urllib.error

        raise urllib.error.URLError("dns is a social construct")

    client = RingApiClient(access_token="t", transport=broken)
    with pytest.raises(EventsApiError, match="transport failure"):
        client.account_id()


# -- users / devices --------------------------------------------------------


def test_account_id_parses_users_me(mock):
    account = make_client(mock).account_id()
    assert account == "ava1.ring.account.XXXYYY"


def test_devices_parse_id_and_name(mock):
    mock.device_list.append(
        {"type": "devices", "id": "ava1.ring.device.door001", "attributes": {"name": "Front Door"}}
    )
    devices = make_client(mock).devices()
    by_id = {d.device_id: d for d in devices}
    assert by_id["ava1.ring.device.XXXYYY"].name == "Front Door Camera"
    # name falls back to the id when attributes carry none
    mock.device_list.append({"type": "devices", "id": "bare", "attributes": {}})
    assert {d.device_id: d for d in make_client(mock).devices()}["bare"].name == "bare"


def test_bad_bearer_token_gets_401_auth_error(mock):
    client = RingApiClient(access_token="wrong-token", transport=mock)
    with pytest.raises(EventsApiAuthError):
        client.devices()


# -- event history ----------------------------------------------------------


def test_history_parses_items_newest_first_with_utc_times(mock):
    for i in range(4):
        mock.add_history(DEV, "motion" if i % 2 else "ding", BASE_MS + i * 60_000)
    page = make_client(mock).history_events(DEV)
    starts = [e.start for e in page.events]
    assert starts == sorted(starts, reverse=True)  # documented: newest first
    assert page.events[0].start.tzinfo is timezone.utc
    assert page.next_cursor is None  # no links.next -> last page
    assert page.events[0].reviewed is False


def test_history_request_shape_commas_and_cursor(mock):
    mock.add_history(DEV, "motion", BASE_MS, sub_type="human")
    client = make_client(mock)
    page = client.history_events(DEV, event_types=["ding", "motion.human"])
    assert mock.requests[0][1].endswith(
        f"/v1/history/devices/{DEV}/events?event_types=ding,motion.human"
    )  # documented OR-filter, literal commas
    assert page.next_cursor is None
    assert [e.event_type for e in page.events] == ["motion"]  # server-side filter won


def test_history_filter_hides_other_subtypes(mock):
    mock.add_history(DEV, "motion", BASE_MS, sub_type="human")
    mock.add_history(DEV, "motion", BASE_MS + 1_000, sub_type="vehicle")
    mock.add_history(DEV, "ding", BASE_MS + 2_000)
    page = make_client(mock).history_events(DEV, event_types=["motion.vehicle"])
    assert [e.event_id for e in page.events] == ["hist-002"]
    # the documented hard truth: the response never says WHY it matched
    assert all("_sub_type" not in e.raw for e in page.events)


def test_history_pagination_follows_links_next(mock):
    mock.page_size = 2
    for i in range(5):
        mock.add_history(DEV, "motion", BASE_MS + i * 60_000)
    client = make_client(mock)
    seen = [e.event_id for e in client.iter_history_events(DEV)]
    assert seen == ["hist-005", "hist-004", "hist-003", "hist-002", "hist-001"]
    # cursors flowed through page[key]
    paginated = [r for r in mock.requests if "page%5Bkey%5D=" in r[1] or "page[key]=" in r[1]]
    assert paginated, "client must send the cursor back as page[key]"


def test_history_malformed_item_raises():
    # the mock models a WELL-FORMED server; malformed bodies get a stub transport
    def transport(method, url, body, headers, timeout):
        bad = b'{"data": [{"type": "history-events", "id": "broken", "attributes": {"event_type": "ding"}}]}'
        return HttpResponse(200, {"content-type": "application/json"}, bad)

    client = RingApiClient(access_token="t", transport=transport)
    with pytest.raises(EventsApiError, match="source"):
        client.history_events(DEV)


def test_history_empty_for_unknown_device_is_an_empty_last_page(mock):
    page = make_client(mock).history_events("ava1.ring.device.nope")
    assert page.events == []
    assert page.next_cursor is None


# -- adapting history events to the shared internal contract ---------------


def history_event(event_type: str, *, event_id="h1", start_ms=BASE_MS, raw=None):
    from ring_assistant.events_api import HistoryEvent

    return HistoryEvent(
        event_id=event_id,
        event_type=event_type,
        device_id=DEV,
        start=datetime.fromtimestamp(start_ms // 1000, tz=timezone.utc),
        end=None,
        reviewed=False,
        raw=raw or {},
    )


@pytest.mark.parametrize(
    ("event_type", "kind", "sub"),
    [
        ("ding", "ding", None),
        ("motion", "motion_detected", None),
        ("motion.human", "motion_detected", "human"),  # defensive dotted tolerance
        ("motion.vehicle", "motion_detected", "vehicle"),
        ("motion.other_motion", "motion_detected", None),  # unknown suffix -> noise
    ],
)
def test_adapt_history_maps_documented_types(event_type, kind, sub):
    payload = adapt_history_event(history_event(event_type))
    assert payload["type"] == kind
    assert payload["sub_type"] == sub
    event = parse_event_payload(payload)
    assert event.device_id == DEV
    assert event.raw["history"] == {} or isinstance(event.raw["history"], dict)


def test_adapt_history_rejects_on_demand(mock):
    mock.add_history(DEV, "on_demand", BASE_MS)
    event = make_client(mock).history_events(DEV).events[0]
    with pytest.raises(UnsupportedEvent) as excinfo:
        adapt_history_event(event)
    assert excinfo.value.event_type == "on_demand"


def test_adapt_history_round_trip_from_mock(mock):
    mock.add_history(DEV, "ding", BASE_MS, end_ms=BASE_MS + 20_000, reviewed=True)
    event = make_client(mock).history_events(DEV).events[0]
    internal = parse_event_payload(adapt_history_event(event))
    assert internal.kind.value == "ding"
    assert internal.sub_type is None
    assert internal.occurred_at == event.start
    assert internal.raw["history"]["attributes"]["is_third_party_reviewed"] is True


# -- snapshots (documented two-step download) -------------------------------


def test_snapshot_two_step_returns_media_with_headers(mock):
    mock.add_snapshot(DEV, BASE_MS, "package-mat")
    snap = make_client(mock).snapshot(DEV, BASE_MS + 1_000)
    assert snap.data[:8] == b"\x89PNG\r\n\x1a\n"
    assert snap.content_type == "image/png"
    assert snap.origin == "recording"
    assert snap.media_timestamp == datetime.fromtimestamp(
        BASE_MS // 1000, tz=timezone.utc
    )
    # step 1 was an at_timestamp POST with image options; step 2 a plain GET
    post = next(r for r in mock.requests if r[0] == "POST")
    body = json.loads(post[2])
    assert body["type"] == "at_timestamp"
    assert body["timestamp"] == BASE_MS + 1_000
    assert body["image_options"]["format"] == "png"


def test_snapshot_component_id_goes_in_the_request_body(mock):
    mock.add_snapshot(DEV, BASE_MS, "person-door")
    make_client(mock).snapshot(DEV, BASE_MS, component_id="1")
    post = next(r for r in mock.requests if r[0] == "POST")
    assert json.loads(post[2])["components"] == [{"component_id": "1"}]


def test_snapshot_no_media_is_documented_416(mock):
    mock.device_list.append({"type": "devices", "id": DEV, "attributes": {"name": "Front Door"}})
    with pytest.raises(EventsApiError) as excinfo:
        make_client(mock).snapshot(DEV, BASE_MS)
    assert excinfo.value.status == 416
    assert excinfo.value.code == "MEDIA_NOT_FOUND"


def test_snapshot_unknown_device_is_documented_404(mock):
    with pytest.raises(EventsApiError) as excinfo:
        make_client(mock).snapshot("ava1.ring.device.nope", BASE_MS)
    assert excinfo.value.status == 404
    assert excinfo.value.code == "DEVICE_NOT_FOUND"


def test_snapshot_not_ready_surfaces_documented_code(mock):
    mock.media_error = (425, "RECORDING_NOT_READY", "try again later")
    with pytest.raises(EventsApiError) as excinfo:
        make_client(mock).snapshot(DEV, BASE_MS)
    assert excinfo.value.code == "RECORDING_NOT_READY"


def test_snapshot_error_body_is_parsed_into_message(mock):
    mock.media_error = (422, "CORRUPT_RECORDING", "the recording is unreadable")
    with pytest.raises(EventsApiError, match="unreadable"):
        make_client(mock).snapshot(DEV, BASE_MS)


def test_snapshot_non_303_success_is_an_error(mock):
    state = {"n": 0}

    def transport(method, url, body, headers, timeout):
        state["n"] += 1
        if state["n"] == 1:
            return HttpResponse(200, {"content-type": "application/json"}, b"{}")
        raise AssertionError("second call should not happen")

    client = RingApiClient(access_token="t", transport=transport)
    with pytest.raises(EventsApiError, match="expected 303"):
        client.snapshot(DEV, BASE_MS)
