from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from playwright.async_api import Browser, Page, Response

from pdd_data_mcp.browser.cdp import CdpConnector, read_locator_value, select_page_by_url
from pdd_data_mcp.browser.core_parsing import (
    ParsedInventoryPage,
    ParsedProductPage,
    ParsedStoreOverview,
    parse_inventory_page_response,
    parse_metric_value,
    parse_product_page_response,
    parse_response_identity,
    parse_store_overview_response,
)
from pdd_data_mcp.browser.parsing import decode_json_object, parse_dom_money
from pdd_data_mcp.browser.promotion import sanitize_discovery_path, sanitized_json_shape
from pdd_data_mcp.browser.store_chart_trend import (
    HOME_PAGE_OVERVIEW_PATH,
    parse_home_page_overview,
)
from pdd_data_mcp.config import (
    CollectionSettings,
    ConnectionSettings,
    CoreAdapterBaseSettings,
    InventoryAdapterSettings,
    ProductCatalogAdapterSettings,
    StoreOverviewAdapterSettings,
)
from pdd_data_mcp.contracts.models import (
    Coverage,
    CoverageStatus,
    DatasetType,
    IdentityEvidence,
    MetricValue,
    MetricWindow,
    ProductCatalogRecord,
    Quality,
    Scope,
    SnapshotDraft,
    StoreChartTrend,
    StoreOverviewPayload,
    WindowKind,
)
from pdd_data_mcp.errors import CollectionRejected
from pdd_data_mcp.security import safe_child
from pdd_data_mcp.utils import canonical_json, scope_key, utc_now

LOGGER = logging.getLogger("pdd_data_mcp.browser")

type CoreAdapter = (
    StoreOverviewAdapterSettings | ProductCatalogAdapterSettings | InventoryAdapterSettings
)
type Trigger = Callable[[], Awaitable[None]]
_ID_CANDIDATE = re.compile(r"(?<!\d)(?:\d[\s\-_.:]*){5,20}(?!\d)")


class CoreDataCdpCollector:
    """Read-only Stage D1/D2/D3 collector selected by dataset type."""

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

    async def collect(
        self,
        *,
        store_id: str,
        dataset_type: DatasetType,
        scope: Scope,
        limit: int,
        batch_id: str | None = None,
    ) -> SnapshotDraft:
        adapter = self._adapter(dataset_type)
        self._validate_request(store_id, dataset_type, scope)
        if not adapter.verified and not adapter.discovery.enabled:
            raise CollectionRejected("ADAPTER_UNVERIFIED", "CORE_ADAPTER_NOT_VERIFIED")
        self._enforce_min_interval(dataset_type)
        requested_at = utc_now()
        started_at = utc_now()
        session = await self.connector.connect(self.connection, self.collection.connect_timeout_ms)
        try:
            page = await select_page_by_url(
                session.browser,
                adapter.target_page_url,
                login_selector=adapter.login_selector,
                captcha_selector=adapter.captcha_selector,
                error_selector=adapter.error_selector,
                page_code=dataset_type.value.upper(),
                auto_open=self.collection.auto_open_missing_pages,
                open_timeout_ms=self.collection.page_open_timeout_ms,
            )
            if not adapter.verified:
                await self._discover(page, adapter, dataset_type)
            merchant_identity: str | None = None
            if adapter.identity_source == "MERCHANT_PAGE_STATE_SHA256":
                merchant_identity = await self._merchant_page_identity(session.browser, adapter)
            if dataset_type is DatasetType.STORE_OVERVIEW:
                assert isinstance(adapter, StoreOverviewAdapterSettings)
                return await self._collect_store(
                    page,
                    adapter,
                    scope,
                    limit,
                    batch_id,
                    requested_at,
                    started_at,
                    merchant_identity,
                )
            if dataset_type is DatasetType.PRODUCT_CATALOG:
                assert isinstance(adapter, ProductCatalogAdapterSettings)
                return await self._collect_products(
                    page,
                    adapter,
                    scope,
                    limit,
                    batch_id,
                    requested_at,
                    started_at,
                    merchant_identity,
                )
            assert isinstance(adapter, InventoryAdapterSettings)
            return await self._collect_inventory(
                page,
                adapter,
                scope,
                limit,
                batch_id,
                requested_at,
                started_at,
                merchant_identity,
            )
        finally:
            await session.disconnect()

    def _adapter(self, dataset_type: DatasetType) -> CoreAdapter:
        if dataset_type is DatasetType.STORE_OVERVIEW:
            return self.connection.store_overview_adapter
        if dataset_type is DatasetType.PRODUCT_CATALOG:
            return self.connection.product_catalog_adapter
        if dataset_type is DatasetType.INVENTORY:
            return self.connection.inventory_adapter
        raise CollectionRejected("DATASET_UNVERIFIED", "REAL_CORE_DATASET_NOT_ADAPTED")

    def _validate_request(self, store_id: str, dataset_type: DatasetType, scope: Scope) -> None:
        if store_id != self.connection.store_id:
            raise CollectionRejected("IDENTITY_MISMATCH", "INTERNAL_STORE_ID_MISMATCH")
        if scope.timezone != "Asia/Shanghai":
            raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "REAL_SCOPE_REQUIRES_ASIA_SHANGHAI")
        today = utc_now().astimezone(ZoneInfo(scope.timezone)).date()
        if scope.business_date != today:
            raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "BUSINESS_DATE_IS_NOT_CURRENT")
        expected = (
            WindowKind.TODAY
            if dataset_type is DatasetType.STORE_OVERVIEW
            else WindowKind.POINT_IN_TIME
        )
        if scope.kind is not expected:
            raise CollectionRejected(
                "TIME_SCOPE_UNVERIFIED", f"{dataset_type.value}_SCOPE_UNSUPPORTED"
            )

    def _enforce_min_interval(self, dataset_type: DatasetType) -> None:
        """Throttle per connection+dataset; lets catalog then inventory sync back to back."""
        directory = safe_child(self.runtime_root, "rate_limits")
        directory.mkdir(parents=True, exist_ok=True)
        path = safe_child(
            directory,
            f"{self.connection.connection_id}__{dataset_type.value}.json",
        )
        now = utc_now()
        if path.exists():
            try:
                previous = datetime.fromisoformat(
                    str(json.loads(path.read_text(encoding="utf-8"))["started_at"])
                )
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                raise CollectionRejected("PLATFORM_ERROR", "INVALID_RATE_LIMIT_STATE") from exc
            elapsed = (now - previous).total_seconds()
            if elapsed < 0 or elapsed < self.collection.min_interval_seconds:
                raise CollectionRejected("BUSY", "MIN_COLLECTION_INTERVAL_NOT_ELAPSED")
        temporary = safe_child(directory, f"tmp_{uuid.uuid4().hex}.json")
        try:
            with temporary.open("xb") as handle:
                handle.write(canonical_json({"started_at": now.isoformat()}) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            raise CollectionRejected("PLATFORM_ERROR", "RATE_LIMIT_STATE_WRITE_FAILED") from exc
        finally:
            temporary.unlink(missing_ok=True)

    async def _merchant_page_identity(
        self, browser: Browser, adapter: CoreAdapterBaseSettings
    ) -> str:
        page = await select_page_by_url(
            browser,
            adapter.identity_page_url,
            page_code="MERCHANT_IDENTITY_PAGE",
            auto_open=self.collection.auto_open_missing_pages,
            open_timeout_ms=self.collection.page_open_timeout_ms,
        )
        text = await page.locator("html").text_content()
        if not isinstance(text, str) or not text:
            raise CollectionRejected("IDENTITY_UNVERIFIED", "MERCHANT_IDENTITY_STATE_MISSING")
        matches: set[str] = set()
        for raw in _ID_CANDIDATE.findall(text):
            candidate = "".join(character for character in raw if character.isdigit())
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

    def _matches(
        self, response: Response, adapter: CoreAdapterBaseSettings, identity: bool
    ) -> bool:
        host = adapter.identity_response_host if identity else adapter.response_host
        path = adapter.identity_response_path if identity else adapter.response_path
        method = adapter.identity_response_method if identity else adapter.response_method
        status = adapter.identity_response_http_status if identity else adapter.response_http_status
        parsed = urlsplit(response.url)
        content_type = response.headers.get("content-type", "").partition(";")[0].casefold()
        return (
            parsed.scheme in {"http", "https"}
            and parsed.hostname is not None
            and parsed.hostname.casefold() == host.casefold()
            and parsed.path == path
            and response.request.method == method
            and response.status == status
            and content_type == "application/json"
        )

    async def _capture_response(
        self, page: Page, adapter: CoreAdapterBaseSettings, trigger: Trigger
    ) -> tuple[bytes, bytes | None, datetime]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[bytes, bytes | None, datetime]] = loop.create_future()
        bodies: dict[str, bytes] = {}
        tasks: set[asyncio.Task[None]] = set()
        active = True

        async def consume(response: Response, kind: Literal["main", "identity"]) -> None:
            if future.done() or not active:
                return
            try:
                raw = await response.body()
                if len(raw) > self.collection.max_browser_response_bytes:
                    raise CollectionRejected("PLATFORM_ERROR", "BROWSER_RESPONSE_TOO_LARGE")
                bodies.setdefault(kind, raw)
                needs_identity = adapter.identity_source == "IDENTITY_RESPONSE"
                if "main" in bodies and (not needs_identity or "identity" in bodies):
                    future.set_result((bodies["main"], bodies.get("identity"), utc_now()))
            except Exception as exc:
                if not future.done() and active:
                    future.set_exception(exc)

        def on_response(response: Response) -> None:
            if not active or future.done() or len(tasks) >= self.collection.max_inflight_responses:
                return
            kind: Literal["main", "identity"] | None = None
            if self._matches(response, adapter, False):
                kind = "main"
            elif adapter.identity_source == "IDENTITY_RESPONSE" and self._matches(
                response, adapter, True
            ):
                kind = "identity"
            if kind is None or kind in bodies:
                return
            task = asyncio.create_task(consume(response, kind))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        page.on("response", on_response)
        try:
            await trigger()
            try:
                return await asyncio.wait_for(
                    future, timeout=self.collection.collection_timeout_ms / 1000
                )
            except TimeoutError as exc:
                raise CollectionRejected("CAPTURE_TIMEOUT", "VERIFIED_RESPONSE_TIMEOUT") from exc
        finally:
            active = False
            page.remove_listener("response", on_response)
            if tasks:
                _, pending = await asyncio.wait(tasks, timeout=2)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

    def _initial_trigger(self, page: Page, adapter: CoreAdapterBaseSettings) -> Trigger:
        async def trigger() -> None:
            if adapter.trigger == "RELOAD":
                await page.reload(
                    wait_until="domcontentloaded",
                    timeout=self.collection.collection_timeout_ms,
                )

        return trigger

    def _next_trigger(self, page: Page, selector: str) -> Trigger:
        async def trigger() -> None:
            locator = page.locator(selector)
            if await locator.count() != 1:
                raise CollectionRejected("DATA_MISMATCH", "NEXT_PAGE_CONTROL_NOT_UNIQUE")
            await locator.click(timeout=self.collection.collection_timeout_ms)

        return trigger

    def _identity(
        self,
        main_raw: bytes,
        identity_raw: bytes | None,
        adapter: CoreAdapterBaseSettings,
        merchant_identity: str | None,
    ) -> str:
        if merchant_identity is not None:
            return merchant_identity
        selected_raw = identity_raw if adapter.identity_source == "IDENTITY_RESPONSE" else main_raw
        if selected_raw is None:
            raise CollectionRejected("IDENTITY_UNVERIFIED", "IDENTITY_RESPONSE_MISSING")
        return parse_response_identity(
            selected_raw,
            adapter,
            expected_store_id=self.connection.expected_platform_store_id,
            expected_store_id_sha256=self.connection.expected_platform_store_id_sha256,
        )

    def _identity_evidence(
        self, adapter: CoreAdapterBaseSettings, observed: str
    ) -> IdentityEvidence:
        response_path: str | None = None
        source: Literal["NETWORK_RESPONSE", "DOM"] = "NETWORK_RESPONSE"
        method: Literal["CONFIG_EXACT_ID", "MERCHANT_PAGE_STATE_SHA256"] = "CONFIG_EXACT_ID"
        if adapter.identity_source == "MERCHANT_PAGE_STATE_SHA256":
            source = "DOM"
            method = "MERCHANT_PAGE_STATE_SHA256"
        elif adapter.identity_source == "IDENTITY_RESPONSE":
            response_path = adapter.identity_platform_store_id_path
        else:
            response_path = adapter.platform_store_id_path
        return IdentityEvidence(
            expected_platform_store_id=(
                self.connection.expected_platform_store_id
                or f"sha256:{self.connection.expected_platform_store_id_sha256}"
            ),
            observed_platform_store_id=observed,
            evidence_source=source,
            independent_verification_method=method,
            independent_verification_reference=adapter.identity_verification_reference,
            response_field_path=response_path,
        )

    async def _collect_store(
        self,
        page: Page,
        adapter: StoreOverviewAdapterSettings,
        scope: Scope,
        limit: int,
        batch_id: str | None,
        requested_at: datetime,
        started_at: datetime,
        merchant_identity: str | None,
    ) -> SnapshotDraft:
        chart_trend: StoreChartTrend | None = None
        if adapter.data_source == "DOM":
            chart_trend = await self._capture_store_chart_trend(
                page, self._initial_trigger(page, adapter)
            )
            captured_at = utc_now()
            if merchant_identity is None:
                raise CollectionRejected("IDENTITY_UNVERIFIED", "DOM_STORE_IDENTITY_MISSING")
            identity = merchant_identity
            parsed = ParsedStoreOverview(
                metrics={name: None for name in adapter.metrics},
                business_date=scope.business_date,
            )
        else:
            main_raw, identity_raw, captured_at = await self._capture_response(
                page, adapter, self._initial_trigger(page, adapter)
            )
            identity = self._identity(main_raw, identity_raw, adapter, merchant_identity)
            parsed = parse_store_overview_response(
                main_raw,
                adapter,
                expected_business_date=scope.business_date,
                observed_at=captured_at,
            )
            chart_trend = parse_home_page_overview(main_raw)
        (
            metrics,
            sources,
            capture_method,
            source_updated_at,
        ) = await self._read_and_crosscheck_store_dom(page, adapter, parsed, scope, captured_at)
        store_name = await self._read_store_name(page, adapter)
        if store_name:
            sources["store_name"] = "DOM"
        if chart_trend is not None:
            sources["chart_trend"] = "NETWORK_RESPONSE"
            if capture_method == "DOM":
                capture_method = "MIXED"
        window_end = source_updated_at or captured_at
        if window_end > captured_at + timedelta(minutes=5):
            raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "SOURCE_UPDATE_TIME_IN_FUTURE")
        missing = [name for name, value in metrics.items() if value is None]
        zone = ZoneInfo(scope.timezone)
        midnight = datetime.combine(scope.business_date, time.min, tzinfo=zone)
        return SnapshotDraft(
            batch_id=batch_id,
            store_id=self.connection.store_id,
            dataset_type=DatasetType.STORE_OVERVIEW,
            scope=scope,
            scope_key=scope_key(scope),
            requested_at=requested_at,
            capture_started_at=started_at,
            capture_finished_at=utc_now(),
            captured_at=captured_at,
            metric_window=MetricWindow(
                kind=WindowKind.TODAY,
                timezone=scope.timezone,
                start=midnight,
                end=window_end,
                window_complete=False,
                source_finalized=False,
            ),
            source_updated_at=source_updated_at,
            source="PDD_BROWSER_CDP",
            capture_method=capture_method,
            parser_version=adapter.parser_version,
            identity_evidence=self._identity_evidence(adapter, identity),
            field_sources=sources,
            payload=StoreOverviewPayload(
                metrics=metrics, store_name=store_name, chart_trend=chart_trend
            ).model_dump(mode="json"),
            missing_fields=missing,
            quality=Quality(
                status="VALID",
                identity="MATCHED",
                coverage=CoverageStatus.COMPLETE,
                dom_check="MATCHED",
            ),
            coverage=Coverage(
                total_observed=1,
                captured=1,
                limit=limit,
                pages_read=1,
                coverage=CoverageStatus.COMPLETE,
                truncated=False,
            ),
        )

    async def _capture_store_chart_trend(
        self, page: Page, trigger: Trigger
    ) -> StoreChartTrend | None:
        """Best-effort capture of MMS home realtime trend; never fails the store collect."""
        bodies: list[bytes] = []
        tasks: set[asyncio.Task[None]] = set()

        def _match(response: Response) -> bool:
            parsed = urlsplit(response.url)
            host = (parsed.hostname or "").casefold()
            return (
                "mms.pinduoduo.com" in host
                and parsed.path == HOME_PAGE_OVERVIEW_PATH
                and response.request.method == "POST"
                and response.status == 200
            )

        async def consume(response: Response) -> None:
            if not _match(response):
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

        page.on("response", on_response)
        try:
            # Realtime panel lazy-init is gated on tab visibility/rAF; in a background
            # tab the panel never initializes and clicking refresh fires no XHR.
            # 试验：暂不抢前台，观察后台标签页能否正常采到实时趋势。
            # 若 bodies 一直为空 / 趋势采不到，再恢复下面的 bring_to_front。
            # with suppress(Exception):
            #     await page.bring_to_front()
            await trigger()
            with suppress(Exception):
                await page.wait_for_load_state(
                    "networkidle",
                    timeout=min(12_000, self.collection.collection_timeout_ms),
                )
            # Realtime panel XHR usually appears only after clicking refresh
            with suppress(Exception):
                await page.evaluate(
                    """() => {
                      const el = [...document.querySelectorAll('*')].find((node) =>
                        (node.innerText || '').includes('实时数据更新时间')
                      );
                      if (el) el.scrollIntoView({ block: 'center' });
                    }"""
                )
            # First click can land while the panel still initializes; click at most
            # twice, allowing a 4s response window after each click.
            loop = asyncio.get_running_loop()
            timeout_seconds = min(12.0, max(6.0, self.collection.collection_timeout_ms / 1000))
            deadline = loop.time() + timeout_seconds
            clicks = 0
            while loop.time() < deadline and not bodies:
                refresh = page.locator('[data-tracking-click-viewid="el_refresh_button"]').first
                if clicks < 2:
                    try:
                        if await refresh.count() > 0:
                            await refresh.click(force=True, timeout=5_000)
                            clicks += 1
                    except Exception:
                        pass
                wait_until = min(deadline, loop.time() + 4.0)
                while loop.time() < wait_until and not bodies:
                    await asyncio.sleep(0.25)
            if tasks:
                await asyncio.wait(tasks, timeout=2)
        except Exception:
            return None
        finally:
            page.remove_listener("response", on_response)
            for task in list(tasks):
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        for raw in reversed(bodies):
            trend = parse_home_page_overview(raw)
            if trend is not None:
                return trend
        if bodies:
            LOGGER.info("store_chart_trend: %s bodies captured but none parsed", len(bodies))
        else:
            LOGGER.info("store_chart_trend: homePageOverView response not observed")
        return None

    async def _read_store_name(
        self, page: Page, adapter: StoreOverviewAdapterSettings
    ) -> str | None:
        """Best-effort mall display name from MMS chrome; never fails the whole collect."""
        selector = (adapter.dom_store_name_selector or "").strip()
        if not selector:
            return None
        try:
            text = await read_locator_value(
                page,
                selector,
                adapter.dom_store_name_attribute,
                wait_timeout_ms=min(5_000, self.collection.collection_timeout_ms),
                failure_status="DATA_MISMATCH",
                failure_code="DOM_STORE_NAME_MISSING",
            )
        except CollectionRejected:
            return None
        cleaned = " ".join(text.split()).strip()
        if not cleaned or cleaned in {"主账号", "子账号", "商家后台"}:
            return None
        # Header sometimes is "店名 主账号"; prefer the name leaf only.
        if cleaned.endswith(" 主账号"):
            cleaned = cleaned[: -len(" 主账号")].strip()
        return cleaned[:64] or None

    async def _read_and_crosscheck_store_dom(
        self,
        page: Page,
        adapter: StoreOverviewAdapterSettings,
        parsed: ParsedStoreOverview,
        scope: Scope,
        observed_at: datetime,
    ) -> tuple[
        dict[str, MetricValue | None],
        dict[str, Literal["NETWORK_RESPONSE", "DOM"]],
        Literal["NETWORK_RESPONSE", "DOM", "MIXED"],
        datetime | None,
    ]:
        dom_date = await read_locator_value(
            page,
            adapter.dom_business_date_selector,
            adapter.dom_business_date_attribute,
            wait_timeout_ms=min(10_000, self.collection.collection_timeout_ms),
            failure_status="TIME_SCOPE_UNVERIFIED",
            failure_code="DOM_BUSINESS_DATE_MISSING",
        )
        source_updated_at: datetime | None = None
        if adapter.dom_business_date_format == "CONTAINS_ISO_DATETIME_SECONDS":
            timestamps = re.findall(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", dom_date)
            if len(timestamps) != 1 or (
                adapter.dom_today_label and adapter.dom_today_label not in dom_date
            ):
                raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "DOM_BUSINESS_DATE_MISMATCH")
            source_updated_at = datetime.strptime(timestamps[0], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=ZoneInfo(scope.timezone)
            )
            if source_updated_at.date() != scope.business_date:
                raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "DOM_BUSINESS_DATE_MISMATCH")
        elif dom_date != (adapter.dom_today_label or scope.business_date.isoformat()):
            raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "DOM_BUSINESS_DATE_MISMATCH")
        metrics = dict(parsed.metrics)
        sources: dict[str, Literal["NETWORK_RESPONSE", "DOM"]] = {}
        checked = 0
        for name, mapping in adapter.metrics.items():
            metric = metrics.get(name)
            if metric is not None:
                if source_updated_at is not None:
                    metric = metric.model_copy(update={"source_updated_at": source_updated_at})
                    metrics[name] = metric
                sources[f"metrics.{name}"] = "NETWORK_RESPONSE"
            if not mapping.dom_selector:
                continue
            text = await read_locator_value(
                page,
                mapping.dom_selector,
                mapping.dom_attribute,
                wait_timeout_ms=min(10_000, self.collection.collection_timeout_ms),
                failure_status="DATA_MISMATCH",
                failure_code="DOM_STORE_METRIC_MISSING",
            )
            normalized_text = text.strip().replace(",", "")
            if mapping.source_unit == "PERCENT":
                normalized_text = normalized_text.removesuffix("%")
            if mapping.source_unit in {"CNY", "CNY_CENT"}:
                dom_money = parse_dom_money(text, mapping.source_unit)
                dom_value: int | str = dom_money.displayed_cents
                dom_precision = dom_money.precision
                matches = metric is None or (
                    isinstance(metric.value, int) and dom_money.matches(metric.value)
                )
            else:
                dom_value = parse_metric_value(normalized_text, mapping)
                dom_precision = mapping.precision
                matches = metric is None or dom_value == metric.value
            if not matches:
                raise CollectionRejected("DATA_MISMATCH", "NETWORK_DOM_STORE_METRIC_MISMATCH")
            if metric is None:
                metrics[name] = MetricValue(
                    value=dom_value,
                    unit=mapping.output_unit,
                    observed_at=observed_at,
                    source_updated_at=source_updated_at,
                    capture_method="DOM",
                    precision=dom_precision,
                )
                sources[f"metrics.{name}"] = "DOM"
            checked += 1
        if checked < 2:
            raise CollectionRejected("DATA_MISMATCH", "INSUFFICIENT_STORE_DOM_CROSSCHECKS")
        if sum(value is not None for value in metrics.values()) < 2:
            raise CollectionRejected("DATA_MISMATCH", "INSUFFICIENT_STABLE_STORE_METRICS")
        unique_sources = set(sources.values())
        capture_method: Literal["NETWORK_RESPONSE", "DOM", "MIXED"] = (
            "MIXED"
            if len(unique_sources) > 1
            else "DOM"
            if unique_sources == {"DOM"}
            else "NETWORK_RESPONSE"
        )
        return metrics, sources, capture_method, source_updated_at

    async def _collect_products(
        self,
        page: Page,
        adapter: ProductCatalogAdapterSettings,
        scope: Scope,
        limit: int,
        batch_id: str | None,
        requested_at: datetime,
        started_at: datetime,
        merchant_identity: str | None,
    ) -> SnapshotDraft:
        records: list[ProductCatalogRecord] = []
        total: int | None = None
        identity: str | None = merchant_identity
        pages_read = 0
        trigger = self._initial_trigger(page, adapter)
        captured_at = started_at
        while pages_read < 100:
            main_raw, identity_raw, captured_at = await self._capture_response(
                page, adapter, trigger
            )
            if identity is None:
                identity = self._identity(main_raw, identity_raw, adapter, None)
            parsed: ParsedProductPage = parse_product_page_response(
                main_raw,
                adapter,
                observed_at=captured_at,
                expected_store_id=self.connection.expected_platform_store_id,
                expected_store_id_sha256=self.connection.expected_platform_store_id_sha256,
            )
            pages_read += 1
            if total is None:
                total = parsed.total_observed
            elif total != parsed.total_observed:
                raise CollectionRejected("DATA_MISMATCH", "PRODUCT_TOTAL_CHANGED_DURING_CAPTURE")
            incoming_ids = [record.product_id for record in parsed.records]
            existing_ids = {record.product_id for record in records}
            if existing_ids.intersection(incoming_ids):
                raise CollectionRejected("DATA_MISMATCH", "DUPLICATE_PRODUCT_ACROSS_PAGES")
            remaining = max(0, limit - len(records))
            records.extend(parsed.records[:remaining])
            if total <= len(records) or len(records) >= limit:
                break
            if not adapter.next_page_selector or not parsed.records:
                break
            trigger = self._next_trigger(page, adapter.next_page_selector)
        assert total is not None and identity is not None
        await self._crosscheck_total(
            page, adapter.dom_total_selector, adapter.dom_total_attribute, total
        )
        coverage, truncated, stop_reason = self._coverage(total, len(records), limit)
        return SnapshotDraft(
            batch_id=batch_id,
            store_id=self.connection.store_id,
            dataset_type=DatasetType.PRODUCT_CATALOG,
            scope=scope,
            scope_key=scope_key(scope),
            requested_at=requested_at,
            capture_started_at=started_at,
            capture_finished_at=utc_now(),
            captured_at=captured_at,
            source="PDD_BROWSER_CDP",
            capture_method="NETWORK_RESPONSE",
            parser_version=adapter.parser_version,
            identity_evidence=self._identity_evidence(adapter, identity),
            field_sources={"records.*": "NETWORK_RESPONSE"},
            payload=[record.model_dump(mode="json") for record in records],
            missing_fields=[],
            quality=Quality(
                status="VALID", identity="MATCHED", coverage=coverage, dom_check="MATCHED"
            ),
            coverage=Coverage(
                total_observed=total,
                captured=len(records),
                limit=limit,
                pages_read=pages_read,
                coverage=coverage,
                truncated=truncated,
                stop_reason=stop_reason,
            ),
        )

    async def _collect_inventory(
        self,
        page: Page,
        adapter: InventoryAdapterSettings,
        scope: Scope,
        limit: int,
        batch_id: str | None,
        requested_at: datetime,
        started_at: datetime,
        merchant_identity: str | None,
    ) -> SnapshotDraft:
        main_raw, identity_raw, captured_at = await self._capture_response(
            page, adapter, self._initial_trigger(page, adapter)
        )
        identity = self._identity(main_raw, identity_raw, adapter, merchant_identity)
        parsed: ParsedInventoryPage = parse_inventory_page_response(
            main_raw,
            adapter,
            observed_at=captured_at,
            expected_store_id=self.connection.expected_platform_store_id,
            expected_store_id_sha256=self.connection.expected_platform_store_id_sha256,
        )
        await self._crosscheck_total(
            page, adapter.dom_total_selector, adapter.dom_total_attribute, parsed.total_observed
        )
        selected = parsed.records[:limit]
        coverage, truncated, stop_reason = self._coverage(
            parsed.total_observed, len(selected), limit
        )
        missing = [
            f"records[{index}].inventory"
            for index, record in enumerate(selected)
            if record.inventory is None
        ]
        return SnapshotDraft(
            batch_id=batch_id,
            store_id=self.connection.store_id,
            dataset_type=DatasetType.INVENTORY,
            scope=scope,
            scope_key=scope_key(scope),
            requested_at=requested_at,
            capture_started_at=started_at,
            capture_finished_at=utc_now(),
            captured_at=captured_at,
            source="PDD_BROWSER_CDP",
            capture_method="NETWORK_RESPONSE",
            parser_version=adapter.parser_version,
            identity_evidence=self._identity_evidence(adapter, identity),
            field_sources={"records.*": "NETWORK_RESPONSE"},
            payload=[record.model_dump(mode="json") for record in selected],
            missing_fields=missing,
            quality=Quality(
                status="VALID", identity="MATCHED", coverage=coverage, dom_check="MATCHED"
            ),
            coverage=Coverage(
                total_observed=parsed.total_observed,
                captured=len(selected),
                limit=limit,
                pages_read=1,
                coverage=coverage,
                truncated=truncated,
                stop_reason=stop_reason,
            ),
        )

    def _coverage(
        self, total: int, captured: int, limit: int
    ) -> tuple[CoverageStatus, bool, str | None]:
        if captured == total:
            return CoverageStatus.COMPLETE, False, None
        if total > limit and captured == limit:
            return CoverageStatus.TRUNCATED, True, "CAPTURE_LIMIT_REACHED"
        return CoverageStatus.PARTIAL, False, "SOURCE_ROWS_MISSING"

    async def _crosscheck_total(
        self, page: Page, selector: str, attribute: str, expected: int
    ) -> None:
        text = await read_locator_value(
            page,
            selector,
            attribute,
            wait_timeout_ms=min(10_000, self.collection.collection_timeout_ms),
            failure_status="DATA_MISMATCH",
            failure_code="DOM_TOTAL_MISSING",
        )
        numbers = re.findall(r"\d[\d,]*", text)
        if len(numbers) != 1 or int(numbers[0].replace(",", "")) != expected:
            raise CollectionRejected("DATA_MISMATCH", "NETWORK_DOM_TOTAL_MISMATCH")

    async def _discover(
        self, page: Page, adapter: CoreAdapterBaseSettings, dataset_type: DatasetType
    ) -> None:
        discovery = adapter.discovery
        if not discovery.enabled:
            raise CollectionRejected("ADAPTER_UNVERIFIED", "CORE_ADAPTER_NOT_VERIFIED")
        metadata: list[dict[str, object]] = []
        shapes: dict[str, dict[str, object]] = {}
        seen: set[tuple[object, ...]] = set()
        tasks: set[asyncio.Task[None]] = set()
        probed: set[str] = set()
        active = True

        async def probe(response: Response, path: str) -> None:
            try:
                raw = await response.body()
                if len(raw) > self.collection.max_browser_response_bytes:
                    shapes[path] = {"status": "RESPONSE_TOO_LARGE", "shape": []}
                    return
                shapes[path] = {
                    "status": "PARSED_IN_MEMORY",
                    "shape": sanitized_json_shape(decode_json_object(raw)),
                }
            except (CollectionRejected, OSError, ValueError):
                shapes[path] = {"status": "PARSE_FAILED", "shape": []}

        def on_response(response: Response) -> None:
            if not active:
                return
            parsed = urlsplit(response.url)
            if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
                return
            content_type = response.headers.get("content-type", "").partition(";")[0].casefold()
            item: dict[str, object] = {
                "host": parsed.hostname.casefold(),
                "path": sanitize_discovery_path(parsed.path),
                "method": response.request.method,
                "status": response.status,
                "content_type": content_type,
            }
            signature = tuple(item.values())
            if signature not in seen and len(metadata) < discovery.max_metadata_entries:
                seen.add(signature)
                metadata.append(item)
            if (
                discovery.candidate_body_probe_enabled
                and parsed.hostname.casefold() == discovery.candidate_response_host.casefold()
                and parsed.path in discovery.candidate_response_paths
                and response.request.method == discovery.candidate_response_method
                and response.status == 200
                and content_type == "application/json"
                and parsed.path not in probed
                and len(tasks) < self.collection.max_inflight_responses
            ):
                probed.add(parsed.path)
                task = asyncio.create_task(probe(response, parsed.path))
                tasks.add(task)
                task.add_done_callback(tasks.discard)

        page.on("response", on_response)
        try:
            if adapter.trigger == "RELOAD":
                await page.reload(
                    wait_until="domcontentloaded",
                    timeout=self.collection.collection_timeout_ms,
                )
            await asyncio.sleep(discovery.observe_seconds)
        finally:
            active = False
            page.remove_listener("response", on_response)
            if tasks:
                _, pending = await asyncio.wait(tasks, timeout=2)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        directory = safe_child(
            self.runtime_root, "discovery", self.connection.connection_id, dataset_type.value
        )
        directory.mkdir(parents=True, exist_ok=True)
        now = utc_now()
        name = f"{now:%Y%m%dT%H%M%S}_{uuid.uuid4().hex}.json"
        destination = safe_child(directory, name)
        temporary = safe_child(directory, f"tmp_{uuid.uuid4().hex}.json")
        content = canonical_json(
            {
                "connection_id": self.connection.connection_id,
                "dataset_type": dataset_type.value,
                "captured_at": now.isoformat(),
                "entries": metadata,
                "candidate_shapes": shapes,
                "raw_bodies_persisted": False,
                "candidate_values_persisted": False,
            }
        )
        try:
            with temporary.open("xb") as handle:
                handle.write(content + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        raise CollectionRejected(
            "ADAPTER_UNVERIFIED",
            "DISCOVERY_METADATA_RECORDED",
            f"DISCOVERY_ENTRIES:{len(metadata)}",
            f"DISCOVERY_REPORT:{name}",
        )
