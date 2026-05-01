from __future__ import annotations

from abc import ABC, abstractmethod

from app.models import ShipmentRequest, TrackingResult
from app.services.normalize import normalize_awb, normalize_carrier


CARRIER_DISPLAY_NAMES = {
    "delta": "Delta",
    "united": "United",
    "southwest": "Southwest",
    "american": "American",
    "alaska": "Alaska",
}


class BaseTracker(ABC):
    carrier_code: str

    @abstractmethod
    async def track(self, shipment: ShipmentRequest) -> TrackingResult:
        raise NotImplementedError

    def build_base_result(self, shipment: ShipmentRequest) -> TrackingResult:
        carrier_code = normalize_carrier(shipment.carrier)
        return TrackingResult(
            carrier=CARRIER_DISPLAY_NAMES.get(carrier_code, shipment.carrier),
            awb=shipment.awb,
            customer=shipment.customer,
            destination=shipment.arrival_location,
        )


class PlaceholderTracker(BaseTracker):
    def __init__(self, carrier_code: str) -> None:
        self.carrier_code = carrier_code

    async def track(self, shipment: ShipmentRequest) -> TrackingResult:
        result = self.build_base_result(shipment)
        normalized_awb = normalize_awb(shipment.awb)
        result.error = (
            "Tracker for carrier '{carrier}' is not implemented yet.".format(
                carrier=self.carrier_code
            )
        )
        result.raw_summary = {
            "normalized_carrier": normalize_carrier(shipment.carrier),
            "normalized_awb": normalized_awb,
        }
        return result
