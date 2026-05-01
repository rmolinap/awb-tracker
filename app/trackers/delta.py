from __future__ import annotations

import asyncio
import html
import random
import re
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import Browser, BrowserContext, Error as PlaywrightError
from playwright.async_api import Page, Playwright, TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from app.config import settings
from app.models import ShipmentRequest, TrackingResult
from app.services.normalize import normalize_awb
from app.services.oxylabs import fetch_with_oxylabs
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
HTML_BLOCK_TAG_RE = re.compile(
    r"</?(?:article|aside|br|div|footer|h[1-6]|header|li|main|ol|p|section|table|td|th|tr|ul)[^>]*>",
    re.IGNORECASE,
)
HTML_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
HTML_TAG_RE = re.compile(r"<[^>]+>")
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


def is_closed_resource_error(exc: Exception) -> bool:
    return "target page, context or browser has been closed" in str(exc).lower()


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


async def safe_close_page(page: Optional[Page]) -> Optional[str]:
    if page is None:
        return None
    try:
        await page.close()
    except Exception as exc:
        if is_closed_resource_error(exc):
            return None
        return "Delta page cleanup failed: {error}".format(error=exc)
    return None


async def safe_close_context(context: Optional[BrowserContext]) -> Optional[str]:
    if context is None:
        return None
    try:
        await context.close()
    except Exception as exc:
        if is_closed_resource_error(exc):
            return None
        return "Delta browser context cleanup failed: {error}".format(error=exc)
    return None


async def safe_close_browser(browser: Optional[Browser]) -> Optional[str]:
    if browser is None:
        return None
    try:
        await browser.close()
    except Exception as exc:
        if is_closed_resource_error(exc):
            return None
        return "Delta browser cleanup failed: {error}".format(error=exc)
    return None


async def safe_stop_playwright(playwright: Optional[Playwright]) -> Optional[str]:
    if playwright is None:
        return None
    try:
        await playwright.stop()
    except Exception as exc:
        if is_closed_resource_error(exc):
            return None
        return "Delta Playwright shutdown failed: {error}".format(error=exc)
    return None


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

        if raw_summary.get("fetch_failed"):
            result.error = raw_summary.get("warning", "Delta tracking fetch failed.")
        elif raw_summary.get("access_denied_detected") and not parsed_fields["status"]:
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
        playwright: Optional[Playwright] = None
        browser: Optional[Browser] = None
        context: Optional[BrowserContext] = None
        summary: Optional[dict[str, Any]] = None

        try:
            playwright = await async_playwright().start()
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
            summary = await self._fetch_with_retries(
                context=context,
                tracking_url=tracking_url,
                original_awb=original_awb,
                normalized_awb=normalized_awb,
            )
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            summary = self._build_error_summary(
                tracking_url=tracking_url,
                normalized_awb=normalized_awb,
                warning=str(exc),
            )
        except Exception as exc:
            summary = self._build_error_summary(
                tracking_url=tracking_url,
                normalized_awb=normalized_awb,
                warning=str(exc),
            )
        finally:
            cleanup_warning = self._combine_cleanup_warnings(
                await safe_close_context(context),
                await safe_close_browser(browser),
                await safe_stop_playwright(playwright),
            )
            if summary is None:
                summary = self._build_error_summary(
                    tracking_url=tracking_url,
                    normalized_awb=normalized_awb,
                    warning=cleanup_warning or "Delta tracker did not capture a response.",
                )
            elif cleanup_warning:
                self._record_cleanup_warning(summary, cleanup_warning)

        return await self._maybe_apply_oxylabs_fallback(
            tracking_url=tracking_url,
            normalized_awb=normalized_awb,
            summary=summary,
        )

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
        page: Optional[Page] = None
        summary: Optional[dict[str, Any]] = None

        try:
            page = await context.new_page()
            await page.goto(
                tracking_url,
                wait_until="domcontentloaded",
                timeout=settings.tracker_timeout_seconds * 1000,
            )
            await self._wait_for_page_settle(page)

            visible_text = await page.locator("body").inner_text()
            page_title = await self._safe_page_title(page)
            final_url = self._safe_page_url(page, tracking_url)
            summary = {
                "tracking_url": tracking_url,
                "normalized_awb": normalized_awb,
                "visible_text": visible_text,
                "page_title": page_title,
                "final_url": final_url,
                "retry_count": 0,
                "access_denied_detected": detect_access_denied(visible_text),
                "screenshot_path": None,
                "fetch_failed": False,
            }

            if detect_access_denied(self._build_access_denied_haystack(summary)):
                summary["screenshot_path"] = await self._maybe_take_access_denied_screenshot(
                    page=page,
                    original_awb=original_awb,
                    attempt_number=attempt_number,
                )
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            summary = self._build_error_summary(
                tracking_url=tracking_url,
                normalized_awb=normalized_awb,
                warning=str(exc),
                page_title=await self._safe_page_title(page),
                final_url=self._safe_page_url(page, tracking_url),
            )
        except Exception as exc:
            summary = self._build_error_summary(
                tracking_url=tracking_url,
                normalized_awb=normalized_awb,
                warning=str(exc),
                page_title=await self._safe_page_title(page),
                final_url=self._safe_page_url(page, tracking_url),
            )
        finally:
            page_close_warning = await safe_close_page(page)
            if summary is None:
                summary = self._build_error_summary(
                    tracking_url=tracking_url,
                    normalized_awb=normalized_awb,
                    warning=page_close_warning or "Delta page did not return a response.",
                    page_title=await self._safe_page_title(page),
                    final_url=self._safe_page_url(page, tracking_url),
                )
            elif page_close_warning:
                self._record_cleanup_warning(summary, page_close_warning)

        return summary

    def _should_try_oxylabs(self, summary: dict[str, Any]) -> bool:
        return settings.oxylabs_enabled and summary.get("access_denied_detected") is True

    async def _maybe_apply_oxylabs_fallback(
        self,
        tracking_url: str,
        normalized_awb: str,
        summary: dict[str, Any],
    ) -> dict[str, Any]:
        resolved_summary = self._ensure_oxylabs_metadata(summary)
        if not self._should_try_oxylabs(resolved_summary):
            return resolved_summary

        return await self._fetch_tracking_page_with_oxylabs(
            tracking_url=tracking_url,
            normalized_awb=normalized_awb,
            current_summary=resolved_summary,
        )

    async def _fetch_tracking_page_with_oxylabs(
        self,
        tracking_url: str,
        normalized_awb: str,
        current_summary: dict[str, Any],
    ) -> dict[str, Any]:
        summary = self._ensure_oxylabs_metadata(dict(current_summary))
        summary["oxylabs_used"] = True

        oxylabs_result = await asyncio.to_thread(fetch_with_oxylabs, tracking_url)
        summary["oxylabs_status_code"] = oxylabs_result.get("status_code")
        summary["oxylabs_error"] = oxylabs_result.get("error")

        if not oxylabs_result.get("ok"):
            return summary

        oxylabs_visible_text = self._extract_visible_text_from_content(
            oxylabs_result.get("content", "")
        )
        if detect_access_denied(oxylabs_visible_text):
            summary["oxylabs_error"] = (
                "Oxylabs response still matched Delta access denied markers."
            )
            return summary

        summary.update(
            {
                "tracking_url": tracking_url,
                "normalized_awb": normalized_awb,
                "visible_text": oxylabs_visible_text,
                "page_title": "Delta Cargo via Oxylabs",
                "final_url": oxylabs_result.get("result_url") or tracking_url,
                "playwright_access_denied_detected": current_summary.get(
                    "access_denied_detected",
                    False,
                ),
                "access_denied_detected": False,
                "fetch_failed": False,
                "warning": None,
                "oxylabs_content": oxylabs_result.get("content"),
            }
        )
        return summary

    def _build_error_summary(
        self,
        tracking_url: str,
        normalized_awb: str,
        warning: str,
        *,
        visible_text: str = "",
        page_title: Optional[str] = None,
        final_url: Optional[str] = None,
        retry_count: int = 0,
        access_denied_detected: Optional[bool] = None,
        screenshot_path: Optional[str] = None,
        fetch_failed: bool = True,
    ) -> dict[str, Any]:
        resolved_final_url = final_url or tracking_url
        if access_denied_detected is None:
            access_denied_detected = detect_access_denied(
                " ".join(
                    part
                    for part in (
                        visible_text,
                        page_title or "",
                        resolved_final_url,
                        warning,
                    )
                    if part
                )
            )

        return {
            "tracking_url": tracking_url,
            "normalized_awb": normalized_awb,
            "visible_text": visible_text,
            "page_title": page_title,
            "final_url": resolved_final_url,
            "retry_count": retry_count,
            "access_denied_detected": access_denied_detected,
            "screenshot_path": screenshot_path,
            "fetch_failed": fetch_failed,
            "warning": warning,
            "oxylabs_used": False,
            "oxylabs_status_code": None,
            "oxylabs_error": None,
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

    async def _safe_page_title(self, page: Optional[Page]) -> Optional[str]:
        if page is None:
            return None
        try:
            return await page.title()
        except Exception:
            return None

    def _safe_page_url(self, page: Optional[Page], fallback_url: str) -> str:
        if page is None:
            return fallback_url
        try:
            return page.url or fallback_url
        except Exception:
            return fallback_url

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

    def _combine_cleanup_warnings(self, *warnings: Optional[str]) -> Optional[str]:
        filtered = [warning for warning in warnings if warning]
        if not filtered:
            return None
        return " | ".join(filtered)

    def _record_cleanup_warning(
        self,
        summary: dict[str, Any],
        cleanup_warning: str,
    ) -> None:
        if summary.get("warning"):
            summary["cleanup_warning"] = cleanup_warning
            return
        summary["warning"] = cleanup_warning

    def _ensure_oxylabs_metadata(self, summary: dict[str, Any]) -> dict[str, Any]:
        summary.setdefault("oxylabs_used", False)
        summary.setdefault("oxylabs_status_code", None)
        summary.setdefault("oxylabs_error", None)
        return summary

    def _extract_visible_text_from_content(self, content: str) -> str:
        if "<" not in content or ">" not in content:
            return content

        without_scripts = HTML_SCRIPT_STYLE_RE.sub(" ", content)
        with_block_breaks = HTML_BLOCK_TAG_RE.sub("\n", without_scripts)
        without_tags = HTML_TAG_RE.sub(" ", with_block_breaks)
        return html.unescape(without_tags)
