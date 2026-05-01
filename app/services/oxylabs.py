from __future__ import annotations

from typing import Any

import httpx

from app.config import settings


def fetch_with_oxylabs(url: str) -> dict[str, Any]:
    if not settings.oxylabs_username or not settings.oxylabs_password:
        return {
            "ok": False,
            "status_code": None,
            "content": "",
            "error": "Oxylabs credentials are not configured.",
        }

    payload = {
        "source": "universal",
        "url": url,
        "geo_location": "United States",
        "render": "html",
    }

    try:
        response = httpx.post(
            settings.oxylabs_endpoint,
            auth=(settings.oxylabs_username, settings.oxylabs_password),
            json=payload,
            timeout=settings.tracker_timeout_seconds,
        )
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "status_code": None,
            "content": "",
            "error": "Oxylabs request failed: {error}".format(error=exc),
        }

    try:
        response_body = response.json()
    except ValueError as exc:
        return {
            "ok": False,
            "status_code": response.status_code,
            "content": "",
            "error": "Oxylabs returned invalid JSON: {error}".format(error=exc),
        }

    if response.status_code >= 400:
        return {
            "ok": False,
            "status_code": response.status_code,
            "content": "",
            "error": "Oxylabs request failed with status {status_code}.".format(
                status_code=response.status_code
            ),
        }

    results = response_body.get("results")
    if not isinstance(results, list) or not results:
        return {
            "ok": False,
            "status_code": response.status_code,
            "content": "",
            "error": "Oxylabs response did not include any results.",
        }

    first_result = results[0]
    if not isinstance(first_result, dict):
        return {
            "ok": False,
            "status_code": response.status_code,
            "content": "",
            "error": "Oxylabs returned an unexpected result payload.",
        }

    content = first_result.get("content")
    result_status_code = first_result.get("status_code")
    resolved_status_code = (
        result_status_code if isinstance(result_status_code, int) else response.status_code
    )

    if not isinstance(content, str) or not content:
        return {
            "ok": False,
            "status_code": resolved_status_code,
            "content": "",
            "error": "Oxylabs response did not include HTML content.",
        }

    return {
        "ok": True,
        "status_code": resolved_status_code,
        "content": content,
        "error": None,
        "result_url": first_result.get("url"),
        "job_id": first_result.get("job_id"),
    }
