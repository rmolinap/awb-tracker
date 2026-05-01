# AGENTS.md

## Project context

This repository contains the Air Cargo AWB Tracking microservice used in an internal seafood wholesale logistics workflow.

Primary workflow:
- n8n sends shipment rows to this service.
- The service normalizes carrier and AWB data.
- Carrier-specific trackers fetch cargo tracking pages.
- The service returns structured JSON to n8n.
- n8n updates Google Sheets and sends internal email reports.

Phase 1 scope:
- Internal use only
- Service skeleton in place
- Delta Cargo implemented first
- United, Southwest, American, and Alaska left as clean placeholders
- No client notifications
- No Oxylabs integration yet

## Tech stack

- Python
- FastAPI
- Pydantic
- Playwright
- pytest
- Docker-ready project layout

## Main endpoints

### `GET /health`

Response:

```json
{ "ok": true }
```

### `POST /track`

Accepts:

```json
{
  "shipments": [
    {
      "carrier": "Delta",
      "awb": "006-22953556",
      "customer": "Inland",
      "po_number": null,
      "notify_email": "employee@company.com",
      "arrival_location": "ATL"
    }
  ]
}
```

Returns structured tracking results per shipment. Preserve the original AWB in the response. Use normalized carrier and normalized AWB internally.

## Carrier support plan

Phase 1:
- `delta`: implemented first
- `united`: placeholder tracker
- `southwest`: placeholder tracker
- `american`: placeholder tracker
- `alaska`: placeholder tracker

Normalization rules:
- Delta, delta, Delta Cargo -> `delta`
- United, United Cargo -> `united`
- Southwest, SWA, Southwest Cargo -> `southwest`
- American, AA, American Airlines Cargo -> `american`
- Alaska, Alaska Cargo -> `alaska`

Implementation expectations:
- Delta tracker should generate the official Delta Cargo tracking URL using the normalized AWB.
- Delta tracker should extract structured fields from visible page text when available.
- Unsupported or placeholder carriers must return structured error results.
- Do not let one bad carrier response crash the full batch request.

## Coding rules

- Keep modules small and explicit.
- Prefer typed, composable functions over hidden side effects.
- Preserve request/response schema stability.
- Normalize inputs in a dedicated service layer.
- Keep tracker-specific logic inside `app/trackers/`.
- Return structured error payloads instead of raising unhandled exceptions for expected tracking failures.
- Keep parser logic testable without live website access.
- Avoid embedding network assumptions in tests.
- Use ASCII unless an existing file requires otherwise.
- Keep placeholders honest: clear about what is implemented versus not yet implemented.

## Testing commands

Create a virtual environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

Run tests:

```bash
pytest
```

Run a focused test file:

```bash
pytest tests/test_delta.py
```

## Environment variables

- `TRACKER_TIMEOUT_SECONDS=30`
- `ENABLE_SCREENSHOTS=false`
- `SCREENSHOT_DIR=./screenshots`

Optional future additions should remain centralized in `app/config.py`.

## Definition of done

- Project files are created
- FastAPI app runs locally
- Tests pass with `pytest`
- `GET /health` works
- `POST /track` returns structured JSON
- Delta URL generation works
- Delta parsing returns useful structured fields when the page exposes them
- Code is clean and extensible for more carriers
