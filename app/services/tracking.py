from __future__ import annotations

from app.config import settings
from app.models import ShipmentRequest, TrackingResult
from app.trackers.base import PlaceholderTracker
from app.trackers.registry import get_tracker
from app.trackers.track_trace import TrackTraceTracker


TRACK_TRACE_TRACKER = TrackTraceTracker()


async def track_shipment(shipment: ShipmentRequest) -> TrackingResult:
    carrier_tracker = get_tracker(shipment.carrier)

    if not settings.track_trace_enabled:
        return await carrier_tracker.track(shipment)

    track_trace_result = await TRACK_TRACE_TRACKER.track(shipment)
    if should_use_track_trace_result(track_trace_result, carrier_tracker):
        return track_trace_result

    carrier_result = await carrier_tracker.track(shipment)
    if track_trace_result.raw_summary:
        carrier_result.raw_summary = {
            **carrier_result.raw_summary,
            "track_trace": track_trace_result.raw_summary,
        }
    return carrier_result


def should_use_track_trace_result(
    result: TrackingResult,
    carrier_tracker,
) -> bool:
    parsed_fields = result.raw_summary.get("parsed_fields", {})
    if any(
        parsed_fields.get(field)
        for field in ("status", "eta", "origin", "destination", "last_update")
    ):
        return True

    if parsed_fields.get("exception") is True:
        return True

    if isinstance(carrier_tracker, PlaceholderTracker):
        visible_text = result.raw_summary.get("visible_text", "").strip()
        final_url = result.raw_summary.get("final_url", "")
        return bool(visible_text) or "track-trace.com" not in final_url

    return False
