from pathlib import Path

import pytest

from app.config import settings
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


@pytest.mark.asyncio
async def test_delta_does_not_call_oxylabs_when_disabled(monkeypatch) -> None:
    tracker = DeltaTracker()
    called = False

    async def fake_oxylabs_fallback(
        tracking_url: str,
        normalized_awb: str,
        current_summary: dict[str, object],
    ) -> dict[str, object]:
        nonlocal called
        called = True
        return current_summary

    monkeypatch.setattr(settings, "oxylabs_enabled", False)
    monkeypatch.setattr(tracker, "_fetch_tracking_page_with_oxylabs", fake_oxylabs_fallback)

    summary = await tracker._maybe_apply_oxylabs_fallback(
        tracking_url="https://www.deltacargo.com/Cargo/trackShipment?awbNumber=00622953556",
        normalized_awb="00622953556",
        summary={
            "tracking_url": "https://www.deltacargo.com/Cargo/trackShipment?awbNumber=00622953556",
            "normalized_awb": "00622953556",
            "visible_text": "Access Denied\nReference #18.abc123",
            "access_denied_detected": True,
            "fetch_failed": False,
            "warning": "Delta Cargo access denied by Akamai after retries.",
        },
    )

    assert called is False
    assert summary["oxylabs_used"] is False
    assert summary["oxylabs_status_code"] is None
    assert summary["oxylabs_error"] is None


@pytest.mark.asyncio
async def test_delta_calls_oxylabs_when_enabled_and_access_denied_occurs(
    monkeypatch,
) -> None:
    tracker = DeltaTracker()
    called = False

    async def fake_oxylabs_fallback(
        tracking_url: str,
        normalized_awb: str,
        current_summary: dict[str, object],
    ) -> dict[str, object]:
        nonlocal called
        called = True
        return {
            **current_summary,
            "oxylabs_used": True,
            "oxylabs_status_code": 200,
            "oxylabs_error": None,
        }

    monkeypatch.setattr(settings, "oxylabs_enabled", True)
    monkeypatch.setattr(tracker, "_fetch_tracking_page_with_oxylabs", fake_oxylabs_fallback)

    summary = await tracker._maybe_apply_oxylabs_fallback(
        tracking_url="https://www.deltacargo.com/Cargo/trackShipment?awbNumber=00622953556",
        normalized_awb="00622953556",
        summary={
            "tracking_url": "https://www.deltacargo.com/Cargo/trackShipment?awbNumber=00622953556",
            "normalized_awb": "00622953556",
            "visible_text": "Access Denied\nReference #18.abc123",
            "access_denied_detected": True,
            "fetch_failed": False,
            "warning": "Delta Cargo access denied by Akamai after retries.",
        },
    )

    assert called is True
    assert summary["oxylabs_used"] is True
    assert summary["oxylabs_status_code"] == 200


@pytest.mark.asyncio
async def test_delta_oxylabs_failure_keeps_original_akamai_error(monkeypatch) -> None:
    tracker = DeltaTracker()
    monkeypatch.setattr(
        "app.trackers.delta.fetch_with_oxylabs",
        lambda url: {
            "ok": False,
            "status_code": 502,
            "content": "",
            "error": "Oxylabs upstream failed.",
        },
    )

    summary = await tracker._fetch_tracking_page_with_oxylabs(
        tracking_url="https://www.deltacargo.com/Cargo/trackShipment?awbNumber=00622953556",
        normalized_awb="00622953556",
        current_summary={
            "tracking_url": "https://www.deltacargo.com/Cargo/trackShipment?awbNumber=00622953556",
            "normalized_awb": "00622953556",
            "visible_text": "Access Denied\nReference #18.abc123",
            "page_title": "Access Denied",
            "final_url": "https://errors.edgesuite.net/18.abc123",
            "access_denied_detected": True,
            "fetch_failed": False,
            "warning": "Delta Cargo access denied by Akamai after retries.",
            "oxylabs_used": False,
            "oxylabs_status_code": None,
            "oxylabs_error": None,
        },
    )

    assert summary["warning"] == "Delta Cargo access denied by Akamai after retries."
    assert summary["oxylabs_used"] is True
    assert summary["oxylabs_status_code"] == 502
    assert summary["oxylabs_error"] == "Oxylabs upstream failed."


@pytest.mark.asyncio
async def test_delta_oxylabs_success_passes_text_into_parser(monkeypatch) -> None:
    tracker = DeltaTracker()
    monkeypatch.setattr(
        "app.trackers.delta.fetch_with_oxylabs",
        lambda url: {
            "ok": True,
            "status_code": 200,
            "content": """
                <html>
                  <body>
                    <div>Status</div>
                    <div>Arrived</div>
                    <div>ETA</div>
                    <div>2026-05-02 14:30</div>
                    <div>Origin</div>
                    <div>SFO</div>
                    <div>Destination</div>
                    <div>ATL</div>
                    <div>Last Update</div>
                    <div>2026-05-01 08:15</div>
                  </body>
                </html>
            """,
            "error": None,
            "result_url": "https://www.deltacargo.com/Cargo/trackShipment?awbNumber=00622953556",
        },
    )

    summary = await tracker._fetch_tracking_page_with_oxylabs(
        tracking_url="https://www.deltacargo.com/Cargo/trackShipment?awbNumber=00622953556",
        normalized_awb="00622953556",
        current_summary={
            "tracking_url": "https://www.deltacargo.com/Cargo/trackShipment?awbNumber=00622953556",
            "normalized_awb": "00622953556",
            "visible_text": "Access Denied\nReference #18.abc123",
            "page_title": "Access Denied",
            "final_url": "https://errors.edgesuite.net/18.abc123",
            "access_denied_detected": True,
            "fetch_failed": False,
            "warning": "Delta Cargo access denied by Akamai after retries.",
            "oxylabs_used": False,
            "oxylabs_status_code": None,
            "oxylabs_error": None,
        },
    )

    parsed = tracker.parse_tracking_text(summary["visible_text"])

    assert summary["oxylabs_used"] is True
    assert summary["oxylabs_status_code"] == 200
    assert summary["oxylabs_error"] is None
    assert summary["fetch_failed"] is False
    assert summary["warning"] is None
    assert summary["access_denied_detected"] is False
    assert parsed == {
        "status": "Arrived",
        "eta": "2026-05-02 14:30",
        "origin": "SFO",
        "destination": "ATL",
        "last_update": "2026-05-01 08:15",
        "exception": False,
    }


@pytest.mark.asyncio
async def test_delta_tracker_ignores_already_closed_context_cleanup(monkeypatch) -> None:
    tracker = DeltaTracker()

    class FakeContext:
        async def add_init_script(self, script: str) -> None:
            return None

        async def close(self) -> None:
            raise RuntimeError("Target page, context or browser has been closed")

    class FakeBrowser:
        async def new_context(self, **kwargs):
            return FakeContext()

        async def close(self) -> None:
            return None

    class FakeChromium:
        async def launch(self, **kwargs):
            return FakeBrowser()

    class FakePlaywright:
        def __init__(self) -> None:
            self.chromium = FakeChromium()

        async def stop(self) -> None:
            return None

    class FakeAsyncPlaywrightStarter:
        async def start(self) -> FakePlaywright:
            return FakePlaywright()

    async def fake_fetch_with_retries(
        context,
        tracking_url: str,
        original_awb: str,
        normalized_awb: str,
    ):
        return {
            "tracking_url": tracking_url,
            "normalized_awb": normalized_awb,
            "visible_text": "Access Denied\nReference #18.abc123",
            "page_title": "Access Denied",
            "final_url": "https://errors.edgesuite.net/18.abc123",
            "retry_count": 2,
            "access_denied_detected": True,
            "screenshot_path": None,
            "fetch_failed": False,
            "warning": "Delta Cargo access denied by Akamai after retries.",
        }

    monkeypatch.setattr(
        "app.trackers.delta.async_playwright",
        lambda: FakeAsyncPlaywrightStarter(),
    )
    monkeypatch.setattr(tracker, "_fetch_with_retries", fake_fetch_with_retries)

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

    assert result.raw_summary["tracking_url"] == (
        "https://www.deltacargo.com/Cargo/trackShipment?awbNumber=00622953556"
    )
    assert result.raw_summary["normalized_awb"] == "00622953556"
    assert result.raw_summary["page_title"] == "Access Denied"
    assert result.raw_summary["final_url"] == "https://errors.edgesuite.net/18.abc123"
    assert result.raw_summary["retry_count"] == 2
    assert result.raw_summary["access_denied_detected"] is True
    assert result.raw_summary["warning"] == (
        "Delta Cargo access denied by Akamai after retries."
    )
    assert "cleanup_warning" not in result.raw_summary
    assert result.error == "Delta Cargo access denied by Akamai after retries."
