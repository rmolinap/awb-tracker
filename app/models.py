from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, EmailStr, Field


class ShipmentRequest(BaseModel):
    carrier: str
    awb: str
    customer: str
    po_number: Optional[str] = None
    notify_email: EmailStr
    arrival_location: str


class TrackRequest(BaseModel):
    shipments: list[ShipmentRequest] = Field(default_factory=list)


class TrackingResult(BaseModel):
    carrier: str
    awb: str
    customer: str
    status: Optional[str] = None
    eta: Optional[str] = None
    origin: Optional[str] = None
    destination: Optional[str] = None
    last_update: Optional[str] = None
    exception: bool = False
    tracking_url: Optional[str] = None
    screenshot_path: Optional[str] = None
    raw_summary: dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None


class TrackResponse(BaseModel):
    results: list[TrackingResult]


class HealthResponse(BaseModel):
    ok: bool
