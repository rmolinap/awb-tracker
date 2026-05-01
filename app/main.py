from fastapi import FastAPI

from app.models import HealthResponse, TrackRequest, TrackResponse
from app.trackers.registry import get_tracker

app = FastAPI(title="Air Cargo AWB Tracking Service")


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(ok=True)


@app.post("/track", response_model=TrackResponse)
async def track_shipments(payload: TrackRequest) -> TrackResponse:
    results = []
    for shipment in payload.shipments:
        tracker = get_tracker(shipment.carrier)
        result = await tracker.track(shipment)
        results.append(result)
    return TrackResponse(results=results)
