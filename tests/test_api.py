from fastapi.testclient import TestClient

from app.main import app
from app.models import ShipmentRequest
from app.trackers.delta import DeltaTracker
from app.trackers.registry import TRACKER_REGISTRY


class FakeDeltaTracker(DeltaTracker):
    async def track(self, shipment: ShipmentRequest):
        result = self.build_base_result(shipment)
        result.tracking_url = self.build_tracking_url(shipment.awb)
        result.raw_summary = {
            "tracking_url": result.tracking_url,
            "visible_text": "Status\nArrived\nDestination\nATL",
            "oxylabs_used": False,
            "oxylabs_status_code": None,
            "oxylabs_error": None,
            "parsed_fields": {
                "status": "Arrived",
                "eta": None,
                "origin": None,
                "destination": "ATL",
                "last_update": None,
                "exception": False,
            },
        }
        result.status = "Arrived"
        result.destination = "ATL"
        return result


client = TestClient(app)


def test_health() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_track_valid_payload(monkeypatch) -> None:
    monkeypatch.setitem(TRACKER_REGISTRY, "delta", FakeDeltaTracker())

    response = client.post(
        "/track",
        json={
            "shipments": [
                {
                    "carrier": "Delta",
                    "awb": "006-22953556",
                    "customer": "Inland",
                    "po_number": None,
                    "notify_email": "employee@company.com",
                    "arrival_location": "ATL",
                }
            ]
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "results": [
            {
                "carrier": "Delta",
                "awb": "006-22953556",
                "customer": "Inland",
                "status": "Arrived",
                "eta": None,
                "origin": None,
                "destination": "ATL",
                "last_update": None,
                "exception": False,
                "tracking_url": "https://www.deltacargo.com/Cargo/trackShipment?awbNumber=00622953556",
                "screenshot_path": None,
                "raw_summary": {
                    "tracking_url": "https://www.deltacargo.com/Cargo/trackShipment?awbNumber=00622953556",
                    "visible_text": "Status\nArrived\nDestination\nATL",
                    "oxylabs_used": False,
                    "oxylabs_status_code": None,
                    "oxylabs_error": None,
                    "parsed_fields": {
                        "status": "Arrived",
                        "eta": None,
                        "origin": None,
                        "destination": "ATL",
                        "last_update": None,
                        "exception": False,
                    },
                },
                "error": None,
            }
        ]
    }


def test_track_unsupported_carrier_returns_structured_error() -> None:
    response = client.post(
        "/track",
        json={
            "shipments": [
                {
                    "carrier": "Lufthansa",
                    "awb": "020-12345675",
                    "customer": "Inland",
                    "po_number": None,
                    "notify_email": "employee@company.com",
                    "arrival_location": "JFK",
                }
            ]
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["results"][0]["carrier"] == "Lufthansa"
    assert payload["results"][0]["awb"] == "020-12345675"
    assert payload["results"][0]["error"] == (
        "Tracker for carrier 'lufthansa' is not implemented yet."
    )


def test_track_delta_oxylabs_failure_returns_structured_error(monkeypatch) -> None:
    tracker = DeltaTracker()

    async def fake_fetch_tracking_page(tracking_url: str, original_awb: str):
        return {
            "tracking_url": tracking_url,
            "normalized_awb": "00622953556",
            "visible_text": "Access Denied\nReference #18.abc123",
            "page_title": "Access Denied",
            "final_url": "https://errors.edgesuite.net/18.abc123",
            "retry_count": 2,
            "access_denied_detected": True,
            "screenshot_path": None,
            "fetch_failed": False,
            "warning": "Delta Cargo access denied by Akamai after retries.",
            "oxylabs_used": True,
            "oxylabs_status_code": 502,
            "oxylabs_error": "Oxylabs upstream failed.",
        }

    monkeypatch.setitem(TRACKER_REGISTRY, "delta", tracker)
    monkeypatch.setattr(tracker, "_fetch_tracking_page", fake_fetch_tracking_page)

    response = client.post(
        "/track",
        json={
            "shipments": [
                {
                    "carrier": "Delta",
                    "awb": "006-22953556",
                    "customer": "Inland",
                    "po_number": None,
                    "notify_email": "employee@company.com",
                    "arrival_location": "ATL",
                }
            ]
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["results"][0]["error"] == "Delta Cargo access denied by Akamai after retries."
    assert payload["results"][0]["raw_summary"]["oxylabs_used"] is True
    assert payload["results"][0]["raw_summary"]["oxylabs_status_code"] == 502
    assert payload["results"][0]["raw_summary"]["oxylabs_error"] == (
        "Oxylabs upstream failed."
    )
