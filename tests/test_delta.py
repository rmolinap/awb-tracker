from pathlib import Path

import pytest

from app.models import ShipmentRequest
from app.trackers.delta import (
    DeltaTracker,
    build_access_denied_screenshot_path,
    detect_access_denied,
)


FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def test_delta_tracking_url_generation() -> None:
    tracker = DeltaTracker()

    assert tracker.build_tracking_url("006-22953556") == (
        "https://www.deltacargo.com/Cargo/trackShipment?awbNumber=00622953556"
    )


def test_delta_parse_tracking_text_extracts_structured_fields() -> None:
    tracker = DeltaTracker()
    visible_text = load_fixture("delta_tracking_page.txt")

    parsed = tracker.parse_tracking_text(visible_text)

    assert parsed == {
        "status": "Arrived",
        "eta": "2026-05-02 14:30",
        "origin": "SFO",
        "destination": "ATL",
        "last_update": "2026-05-01 08:15",
        "exception": False,
    }


def test_delta_parse_tracking_text_sets_exception_flag() -> None:
    tracker = DeltaTracker()
    visible_text = load_fixture("delta_exception_page.txt")

    parsed = tracker.parse_tracking_text(visible_text)

    assert parsed["status"] == "Shipment Delayed"
    assert parsed["eta"] == "2026-05-03 09:45"
    assert parsed["origin"] == "SEA"
    assert parsed["destination"] == "JFK"
    assert parsed["last_update"] == "2026-05-01 06:10"
    assert parsed["exception"] is True


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Access Denied", True),
        ("Reference #18.abc123", True),
        ("https://errors.edgesuite.net/18.abc123", True),
        ("Status\nArrived", False),
    ],
)
def test_detect_access_denied(text: str, expected: bool) -> None:
    assert detect_access_denied(text) is expected


def test_build_access_denied_screenshot_path() -> None:
    screenshot_path = build_access_denied_screenshot_path(
        screenshot_dir="/tmp/screenshots",
        awb="006-22953556",
        attempt_number=2,
    )

    assert screenshot_path == Path(
        "/tmp/screenshots/delta_00622953556_access_denied_attempt_2.png"
    )


@pytest.mark.asyncio
async def test_delta_tracker_returns_structured_result_from_sample_text(
    monkeypatch,
) -> None:
    tracker = DeltaTracker()
    visible_text = load_fixture("delta_tracking_page.txt")

    async def fake_fetch(tracking_url: str, original_awb: str):
        return {
            "tracking_url": tracking_url,
            "normalized_awb": "00622953556",
            "visible_text": visible_text,
            "screenshot_path": None,
        }

    monkeypatch.setattr(tracker, "_fetch_tracking_page", fake_fetch)

    result = await tracker.track(
        ShipmentRequest(
            carrier="Delta",
            awb="006-22953556",
            customer="Inland",
            po_number=None,
            notify_email="employee@company.com",
            arrival_location="ATL",
        )
    )

    assert result.carrier == "Delta"
    assert result.awb == "006-22953556"
    assert result.status == "Arrived"
    assert result.eta == "2026-05-02 14:30"
    assert result.origin == "SFO"
    assert result.destination == "ATL"
    assert result.last_update == "2026-05-01 08:15"
    assert result.exception is False
    assert result.tracking_url == (
        "https://www.deltacargo.com/Cargo/trackShipment?awbNumber=00622953556"
    )
    assert result.raw_summary["parsed_fields"]["status"] == "Arrived"
    assert result.error is None


@pytest.mark.asyncio
async def test_delta_tracker_keeps_request_alive_when_fetch_is_incomplete(
    monkeypatch,
) -> None:
    tracker = DeltaTracker()

    async def fake_fetch(tracking_url: str, original_awb: str):
        return {
            "tracking_url": tracking_url,
            "normalized_awb": "00622953556",
            "visible_text": "",
            "warning": "mock parse incomplete",
        }

    monkeypatch.setattr(tracker, "_fetch_tracking_page", fake_fetch)

    result = await tracker.track(
        ShipmentRequest(
            carrier="Delta",
            awb="006-22953556",
            customer="Inland",
            po_number=None,
            notify_email="employee@company.com",
            arrival_location="ATL",
        )
    )

    assert result.status is None
    assert result.eta is None
    assert result.origin is None
    assert result.destination == "ATL"
    assert result.last_update is None
    assert result.exception is False
    assert result.raw_summary["warning"] == "mock parse incomplete"
    assert result.error is None


@pytest.mark.asyncio
async def test_delta_fetch_retries_after_access_denied_then_returns_success(
    monkeypatch,
) -> None:
    tracker = DeltaTracker()
    attempts: list[int] = []
    sleep_calls: list[str] = []

    async def fake_fetch_attempt(
        context,
        tracking_url: str,
        original_awb: str,
        normalized_awb: str,
        attempt_number: int,
    ):
        attempts.append(attempt_number)
        if attempt_number == 1:
            return {
                "tracking_url": tracking_url,
                "normalized_awb": normalized_awb,
                "visible_text": "Access Denied\nReference #18.abc123",
                "page_title": "Access Denied",
                "final_url": "https://errors.edgesuite.net/18.abc123",
                "screenshot_path": "/tmp/denied.png",
            }

        return {
            "tracking_url": tracking_url,
            "normalized_awb": normalized_awb,
            "visible_text": "Status\nArrived",
            "page_title": "Track Shipment",
            "final_url": tracking_url,
            "screenshot_path": None,
        }

    async def fake_sleep_before_retry() -> None:
        sleep_calls.append("slept")

    monkeypatch.setattr(tracker, "_fetch_tracking_attempt", fake_fetch_attempt)
    monkeypatch.setattr(tracker, "_sleep_before_retry", fake_sleep_before_retry)

    summary = await tracker._fetch_with_retries(
        context=object(),
        tracking_url="https://www.deltacargo.com/Cargo/trackShipment?awbNumber=00622953556",
        original_awb="006-22953556",
        normalized_awb="00622953556",
    )

    assert attempts == [1, 2]
    assert sleep_calls == ["slept"]
    assert summary["page_title"] == "Track Shipment"
    assert summary["final_url"] == (
        "https://www.deltacargo.com/Cargo/trackShipment?awbNumber=00622953556"
    )
    assert summary["retry_count"] == 1
    assert summary["access_denied_detected"] is True
    assert summary["screenshot_path"] == "/tmp/denied.png"


@pytest.mark.asyncio
async def test_delta_fetch_stops_after_max_access_denied_retries(monkeypatch) -> None:
    tracker = DeltaTracker()
    attempts: list[int] = []
    sleep_calls: list[str] = []

    async def fake_fetch_attempt(
        context,
        tracking_url: str,
        original_awb: str,
        normalized_awb: str,
        attempt_number: int,
    ):
        attempts.append(attempt_number)
        return {
            "tracking_url": tracking_url,
            "normalized_awb": normalized_awb,
            "visible_text": "Access Denied\nReference #18.abc123",
            "page_title": "Access Denied",
            "final_url": "https://errors.edgesuite.net/18.abc123",
            "screenshot_path": None,
        }

    async def fake_sleep_before_retry() -> None:
        sleep_calls.append("slept")

    monkeypatch.setattr(tracker, "_fetch_tracking_attempt", fake_fetch_attempt)
    monkeypatch.setattr(tracker, "_sleep_before_retry", fake_sleep_before_retry)

    summary = await tracker._fetch_with_retries(
        context=object(),
        tracking_url="https://www.deltacargo.com/Cargo/trackShipment?awbNumber=00622953556",
        original_awb="006-22953556",
        normalized_awb="00622953556",
    )

    assert attempts == [1, 2, 3]
    assert sleep_calls == ["slept", "slept"]
    assert summary["retry_count"] == 2
    assert summary["access_denied_detected"] is True
    assert summary["warning"] == "Delta Cargo access denied by Akamai after retries."
