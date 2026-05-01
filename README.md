# Air Cargo AWB Tracking Service

Internal FastAPI service for tracking air cargo AWB shipments and returning normalized JSON for n8n workflows.

## Scope

Phase 1 focuses on:
- internal use only
- Delta Cargo implemented first
- clean placeholders for United, Southwest, American, and Alaska
- structured JSON responses for downstream automation

## Project structure

```text
app/
  __init__.py
  main.py
  models.py
  config.py
  trackers/
    __init__.py
    base.py
    delta.py
    registry.py
  services/
    __init__.py
    normalize.py
    oxylabs.py
tests/
  fixtures/
```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Install Playwright browsers

```bash
playwright install chromium
```

If browser installation is skipped, the Delta tracker still returns structured results, but live page capture may include an error message in the response.

## Environment variables

Copy `.env.example` to `.env` and adjust as needed.

```bash
cp .env.example .env
```

Available settings:
- `TRACKER_TIMEOUT_SECONDS`
- `ENABLE_SCREENSHOTS`
- `SCREENSHOT_DIR`
- `PLAYWRIGHT_HEADLESS`
- `PLAYWRIGHT_SLOWMO_MS`
- `OXYLABS_ENABLED`
- `OXYLABS_USERNAME`
- `OXYLABS_PASSWORD`
- `OXYLABS_ENDPOINT`

## Run the app

```bash
uvicorn app.main:app --reload
```

Default local URL:

```text
http://127.0.0.1:8000
```

## Run tests

```bash
pytest
```

## Sample curl request

```bash
curl -X POST http://127.0.0.1:8000/track \
  -H "Content-Type: application/json" \
  -d '{
    "shipments": [
      {
        "carrier": "Delta Cargo",
        "awb": "006-22953556",
        "customer": "Inland",
        "po_number": null,
        "notify_email": "employee@company.com",
        "arrival_location": "ATL"
      }
    ]
  }'
```

## Response shape

```json
{
  "results": [
    {
      "carrier": "Delta",
      "awb": "006-22953556",
      "customer": "Inland",
      "status": "Arrived",
      "eta": "2026-05-02 14:30",
      "origin": "SFO",
      "destination": "ATL",
      "last_update": "2026-05-01 08:15",
      "exception": false,
      "tracking_url": "https://www.deltacargo.com/Cargo/trackShipment?awbNumber=00622953556",
      "screenshot_path": null,
      "raw_summary": {},
      "error": null
    }
  ]
}
```

## n8n integration notes

- n8n can send batches of shipment rows to `POST /track`.
- Each shipment result is independent; one carrier failure should not crash the whole request.
- Preserve the original AWB in the response for sheet updates and reporting.
- Use `tracking_url`, `raw_summary`, and `error` for debugging failed or partial carrier lookups.
- `raw_summary` keeps the original visible page text and parsed debug fields for support work.
- Future reporting steps can map `status`, `eta`, `origin`, `destination`, and `last_update` into Google Sheets columns and internal email summaries.

## Delta Akamai behavior

Delta Cargo is fronted by Akamai and may return an `Access Denied` page even when the AWB is valid. This usually means the browser session was fingerprinted or rate-limited before the tracker could read the shipment details.

Current mitigation in the Delta tracker:
- launches Chromium with more realistic browser settings
- removes common automation flags where Playwright allows it
- injects lightweight stealth overrides such as hiding `navigator.webdriver`
- retries up to two times when the response looks like an Akamai denial page
- calls Oxylabs Web Scraper API only after those direct retries still end in Akamai denial and `OXYLABS_ENABLED=true`
- captures screenshots of denied pages when `ENABLE_SCREENSHOTS=true`
- returns structured JSON with `page_title`, `final_url`, `retry_count`, `access_denied_detected`, `oxylabs_used`, `oxylabs_status_code`, and `oxylabs_error` in `raw_summary`

This reduces obvious bot fingerprints, but it does not guarantee access. Akamai can still block direct requests from the local machine or hosting environment.

## Oxylabs fallback

The Oxylabs fallback is implemented for Delta only. It is intentionally narrow:
- direct Playwright remains the first attempt for every Delta request
- Oxylabs is skipped unless `OXYLABS_ENABLED=true`
- Oxylabs is only called when the direct Delta flow detects an Akamai denial page after retries
- the same Delta parser is reused by converting the Oxylabs HTML response into visible text before parsing
- if Oxylabs still fails, the original Akamai error stays in `error` and `raw_summary.oxylabs_error` explains the fallback failure

Required env vars:
- `OXYLABS_ENABLED=false`
- `OXYLABS_USERNAME=`
- `OXYLABS_PASSWORD=`
- `OXYLABS_ENDPOINT=https://realtime.oxylabs.io/v1/queries`

## Troubleshooting Delta access denied

If Delta returns an Akamai denial page:
- confirm Playwright browsers are installed with `playwright install chromium`
- inspect `raw_summary.page_title`, `raw_summary.final_url`, `raw_summary.retry_count`, and `raw_summary.access_denied_detected`
- if Oxylabs is enabled, also inspect `raw_summary.oxylabs_used`, `raw_summary.oxylabs_status_code`, and `raw_summary.oxylabs_error`
- enable screenshots with `ENABLE_SCREENSHOTS=true` and review the saved denied-page image
- keep `PLAYWRIGHT_HEADLESS=true` for server environments, but try local debugging with `PLAYWRIGHT_HEADLESS=false`
- use a small non-zero `PLAYWRIGHT_SLOWMO_MS` only for debugging; it is not a proxy substitute
- if denials remain consistent across retries, enable the Delta-only Oxylabs fallback rather than changing the parser
