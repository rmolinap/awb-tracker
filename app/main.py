from fastapi import FastAPI

from app.models import HealthResponse, TrackRequest, TrackResponse
from app.services.tracking import track_shipment

app = FastAPI(title="Air Cargo AWB Tracking Service")


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(ok=True)


@app.post("/track", response_model=TrackResponse)
async def track_shipments(payload: TrackRequest) -> TrackResponse:
    results = []
    for shipment in payload.shipments:
        result = await track_shipment(shipment)
        results.append(result)
    return TrackResponse(results=results)
