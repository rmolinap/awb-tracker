from __future__ import annotations

import html
import re
from typing import Any, Optional
from urllib.parse import urlencode

from playwright.async_api import Browser, BrowserContext, Error as PlaywrightError
from playwright.async_api import Page, Playwright, TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from app.config import settings
from app.models import ShipmentRequest, TrackingResult
from app.services.normalize import normalize_awb, normalize_carrier
from app.trackers.base import BaseTracker
from app.trackers.delta import (
    ACCEPT_LANGUAGE,
    REALISTIC_USER_AGENT,
    REALISTIC_VIEWPORT,
    STEALTH_INIT_SCRIPT,
    DeltaTracker,
    safe_close_browser,
    safe_close_context,
    safe_close_page,
    safe_stop_playwright,
)


TRACK_TRACE_URL = "https://www.track-trace.com/aircargo"
HTML_BLOCK_TAG_RE = re.compile(
    r"</?(?:article|aside|br|div|footer|form|h[1-6]|header|li|main|ol|p|section|table|td|th|tr|ul)[^>]*>",
    re.IGNORECASE,
)
HTML_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
HTML_TAG_RE = re.compile(r"<[^>]+>")


def format_awb_for_track_trace(awb: str) -> str:
    normalized_awb = normalize_awb(awb)
    if len(normalized_awb) > 3:
        return "{prefix}-{suffix}".format(
            prefix=normalized_awb[:3],
            suffix=normalized_awb[3:],
        )
    return normalized_awb


async def track_with_track_trace(awb: str, carrier: str) -> dict[str, Any]:
    tracker = TrackTraceTracker()
    return await tracker.track_awb(awb=awb, carrier=carrier)


def format_track_trace_warning(error: Exception | str) -> str:
    return "TrackTrace request failed: {error}".format(error=error)


class TrackTraceTracker(BaseTracker):
    carrier_code = "track_trace"
    base_url = TRACK_TRACE_URL
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

    def __init__(self) -> None:
        self._delta_tracker = DeltaTracker()

    async def track(self, shipment: ShipmentRequest) -> TrackingResult:
        result = self.build_base_result(shipment)

        try:
            raw_summary = await self.track_awb(
                awb=shipment.awb,
                carrier=shipment.carrier,
            )
        except Exception as exc:  # pragma: no cover - defensive boundary
            warning = format_track_trace_warning(exc)
            result.error = "Unexpected TrackTrace tracking error: {error}".format(
                error=warning
            )
            result.tracking_url = self.base_url
            result.raw_summary = {
                "provider": "track_trace",
                "tracking_url": self.base_url,
                "fetch_failed": True,
                "warning": warning,
            }
            return result

        result.raw_summary = raw_summary
        result.tracking_url = raw_summary.get("final_url") or self.base_url

        parsed_fields = self.parse_tracking_text(
            visible_text=raw_summary.get("visible_text", ""),
            carrier=shipment.carrier,
            final_url=raw_summary.get("final_url"),
        )
        raw_summary["parsed_fields"] = parsed_fields

        result.status = parsed_fields["status"]
        result.eta = parsed_fields["eta"]
        result.origin = parsed_fields["origin"]
        if parsed_fields["destination"]:
            result.destination = parsed_fields["destination"]
        result.last_update = parsed_fields["last_update"]
        result.exception = parsed_fields["exception"]

        if raw_summary.get("fetch_failed"):
            result.error = raw_summary.get("warning", "TrackTrace tracking fetch failed.")

        return result

    async def track_awb(self, awb: str, carrier: str) -> dict[str, Any]:
        formatted_awb = format_awb_for_track_trace(awb)
        normalized_awb = normalize_awb(awb)
        normalized_carrier = normalize_carrier(carrier)

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
            await context.add_init_script(
                "window.localStorage.setItem('settings-open-self', '1');"
            )
            summary = await self._fetch_tracking_page(
                context=context,
                formatted_awb=formatted_awb,
                normalized_awb=normalized_awb,
                normalized_carrier=normalized_carrier,
            )
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            summary = self._build_error_summary(
                formatted_awb=formatted_awb,
                normalized_awb=normalized_awb,
                normalized_carrier=normalized_carrier,
                warning=format_track_trace_warning(exc),
            )
        except Exception as exc:
            summary = self._build_error_summary(
                formatted_awb=formatted_awb,
                normalized_awb=normalized_awb,
                normalized_carrier=normalized_carrier,
                warning=format_track_trace_warning(exc),
            )
        finally:
            cleanup_warning = self._combine_cleanup_warnings(
                await safe_close_context(context),
                await safe_close_browser(browser),
                await safe_stop_playwright(playwright),
            )
            if summary is None:
                summary = self._build_error_summary(
                    formatted_awb=formatted_awb,
                    normalized_awb=normalized_awb,
                    normalized_carrier=normalized_carrier,
                    warning=cleanup_warning
                    or "TrackTrace tracker did not capture a response.",
                )
            elif cleanup_warning:
                self._record_cleanup_warning(summary, cleanup_warning)

        return summary

    def parse_tracking_text(
        self,
        visible_text: str,
        carrier: str,
        final_url: Optional[str],
    ) -> dict[str, Any]:
        normalized_carrier = normalize_carrier(carrier)
        if normalized_carrier == "delta" or "deltacargo.com" in (final_url or ""):
            parsed_fields = self._delta_tracker.parse_tracking_text(visible_text)
            if self._has_structured_fields(parsed_fields):
                return parsed_fields

        lines = self._normalize_lines(visible_text)
        status = self._extract_labeled_value(
            lines,
            ("status", "current status", "shipment status"),
        )
        eta = self._extract_labeled_value(
            lines,
            ("eta", "estimated arrival", "arrival", "arrival time"),
        )
        origin = self._extract_labeled_value(lines, ("origin", "from"))
        destination = self._extract_labeled_value(
            lines,
            ("destination", "to", "dest"),
        )
        last_update = self._extract_labeled_value(
            lines,
            ("last update", "last updated", "updated", "tracking date"),
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

    async def _fetch_tracking_page(
        self,
        context: BrowserContext,
        formatted_awb: str,
        normalized_awb: str,
        normalized_carrier: str,
    ) -> dict[str, Any]:
        page: Optional[Page] = None
        summary: Optional[dict[str, Any]] = None
        original_url = self.base_url

        try:
            page = await context.new_page()
            await page.goto(
                self.base_url,
                wait_until="domcontentloaded",
                timeout=settings.tracker_timeout_seconds * 1000,
            )
            await self._wait_for_page_settle(page)

            awb_input = page.locator('input[name="number"]')
            await awb_input.fill(formatted_awb)

            direct_button = page.locator("#wc-multi-form-button_direct")
            direct_button_present = await direct_button.count() > 0
            used_direct = False
            direct_resolution_type: Optional[str] = None
            delta_direct_behavior: Optional[str] = None

            if direct_button_present:
                used_direct = True
                await self._wait_for_direct_button(page)
                await direct_button.click()
                direct_resolution_type = await self._wait_for_direct_resolution(
                    page=page,
                    original_url=original_url,
                )
                if page.url == original_url:
                    (
                        direct_resolution_type,
                        delta_direct_behavior,
                    ) = await self._handle_direct_response(page)
            else:
                await page.locator("#wc-multi-form-button_options").click()
                direct_resolution_type = "options"

            await self._wait_for_page_settle(page)
            visible_text, html_content, frame_metadata = await self._capture_page_payload(
                page
            )
            final_url = self._safe_page_url(page, self.base_url)

            summary = {
                "provider": "track_trace",
                "tracking_url": self.base_url,
                "input_awb": formatted_awb,
                "normalized_awb": normalized_awb,
                "normalized_carrier": normalized_carrier,
                "visible_text": visible_text,
                "page_title": await self._safe_page_title(page),
                "final_url": final_url,
                "html_content": html_content,
                "used_direct": used_direct,
                "direct_button_present": direct_button_present,
                "direct_resolution_type": direct_resolution_type,
                "delta_direct_behavior": delta_direct_behavior,
                "fetch_failed": False,
                "warning": None,
                **frame_metadata,
            }
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            summary = self._build_error_summary(
                formatted_awb=formatted_awb,
                normalized_awb=normalized_awb,
                normalized_carrier=normalized_carrier,
                warning=str(exc),
                page_title=await self._safe_page_title(page),
                final_url=self._safe_page_url(page, self.base_url),
            )
        except Exception as exc:
            summary = self._build_error_summary(
                formatted_awb=formatted_awb,
                normalized_awb=normalized_awb,
                normalized_carrier=normalized_carrier,
                warning=str(exc),
                page_title=await self._safe_page_title(page),
                final_url=self._safe_page_url(page, self.base_url),
            )
        finally:
            page_close_warning = await safe_close_page(page)
            if summary is None:
                summary = self._build_error_summary(
                    formatted_awb=formatted_awb,
                    normalized_awb=normalized_awb,
                    normalized_carrier=normalized_carrier,
                    warning=page_close_warning or "TrackTrace page did not return a response.",
                    page_title=await self._safe_page_title(page),
                    final_url=self._safe_page_url(page, self.base_url),
                )
            elif page_close_warning:
                self._record_cleanup_warning(summary, page_close_warning)

        return summary

    def _browser_launch_args(self) -> list[str]:
        return [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-popup-blocking",
            "--window-size={width},{height}".format(**REALISTIC_VIEWPORT),
        ]

    async def _wait_for_direct_button(self, page: Page) -> None:
        await page.wait_for_function(
            """
            () => {
              const button = document.querySelector('#wc-multi-form-button_direct');
              return button && !button.disabled;
            }
            """,
            timeout=5000,
        )

    async def _wait_for_direct_resolution(
        self,
        page: Page,
        original_url: str,
    ) -> Optional[str]:
        try:
            await page.wait_for_function(
                """
                (baseUrl) => {
                  const directType = document.querySelector('#direct-data #direct-type');
                  return window.location.href !== baseUrl || !!directType;
                }
                """,
                arg=original_url,
                timeout=settings.tracker_timeout_seconds * 1000,
            )
        except PlaywrightTimeoutError:
            return None

        if page.url != original_url:
            return "navigated"

        try:
            direct_type = await page.locator("#direct-data #direct-type").text_content(
                timeout=1000
            )
        except PlaywrightTimeoutError:
            return None
        return (direct_type or "").strip() or None

    async def _handle_direct_response(
        self,
        page: Page,
    ) -> tuple[Optional[str], Optional[str]]:
        direct_type = await page.evaluate(
            """
            () => {
              const node = document.querySelector('#direct-data #direct-type');
              return node ? node.textContent.trim() : null;
            }
            """
        )
        if direct_type == "form":
            form_payload = await page.evaluate(
                """
                () => {
                  const form = document.querySelector('#direct-form form');
                  if (!form) {
                    return null;
                  }

                  const inputs = Array.from(form.querySelectorAll('input')).map((input) => ({
                    name: input.name,
                    value: input.value,
                    type: input.type,
                  }));

                  return {
                    action: form.action,
                    method: (form.method || 'get').toLowerCase(),
                    target: form.target || '',
                    inputs,
                  };
                }
                """
            )
            if not form_payload:
                return direct_type, None

            delta_behavior = None
            if "deltacargo.com" in (form_payload.get("action") or ""):
                delta_behavior = (
                    "TrackTrace direct returned a hidden Delta form; "
                    "the tracker submitted it in-page to avoid the options frame."
                )

            if form_payload["method"] == "get":
                params = {}
                for item in form_payload["inputs"]:
                    name = item.get("name")
                    if name:
                        params[name] = item.get("value", "")
                query = urlencode(params)
                destination_url = form_payload["action"]
                if query:
                    separator = "&" if "?" in destination_url else "?"
                    destination_url = "{url}{separator}{query}".format(
                        url=destination_url,
                        separator=separator,
                        query=query,
                    )
                await page.goto(
                    destination_url,
                    wait_until="domcontentloaded",
                    timeout=settings.tracker_timeout_seconds * 1000,
                )
                return direct_type, delta_behavior

            await page.evaluate(
                """
                () => {
                  if (document.trackform) {
                    document.trackform.target = '_top';
                    document.trackform.submit();
                  }
                }
                """
            )
            return direct_type, delta_behavior

        if direct_type == "url":
            destination_url = await page.evaluate(
                """
                () => {
                  const node = document.querySelector('#direct-form');
                  return node ? node.textContent.trim() : null;
                }
                """
            )
            if destination_url:
                await page.goto(
                    destination_url,
                    wait_until="domcontentloaded",
                    timeout=settings.tracker_timeout_seconds * 1000,
                )
            return direct_type, None

        return direct_type, None

    async def _capture_page_payload(
        self,
        page: Page,
    ) -> tuple[str, str, dict[str, Any]]:
        visible_text = await page.locator("body").inner_text()
        html_content = await page.content()
        frame_metadata = {
            "used_frame": False,
            "frame_url": None,
        }

        if visible_text.strip():
            return visible_text, html_content, frame_metadata

        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                frame_text = await frame.locator("body").inner_text(timeout=1000)
            except Exception:
                continue
            if frame_text.strip():
                frame_metadata["used_frame"] = True
                frame_metadata["frame_url"] = frame.url
                frame_html = await frame.content()
                return frame_text, frame_html, frame_metadata

        return visible_text, html_content, frame_metadata

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

    def _has_structured_fields(self, parsed_fields: dict[str, Any]) -> bool:
        return any(
            parsed_fields.get(field)
            for field in ("status", "eta", "origin", "destination", "last_update")
        ) or parsed_fields.get("exception") is True

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

    def _build_error_summary(
        self,
        formatted_awb: str,
        normalized_awb: str,
        normalized_carrier: str,
        warning: str,
        *,
        page_title: Optional[str] = None,
        final_url: Optional[str] = None,
    ) -> dict[str, Any]:
        return {
            "provider": "track_trace",
            "tracking_url": self.base_url,
            "input_awb": formatted_awb,
            "normalized_awb": normalized_awb,
            "normalized_carrier": normalized_carrier,
            "visible_text": "",
            "page_title": page_title,
            "final_url": final_url or self.base_url,
            "html_content": "",
            "used_direct": True,
            "direct_button_present": None,
            "direct_resolution_type": None,
            "delta_direct_behavior": None,
            "used_frame": False,
            "frame_url": None,
            "fetch_failed": True,
            "warning": warning,
        }

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


def extract_visible_text_from_html(content: str) -> str:
    if "<" not in content or ">" not in content:
        return content

    without_scripts = HTML_SCRIPT_STYLE_RE.sub(" ", content)
    with_block_breaks = HTML_BLOCK_TAG_RE.sub("\n", without_scripts)
    without_tags = HTML_TAG_RE.sub(" ", with_block_breaks)
    return html.unescape(without_tags)
