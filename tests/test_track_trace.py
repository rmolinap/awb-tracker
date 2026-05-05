from pathlib import Path

import pytest

from app.models import ShipmentRequest
from app.trackers.track_trace import (
    TrackTraceTracker,
    extract_visible_text_from_html,
    format_track_trace_warning,
    format_awb_for_track_trace,
    track_with_track_trace,
)


FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def test_format_awb_for_track_trace() -> None:
    assert format_awb_for_track_trace("00622953556") == "006-22953556"
    assert format_awb_for_track_trace("020-12345675") == "020-12345675"


def test_track_trace_parse_tracking_text_extracts_structured_fields() -> None:
    tracker = TrackTraceTracker()
    visible_text = load_fixture("track_trace_tracking_page.txt")

    parsed = tracker.parse_tracking_text(
        visible_text=visible_text,
        carrier="United",
        final_url="https://www.track-trace.com/aircargo",
    )

    assert parsed == {
        "status": "Received from airline",
        "eta": "2026-05-03 11:40",
        "origin": "AMS",
        "destination": "JFK",
        "last_update": "2026-05-02 18:25",
        "exception": False,
    }


def test_track_trace_uses_delta_parser_when_direct_resolves_to_delta() -> None:
    tracker = TrackTraceTracker()
    visible_text = load_fixture("delta_tracking_page.txt")

    parsed = tracker.parse_tracking_text(
        visible_text=visible_text,
        carrier="Delta",
        final_url="https://www.deltacargo.com/Cargo/trackShipment?awbNumber=00622953556",
    )

    assert parsed == {
        "status": "Arrived",
        "eta": "2026-05-02 14:30",
        "origin": "SFO",
        "destination": "ATL",
        "last_update": "2026-05-01 08:15",
        "exception": False,
    }


def test_extract_visible_text_from_html() -> None:
    text = extract_visible_text_from_html(
        """
        <html>
          <body>
            <div>Status</div>
            <div>Arrived</div>
            <div>Destination</div>
            <div>ATL</div>
          </body>
        </html>
        """
    )

    assert "Status" in text
    assert "Arrived" in text
    assert "ATL" in text


def test_format_track_trace_warning_adds_provider_context() -> None:
    assert format_track_trace_warning("timeout") == "TrackTrace request failed: timeout"


@pytest.mark.asyncio
async def test_track_trace_tracker_returns_structured_result_from_sample_text(
    monkeypatch,
) -> None:
    tracker = TrackTraceTracker()
    visible_text = load_fixture("track_trace_tracking_page.txt")

    async def fake_track_awb(awb: str, carrier: str):
        return {
            "provider": "track_trace",
            "tracking_url": "https://www.track-trace.com/aircargo",
            "input_awb": "020-12345675",
            "normalized_awb": "02012345675",
            "normalized_carrier": "united",
            "visible_text": visible_text,
            "page_title": "Air cargo tracking - track-trace",
            "final_url": "https://www.track-trace.com/aircargo",
            "html_content": "<html></html>",
            "used_direct": True,
            "direct_button_present": True,
            "direct_resolution_type": "navigated",
            "delta_direct_behavior": None,
            "used_frame": False,
            "frame_url": None,
            "fetch_failed": False,
            "warning": None,
        }

    monkeypatch.setattr(tracker, "track_awb", fake_track_awb)

    result = await tracker.track(
        ShipmentRequest(
            carrier="United",
            awb="020-12345675",
            customer="Inland",
            po_number=None,
            notify_email="employee@company.com",
            arrival_location="JFK",
        )
    )

    assert result.carrier == "United"
    assert result.awb == "020-12345675"
    assert result.status == "Received from airline"
    assert result.eta == "2026-05-03 11:40"
    assert result.origin == "AMS"
    assert result.destination == "JFK"
    assert result.last_update == "2026-05-02 18:25"
    assert result.exception is False
    assert result.tracking_url == "https://www.track-trace.com/aircargo"
    assert result.raw_summary["parsed_fields"]["status"] == "Received from airline"
    assert result.error is None


@pytest.mark.asyncio
async def test_wait_for_direct_resolution_uses_playwright_arg_keyword() -> None:
    tracker = TrackTraceTracker()

    class FakeLocator:
        async def text_content(self, timeout=None):
            return "form"

    class FakePage:
        def __init__(self) -> None:
            self.url = "https://www.track-trace.com/aircargo"
            self.wait_call = None

        async def wait_for_function(
            self,
            expression: str,
            *,
            arg=None,
            timeout=None,
        ):
            self.wait_call = {
                "expression": expression,
                "arg": arg,
                "timeout": timeout,
            }
            return None

        def locator(self, selector: str):
            assert selector == "#direct-data #direct-type"
            return FakeLocator()

    page = FakePage()

    resolution = await tracker._wait_for_direct_resolution(
        page=page,
        original_url="https://www.track-trace.com/aircargo",
    )

    assert resolution == "form"
    assert page.wait_call is not None
    assert page.wait_call["arg"] == "https://www.track-trace.com/aircargo"
    assert "baseUrl" in page.wait_call["expression"]


@pytest.mark.asyncio
async def test_track_trace_tracker_surfaces_contextual_warning_on_fetch_failure(
    monkeypatch,
) -> None:
    tracker = TrackTraceTracker()

    async def fake_track_awb(awb: str, carrier: str):
        return {
            "provider": "track_trace",
            "tracking_url": "https://www.track-trace.com/aircargo",
            "input_awb": "020-12345675",
            "normalized_awb": "02012345675",
            "normalized_carrier": "united",
            "visible_text": "",
            "page_title": "Air cargo tracking - track-trace",
            "final_url": "https://www.track-trace.com/aircargo",
            "html_content": "",
            "used_direct": True,
            "direct_button_present": True,
            "direct_resolution_type": None,
            "delta_direct_behavior": None,
            "used_frame": False,
            "frame_url": None,
            "fetch_failed": True,
            "warning": format_track_trace_warning(
                "site returned an empty direct response"
            ),
        }

    monkeypatch.setattr(tracker, "track_awb", fake_track_awb)

    result = await tracker.track(
        ShipmentRequest(
            carrier="United",
            awb="020-12345675",
            customer="Inland",
            po_number=None,
            notify_email="employee@company.com",
            arrival_location="JFK",
        )
    )

    assert result.error == "TrackTrace request failed: site returned an empty direct response"
    assert result.raw_summary["warning"].startswith("TrackTrace request failed:")


@pytest.mark.asyncio
async def test_track_with_track_trace_function_accepts_awb_and_carrier(
    monkeypatch,
) -> None:
    async def fake_track_awb(self, awb: str, carrier: str):
        assert awb == "020-12345675"
        assert carrier == "United"
        return {
            "provider": "track_trace",
            "tracking_url": "https://www.track-trace.com/aircargo",
            "input_awb": "020-12345675",
            "normalized_awb": "02012345675",
            "normalized_carrier": "united",
            "visible_text": "Status\nReceived from airline",
            "page_title": "Air cargo tracking - track-trace",
            "final_url": "https://www.track-trace.com/aircargo",
            "html_content": "<html></html>",
            "used_direct": True,
            "direct_button_present": True,
            "direct_resolution_type": "navigated",
            "delta_direct_behavior": None,
            "used_frame": False,
            "frame_url": None,
            "fetch_failed": False,
            "warning": None,
        }

    monkeypatch.setattr(TrackTraceTracker, "track_awb", fake_track_awb)

    result = await track_with_track_trace("020-12345675", "United")

    assert result["normalized_awb"] == "02012345675"
    assert result["normalized_carrier"] == "united"
