from __future__ import annotations

import asyncio
import random
import re
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import Browser, BrowserContext, Error as PlaywrightError
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from app.config import settings
from app.models import ShipmentRequest, TrackingResult
from app.services.normalize import normalize_awb
from app.trackers.base import BaseTracker


REALISTIC_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)
REALISTIC_VIEWPORT = {"width": 1440, "height": 900}
ACCEPT_LANGUAGE = "en-US,en;q=0.9"
ACCESS_DENIED_MARKERS = (
    "access denied",
    "reference #",
    "errors.edgesuite.net",
)
STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {
  get: () => undefined,
});

Object.defineProperty(navigator, 'languages', {
  get: () => ['en-US', 'en'],
});

Object.defineProperty(navigator, 'plugins', {
  get: () => [1, 2, 3, 4, 5],
});

Object.defineProperty(navigator, 'platform', {
  get: () => 'MacIntel',
});

Object.defineProperty(navigator, 'hardwareConcurrency', {
  get: () => 8,
});

if (!window.chrome) {
  Object.defineProperty(window, 'chrome', {
    value: { runtime: {} },
    configurable: true,
  });
}

const originalQuery = window.navigator.permissions?.query;
if (originalQuery) {
  window.navigator.permissions.query = (parameters) => (
    parameters && parameters.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : originalQuery(parameters)
  );
}
"""


def detect_access_denied(text: str) -> bool:
    haystack = text.lower()
    return any(marker in haystack for marker in ACCESS_DENIED_MARKERS)


def build_access_denied_screenshot_path(
    screenshot_dir: str | Path,
    awb: str,
    attempt_number: int,
) -> Path:
    normalized_awb = normalize_awb(awb)
    return Path(screenshot_dir) / (
        "delta_{awb}_access_denied_attempt_{attempt}.png".format(
            awb=normalized_awb,
            attempt=attempt_number,
        )
    )


class DeltaTracker(BaseTracker):
    carrier_code = "delta"
    base_url = "https://www.deltacargo.com/Cargo/trackShipment?awbNumber={awb}"
    max_access_denied_retries = 2
    exception_keywords = (
        "exception",
        "delay",
        "delayed",
        "hold",
        "held",
        "problem",
        "failure",
        "unable",
        "missed",
        "cancelled",
        "canceled",
    )

    def build_tracking_url(self, awb: str) -> str:
        return self.base_url.format(awb=normalize_awb(awb))

    async def track(self, shipment: ShipmentRequest) -> TrackingResult:
        result = self.build_base_result(shipment)
        tracking_url = self.build_tracking_url(shipment.awb)
        result.tracking_url = tracking_url

        try:
            raw_summary = await self._fetch_tracking_page(tracking_url, shipment.awb)
        except Exception as exc:  # pragma: no cover - defensive boundary
            result.error = "Unexpected Delta tracking error: {error}".format(error=exc)
            result.raw_summary = {"tracking_url": tracking_url}
            return result

        result.raw_summary = raw_summary
        visible_text = raw_summary.get("visible_text", "")
        parsed_fields = self.parse_tracking_text(visible_text)

        result.status = parsed_fields["status"]
        result.eta = parsed_fields["eta"]
        result.origin = parsed_fields["origin"]
        if parsed_fields["destination"]:
            result.destination = parsed_fields["destination"]
        result.last_update = parsed_fields["last_update"]
        result.exception = parsed_fields["exception"]
        result.screenshot_path = raw_summary.get("screenshot_path")

        if raw_summary.get("access_denied_detected") and not parsed_fields["status"]:
            result.error = raw_summary.get(
                "warning",
                "Delta Cargo access denied by Akamai.",
            )

        raw_summary["parsed_fields"] = parsed_fields
        return result

    def parse_tracking_text(self, visible_text: str) -> dict[str, Any]:
        lines = self._normalize_lines(visible_text)
        status = self._extract_labeled_value(lines, ("status",))
        eta = self._extract_labeled_value(lines, ("eta", "estimated arrival", "arrival"))
        origin = self._extract_labeled_value(lines, ("origin",))
        destination = self._extract_labeled_value(lines, ("destination", "dest"))
        last_update = self._extract_labeled_value(
            lines,
            ("last update", "last updated", "updated"),
        )
        exception = self._detect_exception_flag(status=status, visible_text=visible_text)

        return {
            "status": status,
            "eta": eta,
            "origin": origin,
            "destination": destination,
            "last_update": last_update,
            "exception": exception,
        }

    def _normalize_lines(self, visible_text: str) -> list[str]:
        lines = []
        for raw_line in visible_text.splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip()
            if line:
                lines.append(line)
        return lines

    def _extract_labeled_value(
        self,
        lines: list[str],
        labels: tuple[str, ...],
    ) -> Optional[str]:
        lowered_labels = tuple(label.lower() for label in labels)

        for index, line in enumerate(lines):
            normalized_line = line.strip()
            lowered_line = normalized_line.lower()

            for label in lowered_labels:
                if lowered_line == label:
                    return self._next_non_empty_line(lines, index + 1)

                prefix = "{label}:".format(label=label)
                if lowered_line.startswith(prefix):
                    value = normalized_line[len(prefix) :].strip()
                    return value or self._next_non_empty_line(lines, index + 1)

                alt_prefix = "{label} -".format(label=label)
                if lowered_line.startswith(alt_prefix):
                    value = normalized_line[len(alt_prefix) :].strip()
                    return value or self._next_non_empty_line(lines, index + 1)

        return None

    def _next_non_empty_line(self, lines: list[str], start_index: int) -> Optional[str]:
        for line in lines[start_index:]:
            if line:
                return line
        return None

    def _detect_exception_flag(self, status: Optional[str], visible_text: str) -> bool:
        haystack = " ".join(filter(None, [status or "", visible_text])).lower()
        return any(keyword in haystack for keyword in self.exception_keywords)

    async def _fetch_tracking_page(
        self,
        tracking_url: str,
        original_awb: str,
    ) -> dict[str, Any]:
        normalized_awb = normalize_awb(original_awb)
        browser: Optional[Browser] = None
        context: Optional[BrowserContext] = None

        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    headless=settings.playwright_headless,
                    slow_mo=settings.playwright_slowmo_ms,
                    args=self._browser_launch_args(),
                    ignore_default_args=["--enable-automation"],
                )
                context = await browser.new_context(
                    user_agent=REALISTIC_USER_AGENT,
                    viewport=REALISTIC_VIEWPORT,
                    locale="en-US",
                    timezone_id="America/Los_Angeles",
                    java_script_enabled=True,
                    extra_http_headers={"Accept-Language": ACCEPT_LANGUAGE},
                )
                await context.add_init_script(STEALTH_INIT_SCRIPT)
                return await self._fetch_with_retries(
                    context=context,
                    tracking_url=tracking_url,
                    original_awb=original_awb,
                    normalized_awb=normalized_awb,
                )
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            return self._build_error_summary(
                tracking_url=tracking_url,
                normalized_awb=normalized_awb,
                warning=str(exc),
            )
        except Exception as exc:
            return self._build_error_summary(
                tracking_url=tracking_url,
                normalized_awb=normalized_awb,
                warning=str(exc),
            )
        finally:
            if context is not None:
                await context.close()
            if browser is not None:
                await browser.close()

    def _browser_launch_args(self) -> list[str]:
        return [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-popup-blocking",
            "--window-size={width},{height}".format(**REALISTIC_VIEWPORT),
        ]

    async def _fetch_with_retries(
        self,
        context: BrowserContext,
        tracking_url: str,
        original_awb: str,
        normalized_awb: str,
    ) -> dict[str, Any]:
        access_denied_detected = False
        denied_screenshot_path: Optional[str] = None
        last_summary = self._build_error_summary(
            tracking_url=tracking_url,
            normalized_awb=normalized_awb,
            warning="Delta tracker did not capture a response.",
        )

        for attempt_number in range(1, self.max_access_denied_retries + 2):
            attempt_summary = await self._fetch_tracking_attempt(
                context=context,
                tracking_url=tracking_url,
                original_awb=original_awb,
                normalized_awb=normalized_awb,
                attempt_number=attempt_number,
            )
            last_summary = attempt_summary

            if attempt_summary.get("screenshot_path"):
                denied_screenshot_path = attempt_summary["screenshot_path"]

            combined_text = self._build_access_denied_haystack(attempt_summary)
            attempt_denied = detect_access_denied(combined_text)
            access_denied_detected = access_denied_detected or attempt_denied

            if not attempt_denied:
                break

            if attempt_number <= self.max_access_denied_retries:
                await self._sleep_before_retry()

        last_summary["retry_count"] = max(0, attempt_number - 1)
        last_summary["access_denied_detected"] = access_denied_detected
        if denied_screenshot_path and not last_summary.get("screenshot_path"):
            last_summary["screenshot_path"] = denied_screenshot_path
        if detect_access_denied(self._build_access_denied_haystack(last_summary)):
            last_summary["warning"] = "Delta Cargo access denied by Akamai after retries."
        return last_summary

    async def _fetch_tracking_attempt(
        self,
        context: BrowserContext,
        tracking_url: str,
        original_awb: str,
        normalized_awb: str,
        attempt_number: int,
    ) -> dict[str, Any]:
        page = await context.new_page()

        try:
            await page.goto(
                tracking_url,
                wait_until="domcontentloaded",
                timeout=settings.tracker_timeout_seconds * 1000,
            )
            await self._wait_for_page_settle(page)

            visible_text = await page.locator("body").inner_text()
            page_title = await self._safe_page_title(page)
            final_url = page.url
            summary = {
                "tracking_url": tracking_url,
                "normalized_awb": normalized_awb,
                "visible_text": visible_text,
                "page_title": page_title,
                "final_url": final_url,
                "screenshot_path": None,
            }

            if detect_access_denied(self._build_access_denied_haystack(summary)):
                summary["screenshot_path"] = await self._maybe_take_access_denied_screenshot(
                    page=page,
                    original_awb=original_awb,
                    attempt_number=attempt_number,
                )

            return summary
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            return {
                "tracking_url": tracking_url,
                "normalized_awb": normalized_awb,
                "visible_text": "",
                "page_title": await self._safe_page_title(page),
                "final_url": page.url or tracking_url,
                "screenshot_path": None,
                "warning": str(exc),
            }
        except Exception as exc:
            return {
                "tracking_url": tracking_url,
                "normalized_awb": normalized_awb,
                "visible_text": "",
                "page_title": await self._safe_page_title(page),
                "final_url": page.url or tracking_url,
                "screenshot_path": None,
                "warning": str(exc),
            }
        finally:
            await page.close()

    def _build_error_summary(
        self,
        tracking_url: str,
        normalized_awb: str,
        warning: str,
    ) -> dict[str, Any]:
        return {
            "tracking_url": tracking_url,
            "normalized_awb": normalized_awb,
            "visible_text": "",
            "page_title": None,
            "final_url": tracking_url,
            "retry_count": 0,
            "access_denied_detected": False,
            "screenshot_path": None,
            "warning": warning,
        }

    def _build_access_denied_haystack(self, summary: dict[str, Any]) -> str:
        parts = (
            summary.get("visible_text", ""),
            summary.get("page_title", ""),
            summary.get("final_url", ""),
            summary.get("warning", ""),
        )
        return " ".join(part for part in parts if part)

    async def _wait_for_page_settle(self, page: Page) -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except PlaywrightTimeoutError:
            return

    async def _safe_page_title(self, page: Page) -> Optional[str]:
        try:
            return await page.title()
        except Exception:
            return None

    async def _sleep_before_retry(self) -> None:
        await asyncio.sleep(random.uniform(0.75, 1.75))

    async def _maybe_take_access_denied_screenshot(
        self,
        page: Page,
        original_awb: str,
        attempt_number: int,
    ) -> Optional[str]:
        if not settings.enable_screenshots:
            return None

        screenshot_dir = Path(settings.screenshot_dir)
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = build_access_denied_screenshot_path(
            screenshot_dir=screenshot_dir,
            awb=original_awb,
            attempt_number=attempt_number,
        )
        await page.screenshot(path=str(screenshot_path), full_page=True)
        return str(screenshot_path)
