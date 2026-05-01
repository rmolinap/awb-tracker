from app.services.normalize import normalize_carrier
from app.trackers.base import BaseTracker, PlaceholderTracker
from app.trackers.delta import DeltaTracker


TRACKER_REGISTRY: dict[str, BaseTracker] = {
    "delta": DeltaTracker(),
    "united": PlaceholderTracker("united"),
    "southwest": PlaceholderTracker("southwest"),
    "american": PlaceholderTracker("american"),
    "alaska": PlaceholderTracker("alaska"),
}


def get_tracker(carrier_name: str) -> BaseTracker:
    carrier_code = normalize_carrier(carrier_name)
    return TRACKER_REGISTRY.get(carrier_code, PlaceholderTracker(carrier_code))
