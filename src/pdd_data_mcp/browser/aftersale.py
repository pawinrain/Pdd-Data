"""CDP collector for aftersale_orders (enter list via sidebar/tab, not blind goto)."""

from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Browser, Page, Response

from pdd_data_mcp.browser.aftersale_parsing import parse_aftersale_list_response
from pdd_data_mcp.browser.cdp import CdpConnector, open_missing_page, select_page_by_url
from pdd_data_mcp.config import CollectionSettings, ConnectionSettings
from pdd_data_mcp.contracts.models import (
    Coverage,
    CoverageStatus,
    DatasetType,
    IdentityEvidence,
    Quality,
    Scope,
    SnapshotDraft,
    WindowKind,
)
from pdd_data_mcp.errors import CollectionRejected
from pdd_data_mcp.utils import scope_key, utc_now

_LIST_PATH_HINT = "/aftersales/aftersale_list"
_SETUP_PATH_HINT = "/aftersales/setup"
_WORKBENCH_LABELS = ("售后工作台", "退款/售后", "待商家处理售后")
# Safe in-app entry used only when auto-opening a tab: the list URL itself redirects to
# the setup page on blind navigation, so we open home and enter via the sidebar instead.
_MMS_ENTRY_URL = "https://mms.pinduoduo.com/home"


class AftersaleCdpCollector:
    def __init__(
        self,
        *,
        connection: ConnectionSettings,
        collection: CollectionSettings,
        runtime_root: Path,
        connector: CdpConnector | None = None,
    ) -> None:
        self.connection = connection
        self.collection = collection
        self.runtime_root = runtime_root.resolve(strict=False)
        self.connector = connector or CdpConnector()
        self.adapter = connection.aftersale_adapter

    async def collect(
        self,
        *,
        store_id: str,
        dataset_type: DatasetType,
        scope: Scope,
        limit: int,
        batch_id: str | None = None,
    ) -> SnapshotDraft:
        if dataset_type is not DatasetType.AFTERSALE_ORDERS:
            raise CollectionRejected("DATASET_UNVERIFIED", "REAL_DATASET_NOT_ADAPTED")
        if store_id != self.connection.store_id:
            raise CollectionRejected("IDENTITY_MISMATCH", "STORE_ID_MISMATCH")
        if not self.adapter.verified:
            raise CollectionRejected("ADAPTER_UNVERIFIED", "AFTERSALE_ADAPTER_NOT_VERIFIED")
        if scope.kind is not WindowKind.POINT_IN_TIME:
            raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "AFTERSALE_SCOPE_NOT_ADAPTED")
        if "POINT_IN_TIME" not in self.adapter.supported_windows:
            raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "AFTERSALE_WINDOW_UNSUPPORTED")
        if not (
            self.connection.expected_platform_store_id
            or self.connection.expected_platform_store_id_sha256
        ):
            raise CollectionRejected("IDENTITY_UNVERIFIED", "EXPECTED_PLATFORM_STORE_ID_MISSING")

        requested_at = utc_now()
        started_at = utc_now()
        session = await self.connector.connect(
            self.connection, self.collection.connect_timeout_ms
        )
        try:
            merchant_identity = await self._merchant_page_identity(session.browser)

            bodies: list[bytes] = []
            tasks: set[asyncio.Task[None]] = set()

            async def consume(response: Response) -> None:
                parsed = urlsplit(response.url)
                if (
                    parsed.hostname != self.adapter.response_host
                    or parsed.path != self.adapter.response_path
                    or response.request.method != self.adapter.response_method
                    or response.status != self.adapter.response_http_status
                ):
                    return
                if response.request.resource_type not in {"xhr", "fetch"}:
                    return
                try:
                    raw = await response.body()
                except Exception:
                    return
                if len(raw) <= self.collection.max_browser_response_bytes:
                    bodies.append(raw)

            def on_response(response: Response) -> None:
                task = asyncio.create_task(consume(response))
                tasks.add(task)
                task.add_done_callback(tasks.discard)

            # Attach listener on all contexts before navigating into the list,
            # so the first queryList during tab switch is not missed.
            for context in session.browser.contexts:
                context.on("response", on_response)
            try:
                page = await self._open_aftersale_list_page(session.browser)
                if self.adapter.login_selector and await page.locator(
                    self.adapter.login_selector
                ).count():
                    raise CollectionRejected("AUTH_REQUIRED", "AFTERSALE_LOGIN_REQUIRED")
                if self.adapter.captcha_selector and await page.locator(
                    self.adapter.captcha_selector
                ).count():
                    raise CollectionRejected("AUTH_REQUIRED", "CAPTCHA_PRESENT")
                if _SETUP_PATH_HINT in (page.url or ""):
                    raise CollectionRejected(
                        "TARGET_PAGE_NOT_FOUND",
                        "AFTERSALE_SETUP_REQUIRED",
                    )

                # Always click 待商家处理 so we don't keep an empty default queryList.
                before = len(bodies)
                await self._trigger_list_capture(page)
                try:
                    await page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    pass
                deadline = asyncio.get_running_loop().time() + 8
                while asyncio.get_running_loop().time() < deadline and len(bodies) <= before:
                    await asyncio.sleep(0.25)
                if tasks:
                    await asyncio.wait(tasks, timeout=2)
            finally:
                for context in session.browser.contexts:
                    try:
                        context.remove_listener("response", on_response)
                    except Exception:
                        pass
                for task in list(tasks):
                    if not task.done():
                        task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

            if not bodies:
                raise CollectionRejected("DATA_MISMATCH", "AFTERSALE_RESPONSE_MISSING")
            captured_at = utc_now()
            # Prefer the latest non-empty list payload (default tab may return total=0).
            records = []
            total = None
            for raw in reversed(bodies):
                candidate_records, candidate_total = parse_aftersale_list_response(
                    raw, adapter=self.adapter, observed_at=captured_at
                )
                if candidate_records or (candidate_total or 0) > 0:
                    records, total = candidate_records, candidate_total
                    break
            if not records and total is None:
                records, total = parse_aftersale_list_response(
                    bodies[-1], adapter=self.adapter, observed_at=captured_at
                )
            if limit > 0:
                records = records[:limit]
        finally:
            await session.disconnect()

        return SnapshotDraft(
            batch_id=batch_id,
            store_id=self.connection.store_id,
            dataset_type=DatasetType.AFTERSALE_ORDERS,
            scope=scope,
            scope_key=scope_key(scope),
            requested_at=requested_at,
            capture_started_at=started_at,
            capture_finished_at=utc_now(),
            captured_at=captured_at,
            metric_window=None,
            source_updated_at=None,
            source="PDD_BROWSER_CDP",
            capture_method="NETWORK_RESPONSE",
            parser_version=self.adapter.parser_version or "pdd-aftersale-orders/0.1.0",
            identity_evidence=IdentityEvidence(
                expected_platform_store_id=(
                    self.connection.expected_platform_store_id
                    or f"sha256:{self.connection.expected_platform_store_id_sha256}"
                ),
                observed_platform_store_id=merchant_identity,
                evidence_source="DOM",
                independent_verification_method="MERCHANT_PAGE_STATE_SHA256",
                independent_verification_reference=self.adapter.identity_verification_reference
                or "merchant-basic-page-bootstrap-v1",
            ),
            field_sources={"records": "NETWORK_RESPONSE"},
            payload=[row.model_dump(mode="json") for row in records],
            missing_fields=[],
            quality=Quality(
                status="VALID",
                identity="MATCHED",
                coverage=CoverageStatus.COMPLETE,
                dom_check="MATCHED",
            ),
            coverage=Coverage(
                total_observed=total if total is not None else len(records),
                captured=len(records),
                limit=limit,
                pages_read=1,
                coverage=(
                    CoverageStatus.TRUNCATED
                    if (total is not None and len(records) < total)
                    else CoverageStatus.COMPLETE
                ),
                truncated=bool(total is not None and len(records) < total),
            ),
        )

    async def _open_aftersale_list_page(self, browser: Browser) -> Page:
        """Prefer existing list tab; otherwise click sidebar/tab. Never blind-goto list URL."""
        list_pages = self._pages_matching(browser, _LIST_PATH_HINT)
        if list_pages:
            page = list_pages[0]
            return page

        mms_pages = [
            page
            for context in browser.contexts
            for page in context.pages
            if not page.is_closed() and "mms.pinduoduo.com" in (page.url or "")
        ]
        if not mms_pages:
            if not self.collection.auto_open_missing_pages:
                raise CollectionRejected(
                    "TARGET_PAGE_NOT_FOUND",
                    "AFTERSALE_MMS_TAB_MISSING",
                )
            # Never blind-goto the list URL (it redirects to setup): open a safe entry
            # tab and reach the list through the in-app sidebar below.
            page = await open_missing_page(
                browser,
                _MMS_ENTRY_URL,
                login_selector=self.adapter.login_selector,
                captcha_selector=self.adapter.captcha_selector,
                error_selector=self.adapter.error_selector,
                page_code="AFTERSALE_ENTRY_PAGE",
                open_timeout_ms=self.collection.page_open_timeout_ms,
            )
        else:
            # Prefer an aftersales/* tab (setup included): clicking workbench from setup
            # is reliable.
            preferred = [
                page
                for page in mms_pages
                if "/aftersales/" in (page.url or "")
            ] or [
                page
                for page in mms_pages
                if "/home" in (page.url or "")
            ] or mms_pages
            page = preferred[0]

        clicked = await self._click_workbench_entry(page)
        if not clicked and mms_pages:
            home_pages = [p for p in mms_pages if "/home" in (p.url or "")]
            if home_pages:
                page = home_pages[0]
                clicked = await self._click_workbench_entry(page)
        if not clicked:
            raise CollectionRejected(
                "TARGET_PAGE_NOT_FOUND",
                "AFTERSALE_WORKBENCH_ENTRY_NOT_FOUND",
            )

        await page.wait_for_timeout(2500)
        list_pages = self._pages_matching(browser, _LIST_PATH_HINT)
        if list_pages:
            page = list_pages[0]
            return page

        # Current tab may have navigated in-place.
        if _LIST_PATH_HINT in (page.url or ""):
            return page
        if _SETUP_PATH_HINT in (page.url or ""):
            raise CollectionRejected(
                "TARGET_PAGE_NOT_FOUND",
                "AFTERSALE_SETUP_REQUIRED",
            )
        raise CollectionRejected(
            "TARGET_PAGE_NOT_FOUND",
            "AFTERSALE_LIST_NOT_REACHED",
        )

    def _pages_matching(self, browser: Browser, path_hint: str) -> list[Page]:
        return [
            page
            for context in browser.contexts
            for page in context.pages
            if not page.is_closed() and path_hint in (page.url or "")
        ]

    async def _click_workbench_entry(self, page: Page) -> bool:
        # Exact sidebar href first (more stable than fuzzy text).
        selectors = (
            'a[href="/aftersales/aftersale_list"]',
            'a[href*="/aftersales/aftersale_list"]',
            'a[href*="aftersale_list"]',
        )
        for selector in selectors:
            loc = page.locator(selector).first
            try:
                if await loc.count() == 0:
                    continue
                try:
                    async with page.expect_navigation(timeout=10_000):
                        await loc.click(force=True, timeout=3_000)
                except Exception:
                    await loc.click(force=True, timeout=3_000)
                # Give SPA a moment; success is decided by caller via final URL/tabs.
                await page.wait_for_timeout(1_500)
                return True
            except Exception:
                continue
        for label in _WORKBENCH_LABELS:
            loc = page.get_by_text(label, exact=False).first
            try:
                if await loc.count() == 0:
                    continue
                try:
                    async with page.expect_navigation(timeout=10_000):
                        await loc.click(force=True, timeout=3_000)
                except Exception:
                    await loc.click(force=True, timeout=3_000)
                await page.wait_for_timeout(1_500)
                return True
            except Exception:
                continue
        return False

    async def _trigger_list_capture(self, page: Page) -> None:
        """Refresh list data without reload/goto (reload of list URL often redirects to setup)."""
        if _SETUP_PATH_HINT in (page.url or ""):
            raise CollectionRejected(
                "TARGET_PAGE_NOT_FOUND",
                "AFTERSALE_SETUP_REQUIRED",
            )
        # Prefer filter/tab that matches MVP quickSearchType=7 semantics.
        for label in ("待商家处理", "待商家处理售后", "全部", "全部售后"):
            loc = page.get_by_text(label, exact=False).first
            try:
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click(force=True, timeout=3_000)
                    await page.wait_for_timeout(1_000)
                    if _SETUP_PATH_HINT in (page.url or ""):
                        raise CollectionRejected(
                            "TARGET_PAGE_NOT_FOUND",
                            "AFTERSALE_SETUP_REQUIRED",
                        )
                    return
            except CollectionRejected:
                raise
            except Exception:
                pass
        # Fallback: click sidebar workbench entry again (in-app navigation, not URL goto).
        if await self._click_workbench_entry(page):
            await page.wait_for_timeout(1_000)
            if _SETUP_PATH_HINT in (page.url or ""):
                raise CollectionRejected(
                    "TARGET_PAGE_NOT_FOUND",
                    "AFTERSALE_SETUP_REQUIRED",
                )
            return
        raise CollectionRejected(
            "DATA_MISMATCH",
            "AFTERSALE_LIST_REFRESH_FAILED",
        )

    async def _merchant_page_identity(self, browser: Browser) -> str:
        page = await select_page_by_url(
            browser,
            self.adapter.identity_page_url or "https://mms.pinduoduo.com/mallcenter/info/basic",
            page_code="MERCHANT_IDENTITY_PAGE",
            auto_open=self.collection.auto_open_missing_pages,
            open_timeout_ms=self.collection.page_open_timeout_ms,
        )
        raw_text = await page.locator("html").text_content()
        if not isinstance(raw_text, str) or not raw_text:
            raise CollectionRejected("IDENTITY_UNVERIFIED", "MERCHANT_IDENTITY_STATE_MISSING")
        matches: set[str] = set()
        for token in re.findall(r"\d{5,20}", raw_text):
            candidate = "".join(ch for ch in token if ch.isdigit())
            if not 5 <= len(candidate) <= 20:
                continue
            if self.connection.expected_platform_store_id:
                if candidate == self.connection.expected_platform_store_id:
                    matches.add(candidate)
            else:
                digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
                if digest == self.connection.expected_platform_store_id_sha256:
                    matches.add(f"sha256:{digest}")
        if len(matches) != 1:
            raise CollectionRejected(
                "IDENTITY_MISMATCH" if matches else "IDENTITY_UNVERIFIED",
                "MERCHANT_PAGE_IDENTITY_NOT_UNIQUE"
                if matches
                else "MERCHANT_PAGE_IDENTITY_MISSING",
            )
        return next(iter(matches))
