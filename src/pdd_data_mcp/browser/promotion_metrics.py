from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from playwright.async_api import Browser, Error, Locator, Page, Response

from pdd_data_mcp.browser.cdp import CdpConnector, select_page_by_url
from pdd_data_mcp.browser.core_parsing import parse_response_identity
from pdd_data_mcp.browser.parsing import decode_json_object, read_object_path
from pdd_data_mcp.browser.promotion_metrics_parsing import (
    ParsedPromotedProductResponse,
    parse_promoted_product_response,
)
from pdd_data_mcp.config import (
    CollectionSettings,
    ConnectionSettings,
    PromotionMetricsAdapterSettings,
    PromotionWindowName,
)
from pdd_data_mcp.contracts.models import (
    Coverage,
    CoverageStatus,
    DatasetType,
    IdentityEvidence,
    MetricWindow,
    Quality,
    Scope,
    SnapshotDraft,
    WindowKind,
)
from pdd_data_mcp.errors import CollectionRejected
from pdd_data_mcp.security import safe_child
from pdd_data_mcp.utils import canonical_json, scope_key, utc_now

Trigger = Callable[[], Awaitable[None]]

_TARGET_PAGE_URL = "https://yingxiao.pinduoduo.com/goods/promotion/list"
_MAIN_HOST = "yingxiao.pinduoduo.com"
_MAIN_PATH = "/mms-gateway/venus/api/goods/promotion/v3/list"
_IDENTITY_HOST = "yingxiao.pinduoduo.com"
_IDENTITY_PATH = "/mms-gateway/venus/api/user/userInfo"
_REQUEST_CONTRACT_VERSION = "PROMOTED_PRODUCT_LIST_UNFILTERED_V1"
_REQUEST_KEYS = frozenset(
    {
        "beginDate",
        "blockType",
        "clientType",
        "crawlerInfo",
        "endDate",
        "filter",
        "orderBy",
        "pageNumber",
        "pageSize",
        "scenesMode",
        "showGoodsPromotionHistoryReport",
        "sortBy",
        "withTagsInfo",
    }
)
_REQUEST_CONSTANTS: dict[str, int | bool] = {
    "blockType": 3,
    "clientType": 1,
    "orderBy": 9999,
    "scenesMode": 1,
    "showGoodsPromotionHistoryReport": False,
    "sortBy": 9999,
    "withTagsInfo": True,
}
_EVIDENCED_WINDOW_TESTIDS: dict[PromotionWindowName, str] = {
    "TODAY": "DateAreaQuickOption_0",
}

_SUPPORTED_DATASETS = frozenset(
    {
        DatasetType.PRODUCT_METRICS,
        DatasetType.PROMOTION_CONFIGURATION,
    }
)
_EVIDENCE_PATHS = {
    "business_success_path": "success",
    "identity_business_success_path": "success",
    "identity_platform_store_id_path": "result.mall.mallId",
    "list_path": "result.adInfos",
    "summary_path": "result.sumReportInfo",
    "source_updated_at_path": "result.reportLastUpdateTime",
    "row_platform_store_id_path": "mallId",
    "promotion_id_path": "adId",
    "campaign_id_path": "planId",
    "platform_product_id_path": "goodsId",
    "product_name_path": "goodsInfo.goodsName",
    "report_path": "reportInfo",
    "max_cost_path": "maxCost",
    "target_roi_path": "targetRoi",
    "agent_bid_path": "agentBid",
    "ad_status_path": "adStatus",
    "request_begin_date_field": "beginDate",
    "request_end_date_field": "endDate",
    "request_page_number_field": "pageNumber",
    "request_page_size_field": "pageSize",
}


@dataclass(frozen=True)
class _ExpectedWindow:
    begin_date: datetime
    end_date: datetime
    window_complete: bool

    @property
    def request_begin_date(self) -> str:
        return self.begin_date.date().isoformat()

    @property
    def request_end_date(self) -> str:
        return self.end_date.date().isoformat()


@dataclass(frozen=True)
class _Captured:
    main_raw: bytes
    main_response: Response
    identity_raw: bytes | None
    captured_at: datetime


@dataclass(frozen=True)
class _ParsedCapture:
    response: ParsedPromotedProductResponse
    identity: str
    captured_at: datetime


class PromotionMetricsCdpCollector:
    """Evidence-bound read-only collector for D4 promoted-product data."""

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
        requested_at = utc_now()
        adapter = self.connection.promotion_metrics_adapter
        current_date = utc_now().astimezone(ZoneInfo("Asia/Shanghai")).date()
        requested_window = self._validate_request(
            store_id, dataset_type, scope, adapter, current_date
        )
        self._enforce_min_interval()
        started_at = utc_now()
        session = await self.connector.connect(self.connection, self.collection.connect_timeout_ms)
        page: Page | None = None
        restore_required = False
        try:
            page = await self._select_safe_page(session.browser, adapter)
            today_window = self._window_for_kind(WindowKind.TODAY, current_date, scope.timezone)
            baseline = await self._capture(page, adapter, self._reload(page), require_identity=True)
            self._validate_capture_date(baseline.captured_at, current_date, scope.timezone)
            page = await self._select_safe_page(session.browser, adapter)
            self._validate_response_request(baseline.main_response, adapter, today_window)
            parsed = self._parse_capture(baseline, adapter)
            await self._crosscheck_dom(page, baseline.main_raw, adapter)

            if (
                dataset_type is not DatasetType.PROMOTION_CONFIGURATION
                and scope.kind is not WindowKind.TODAY
            ):
                restore_required = True
                option_name = cast(PromotionWindowName, scope.kind.value)
                test_id = adapter.quick_option_testids.get(option_name)
                if test_id is None:
                    raise CollectionRejected(
                        "TIME_SCOPE_UNVERIFIED", "PROMOTION_WINDOW_OPTION_NOT_CONFIGURED"
                    )
                selected = await self._capture(
                    page,
                    adapter,
                    self._click_quick_option(page, test_id),
                    require_identity=False,
                )
                self._validate_capture_date(selected.captured_at, current_date, scope.timezone)
                page = await self._select_safe_page(session.browser, adapter)
                assert requested_window is not None
                self._validate_response_request(selected.main_response, adapter, requested_window)
                selected_response = parse_promoted_product_response(
                    selected.main_raw,
                    observed_at=selected.captured_at,
                    expected_store_id=self.connection.expected_platform_store_id,
                    expected_store_id_sha256=self.connection.expected_platform_store_id_sha256,
                )
                self._match_row_and_independent_identity(selected_response, parsed.identity)
                await self._crosscheck_dom(page, selected.main_raw, adapter)
                parsed = _ParsedCapture(
                    response=selected_response,
                    identity=parsed.identity,
                    captured_at=selected.captured_at,
                )

            return self._build_draft(
                requested_at=requested_at,
                started_at=started_at,
                store_id=store_id,
                dataset_type=dataset_type,
                scope=scope,
                limit=limit,
                batch_id=batch_id,
                adapter=adapter,
                parsed=parsed,
                requested_window=requested_window,
            )
        finally:
            try:
                if restore_required and page is not None:
                    page = await self._select_safe_page(session.browser, adapter)
                    restored = await self._capture(
                        page, adapter, self._reload(page), require_identity=True
                    )
                    self._validate_capture_date(restored.captured_at, current_date, scope.timezone)
                    today_window = self._window_for_kind(
                        WindowKind.TODAY, current_date, scope.timezone
                    )
                    self._validate_response_request(restored.main_response, adapter, today_window)
                    self._parse_capture(restored, adapter)
                    await self._crosscheck_dom(page, restored.main_raw, adapter)
                    await self._select_safe_page(session.browser, adapter)
            finally:
                await session.disconnect()

    def _validate_request(
        self,
        store_id: str,
        dataset_type: DatasetType,
        scope: Scope,
        adapter: PromotionMetricsAdapterSettings,
        current_date: date,
    ) -> _ExpectedWindow | None:
        if dataset_type not in _SUPPORTED_DATASETS:
            raise CollectionRejected("DATASET_UNVERIFIED", "PROMOTION_DATASET_NOT_ADAPTED")
        if not adapter.verified:
            raise CollectionRejected("ADAPTER_UNVERIFIED", "PROMOTION_METRICS_ADAPTER_NOT_VERIFIED")
        self._validate_evidence_adapter(adapter)
        if store_id != self.connection.store_id:
            raise CollectionRejected("IDENTITY_MISMATCH", "INTERNAL_STORE_ID_MISMATCH")
        if scope.timezone != "Asia/Shanghai":
            raise CollectionRejected(
                "TIME_SCOPE_UNVERIFIED", "PROMOTION_SCOPE_REQUIRES_ASIA_SHANGHAI"
            )
        if (
            scope.object_type != "PROMOTED_PRODUCT_ALL"
            or scope.filters
            or scope.currency != "CNY"
            or scope.attribution != "PLATFORM_DEFAULT"
        ):
            raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "PROMOTION_SCOPE_NOT_ADAPTED")
        if dataset_type is DatasetType.PROMOTION_CONFIGURATION:
            if scope.kind is not WindowKind.POINT_IN_TIME or scope.business_date != current_date:
                raise CollectionRejected(
                    "TIME_SCOPE_UNVERIFIED", "PROMOTION_CONFIGURATION_REQUIRES_CURRENT_POINT"
                )
            if "TODAY" not in adapter.supported_windows:
                raise CollectionRejected(
                    "TIME_SCOPE_UNVERIFIED", "PROMOTION_TODAY_WINDOW_NOT_SUPPORTED"
                )
            return None
        if scope.kind.value not in adapter.supported_windows:
            raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "PROMOTION_WINDOW_NOT_SUPPORTED")
        window = self._window_for_kind(scope.kind, current_date, scope.timezone)
        expected_business_date = (
            current_date - timedelta(days=1) if scope.kind is WindowKind.YESTERDAY else current_date
        )
        if scope.business_date != expected_business_date:
            raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "PROMOTION_BUSINESS_DATE_MISMATCH")
        return window

    def _validate_evidence_adapter(self, adapter: PromotionMetricsAdapterSettings) -> None:
        if (
            adapter.identity_source != "IDENTITY_RESPONSE"
            or adapter.trigger != "RELOAD"
            or adapter.business_success_value is not True
            or adapter.identity_business_success_value is not True
            or not adapter.configuration_current_only
            or not adapter.reload_resets_to_today
        ):
            raise CollectionRejected("ADAPTER_UNVERIFIED", "PROMOTION_IDENTITY_GATE_NOT_ADAPTED")
        if (
            adapter.request_contract_version != _REQUEST_CONTRACT_VERSION
            or adapter.platform_page_size != 50
        ):
            raise CollectionRejected("ADAPTER_UNVERIFIED", "PROMOTION_REQUEST_CONTRACT_NOT_ADAPTED")
        supported_windows = set(adapter.supported_windows)
        if not supported_windows or not supported_windows <= set(_EVIDENCED_WINDOW_TESTIDS):
            raise CollectionRejected("ADAPTER_UNVERIFIED", "PROMOTION_WINDOWS_NOT_EVIDENCED")
        expected_testids = {
            window: _EVIDENCED_WINDOW_TESTIDS[window] for window in adapter.supported_windows
        }
        if adapter.quick_option_testids != expected_testids:
            raise CollectionRejected("ADAPTER_UNVERIFIED", "PROMOTION_WINDOW_TESTIDS_MISMATCH")
        endpoint_values = {
            "target_page_url": (adapter.target_page_url, _TARGET_PAGE_URL),
            "response_host": (adapter.response_host, _MAIN_HOST),
            "response_path": (adapter.response_path, _MAIN_PATH),
            "response_method": (adapter.response_method, "POST"),
            "response_http_status": (adapter.response_http_status, 200),
            "identity_response_host": (adapter.identity_response_host, _IDENTITY_HOST),
            "identity_response_path": (adapter.identity_response_path, _IDENTITY_PATH),
            "identity_response_method": (adapter.identity_response_method, "POST"),
            "identity_response_http_status": (adapter.identity_response_http_status, 200),
            "dom_row_selector": (adapter.dom_row_selector, "tbody tr"),
        }
        for field, (actual, expected) in endpoint_values.items():
            if actual != expected:
                raise CollectionRejected(
                    "ADAPTER_UNVERIFIED", f"PROMOTION_EVIDENCE_ENDPOINT_MISMATCH:{field}"
                )
        for field, expected in _EVIDENCE_PATHS.items():
            if getattr(adapter, field) != expected:
                raise CollectionRejected(
                    "ADAPTER_UNVERIFIED", f"PROMOTION_EVIDENCE_PATH_MISMATCH:{field}"
                )

    def _window_for_kind(
        self, kind: WindowKind, current_date: date, timezone: str
    ) -> _ExpectedWindow:
        zone = ZoneInfo(timezone)
        current_midnight = datetime.combine(current_date, time.min, tzinfo=zone)
        if kind is WindowKind.TODAY:
            return _ExpectedWindow(current_midnight, current_midnight, False)
        if kind is WindowKind.YESTERDAY:
            yesterday = current_midnight - timedelta(days=1)
            return _ExpectedWindow(yesterday, yesterday, True)
        days = {
            WindowKind.LAST_7_DAYS: 7,
            WindowKind.LAST_30_DAYS: 30,
            WindowKind.LAST_90_DAYS: 90,
        }.get(kind)
        if days is None:
            raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "PROMOTION_WINDOW_NOT_ADAPTED")
        return _ExpectedWindow(current_midnight - timedelta(days=days - 1), current_midnight, False)

    async def _select_safe_page(
        self, browser: Browser, adapter: PromotionMetricsAdapterSettings
    ) -> Page:
        page = await select_page_by_url(
            browser,
            adapter.target_page_url,
            login_selector=adapter.login_selector,
            captcha_selector=adapter.captcha_selector,
            error_selector=adapter.error_selector,
            page_code="PROMOTION_METRICS_PAGE",
        )
        parsed = urlsplit(page.url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != _MAIN_HOST
            or parsed.port is not None
            or parsed.path != "/goods/promotion/list"
            or parsed.query
            or parsed.fragment
        ):
            raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "PROMOTION_PAGE_SCOPE_UNVERIFIED")
        return page

    def _reload(self, page: Page) -> Trigger:
        async def trigger() -> None:
            try:
                await page.reload(
                    wait_until="domcontentloaded",
                    timeout=self.collection.collection_timeout_ms,
                )
            except Error as exc:
                raise CollectionRejected("CAPTURE_TIMEOUT", "PROMOTION_RELOAD_FAILED") from exc

        return trigger

    def _click_quick_option(self, page: Page, test_id: str) -> Trigger:
        async def trigger() -> None:
            selector = f'[data-testid="{test_id}"]'
            locator: Locator = page.locator(selector)
            if await locator.count() != 1:
                raise CollectionRejected("DATA_MISMATCH", "WINDOW_OPTION_NOT_UNIQUE")
            if not await locator.is_visible() or not await locator.is_enabled():
                raise CollectionRejected("DATA_MISMATCH", "WINDOW_OPTION_NOT_INTERACTIVE")
            try:
                await locator.click(timeout=self.collection.collection_timeout_ms)
            except Error as exc:
                raise CollectionRejected("CAPTURE_TIMEOUT", "WINDOW_OPTION_CLICK_FAILED") from exc

        return trigger

    def _matches(
        self, response: Response, adapter: PromotionMetricsAdapterSettings, *, identity: bool
    ) -> bool:
        host = adapter.identity_response_host if identity else adapter.response_host
        path = adapter.identity_response_path if identity else adapter.response_path
        method = adapter.identity_response_method if identity else adapter.response_method
        status = adapter.identity_response_http_status if identity else adapter.response_http_status
        parsed = urlsplit(response.url)
        content_type = response.headers.get("content-type", "").partition(";")[0].casefold()
        return (
            parsed.scheme == "https"
            and parsed.hostname is not None
            and parsed.hostname.casefold() == host.casefold()
            and parsed.port is None
            and parsed.path == path
            and not parsed.query
            and not parsed.fragment
            and response.request.method == method
            and response.status == status
            and content_type == "application/json"
        )

    async def _capture(
        self,
        page: Page,
        adapter: PromotionMetricsAdapterSettings,
        trigger: Trigger,
        *,
        require_identity: bool,
    ) -> _Captured:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[_Captured] = loop.create_future()
        bodies: dict[str, bytes] = {}
        responses: dict[str, Response] = {}
        scheduled: set[str] = set()
        tasks: set[asyncio.Task[None]] = set()
        active = True

        async def consume(response: Response, kind: Literal["main", "identity"]) -> None:
            if future.done() or not active:
                return
            try:
                content_length = response.headers.get("content-length")
                if (
                    content_length is not None
                    and int(content_length) > self.collection.max_browser_response_bytes
                ):
                    raise CollectionRejected("PLATFORM_ERROR", "BROWSER_RESPONSE_TOO_LARGE")
                raw = await response.body()
                if len(raw) > self.collection.max_browser_response_bytes:
                    raise CollectionRejected("PLATFORM_ERROR", "BROWSER_RESPONSE_TOO_LARGE")
                bodies[kind] = raw
                responses[kind] = response
                if (
                    "main" in bodies
                    and (not require_identity or "identity" in bodies)
                    and not future.done()
                    and active
                ):
                    future.set_result(
                        _Captured(
                            main_raw=bodies["main"],
                            main_response=responses["main"],
                            identity_raw=bodies.get("identity"),
                            captured_at=utc_now(),
                        )
                    )
            except (ValueError, OSError, CollectionRejected) as exc:
                if not future.done() and active:
                    future.set_exception(exc)

        def on_response(response: Response) -> None:
            if not active or future.done() or len(tasks) >= self.collection.max_inflight_responses:
                return
            kind: Literal["main", "identity"] | None = None
            if self._matches(response, adapter, identity=False):
                kind = "main"
            elif require_identity and self._matches(response, adapter, identity=True):
                kind = "identity"
            if kind is None or kind in scheduled:
                return
            scheduled.add(kind)
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

    def _validate_response_request(
        self,
        response: Response,
        adapter: PromotionMetricsAdapterSettings,
        expected: _ExpectedWindow,
    ) -> None:
        try:
            request = response.request.post_data_json
        except Exception as exc:
            raise CollectionRejected(
                "TIME_SCOPE_UNVERIFIED", "PROMOTION_REQUEST_BODY_UNAVAILABLE"
            ) from exc
        if not isinstance(request, dict):
            raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "PROMOTION_REQUEST_BODY_NOT_OBJECT")
        if set(request) != _REQUEST_KEYS:
            raise CollectionRejected(
                "TIME_SCOPE_UNVERIFIED", "PROMOTION_REQUEST_FILTERS_UNVERIFIED"
            )
        if type(request.get("filter")) is not dict or request["filter"]:
            raise CollectionRejected(
                "TIME_SCOPE_UNVERIFIED", "PROMOTION_REQUEST_FILTERS_UNVERIFIED"
            )
        crawler_info = request.get("crawlerInfo")
        if (
            type(crawler_info) is not str
            or not crawler_info
            or len(crawler_info) > adapter.request_crawler_info_max_length
        ):
            raise CollectionRejected(
                "TIME_SCOPE_UNVERIFIED", "PROMOTION_REQUEST_CRAWLER_INFO_INVALID"
            )
        for field, expected_constant in _REQUEST_CONSTANTS.items():
            actual = request.get(field)
            if type(actual) is not type(expected_constant) or actual != expected_constant:
                raise CollectionRejected(
                    "TIME_SCOPE_UNVERIFIED", f"PROMOTION_REQUEST_CONSTANT_MISMATCH:{field}"
                )
        begin = self._request_date(request, adapter.request_begin_date_field)
        end = self._request_date(request, adapter.request_end_date_field)
        if begin != expected.request_begin_date or end != expected.request_end_date:
            raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "PROMOTION_REQUEST_DATE_MISMATCH")
        page_number = self._request_integer(request, adapter.request_page_number_field)
        page_size = self._request_integer(request, adapter.request_page_size_field)
        if page_number != 1 or page_size != adapter.platform_page_size:
            raise CollectionRejected("DATA_MISMATCH", "PROMOTION_REQUEST_PAGINATION_MISMATCH")

    def _validate_capture_date(
        self, captured_at: datetime, current_date: date, timezone: str
    ) -> None:
        if captured_at.astimezone(ZoneInfo(timezone)).date() != current_date:
            raise CollectionRejected(
                "TIME_SCOPE_UNVERIFIED", "PROMOTION_CAPTURE_CROSSED_DATE_BOUNDARY"
            )

    def _request_date(self, request: dict[str, Any], path: str) -> str:
        try:
            value = read_object_path(request, path)
        except CollectionRejected as exc:
            raise CollectionRejected(
                "TIME_SCOPE_UNVERIFIED", "PROMOTION_REQUEST_DATE_MISSING"
            ) from exc
        if not isinstance(value, str):
            raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "PROMOTION_REQUEST_DATE_INVALID")
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError as exc:
            raise CollectionRejected(
                "TIME_SCOPE_UNVERIFIED", "PROMOTION_REQUEST_DATE_INVALID"
            ) from exc
        if parsed.isoformat() != value:
            raise CollectionRejected(
                "TIME_SCOPE_UNVERIFIED", "PROMOTION_REQUEST_DATE_NOT_CANONICAL"
            )
        return value

    def _request_integer(self, request: dict[str, Any], path: str) -> int:
        try:
            value = read_object_path(request, path)
        except CollectionRejected as exc:
            raise CollectionRejected(
                "DATA_MISMATCH", "PROMOTION_REQUEST_PAGINATION_MISSING"
            ) from exc
        if type(value) is not int or value < 1:
            raise CollectionRejected("DATA_MISMATCH", "PROMOTION_REQUEST_PAGINATION_INVALID")
        return value

    def _parse_capture(
        self, captured: _Captured, adapter: PromotionMetricsAdapterSettings
    ) -> _ParsedCapture:
        if captured.identity_raw is None:
            raise CollectionRejected("IDENTITY_UNVERIFIED", "IDENTITY_RESPONSE_MISSING")
        independent_identity = parse_response_identity(
            captured.identity_raw,
            adapter,
            expected_store_id=self.connection.expected_platform_store_id,
            expected_store_id_sha256=self.connection.expected_platform_store_id_sha256,
        )
        parsed = parse_promoted_product_response(
            captured.main_raw,
            observed_at=captured.captured_at,
            expected_store_id=self.connection.expected_platform_store_id,
            expected_store_id_sha256=self.connection.expected_platform_store_id_sha256,
        )
        self._match_row_and_independent_identity(parsed, independent_identity)
        return _ParsedCapture(parsed, independent_identity, captured.captured_at)

    def _match_row_and_independent_identity(
        self, parsed: ParsedPromotedProductResponse, independent_identity: str
    ) -> None:
        if (
            parsed.observed_platform_store_id is not None
            and parsed.observed_platform_store_id != independent_identity
        ):
            raise CollectionRejected("IDENTITY_MISMATCH", "PROMOTION_IDENTITY_GATES_MISMATCH")

    async def _crosscheck_dom(
        self, page: Page, raw: bytes, adapter: PromotionMetricsAdapterSettings
    ) -> None:
        payload = decode_json_object(raw)
        rows_value = read_object_path(payload, adapter.list_path)
        if not isinstance(rows_value, list) or not all(
            isinstance(item, dict) for item in rows_value
        ):
            raise CollectionRejected("DATA_MISMATCH", "PROMOTION_ROWS_NOT_ARRAY")
        locator = page.locator(adapter.dom_row_selector)
        dom_rows = await locator.all_text_contents()
        normalized_dom_rows = ["".join(text.split()) for text in dom_rows]
        matched_dom_indices: set[int] = set()
        for item in rows_value:
            assert isinstance(item, dict)
            try:
                name = read_object_path(item, adapter.product_name_path)
            except CollectionRejected as exc:
                raise CollectionRejected("DATA_MISMATCH", "PROMOTION_PRODUCT_NAME_MISSING") from exc
            if not isinstance(name, str) or not "".join(name.split()):
                raise CollectionRejected("DATA_MISMATCH", "PROMOTION_PRODUCT_NAME_MISSING")
            normalized_name = "".join(name.split())
            matches = [
                index
                for index, dom_row in enumerate(normalized_dom_rows)
                if normalized_name in dom_row
            ]
            if len(matches) != 1 or matches[0] in matched_dom_indices:
                raise CollectionRejected("DATA_MISMATCH", "PROMOTION_NETWORK_DOM_PRODUCT_MISMATCH")
            matched_dom_indices.add(matches[0])

    def _coverage(self, *, source_rows: int, payload_records: int, limit: int) -> Coverage:
        page_size = self.connection.promotion_metrics_adapter.platform_page_size
        if source_rows > page_size:
            raise CollectionRejected("DATA_MISMATCH", "PROMOTION_PAGE_EXCEEDS_REQUEST_SIZE")
        if payload_records < source_rows:
            return Coverage(
                total_observed=source_rows if source_rows < page_size else None,
                captured=payload_records,
                limit=limit,
                pages_read=1,
                coverage=CoverageStatus.TRUNCATED,
                truncated=True,
                stop_reason="CAPTURE_LIMIT_REACHED",
            )
        if source_rows == page_size:
            return Coverage(
                total_observed=None,
                captured=payload_records,
                limit=limit,
                pages_read=1,
                coverage=CoverageStatus.TRUNCATED,
                truncated=True,
                stop_reason="FULL_PAGE_REQUIRES_PAGINATION",
            )
        return Coverage(
            total_observed=source_rows,
            captured=payload_records,
            limit=limit,
            pages_read=1,
            coverage=CoverageStatus.COMPLETE,
            truncated=False,
        )

    def _build_draft(
        self,
        *,
        requested_at: datetime,
        started_at: datetime,
        store_id: str,
        dataset_type: DatasetType,
        scope: Scope,
        limit: int,
        batch_id: str | None,
        adapter: PromotionMetricsAdapterSettings,
        parsed: _ParsedCapture,
        requested_window: _ExpectedWindow | None,
    ) -> SnapshotDraft:
        response = parsed.response
        source_rows = len(response.metric_records)
        metric_window: MetricWindow | None = None
        source_updated_at: datetime | None = response.source_updated_at
        missing_fields: list[str]
        field_sources: dict[str, Literal["NETWORK_RESPONSE", "DOM"]]
        payload: dict[str, Any] | list[dict[str, Any]]

        if dataset_type is DatasetType.PRODUCT_METRICS:
            metric_records = response.metric_records[:limit]
            payload = [record.model_dump(mode="json") for record in metric_records]
            payload_records = len(metric_records)
            coverage = self._coverage(
                source_rows=source_rows, payload_records=payload_records, limit=limit
            )
            missing_fields = [
                f"records[{index}].metrics.{field}:{reason}"
                for index, record in enumerate(metric_records)
                for field, reason in record.metrics.missing_reasons.items()
            ]
            field_sources = {
                f"records.metrics.{field}": "NETWORK_RESPONSE"
                for record in metric_records
                for field in record.metrics.model_fields_set
                if field != "missing_reasons" and getattr(record.metrics, field) is not None
            }
            assert requested_window is not None
            metric_window = self._metric_window(scope, requested_window, parsed.captured_at)
            if source_updated_at is None or not (
                metric_window.start < source_updated_at <= metric_window.end
            ):
                raise CollectionRejected(
                    "DATA_MISMATCH", "PROMOTION_SOURCE_UPDATE_OUTSIDE_METRIC_WINDOW"
                )
        else:
            configuration_records = response.configuration_records[:limit]
            payload = [record.model_dump(mode="json") for record in configuration_records]
            payload_records = len(configuration_records)
            coverage = self._coverage(
                source_rows=source_rows, payload_records=payload_records, limit=limit
            )
            missing_fields = [
                f"records[{index}].{field}:{reason}"
                for index, record in enumerate(configuration_records)
                for field, reason in record.missing_reasons.items()
            ]
            field_sources = {
                f"records.{field}": "NETWORK_RESPONSE"
                for record in configuration_records
                for field in ("max_cost_cents", "target_roi", "agent_bid", "ad_status")
                if getattr(record, field) is not None
            }
            source_updated_at = None

        return SnapshotDraft(
            batch_id=batch_id,
            store_id=store_id,
            dataset_type=dataset_type,
            scope=scope,
            scope_key=scope_key(scope),
            requested_at=requested_at,
            capture_started_at=started_at,
            capture_finished_at=utc_now(),
            captured_at=parsed.captured_at,
            metric_window=metric_window,
            source_updated_at=source_updated_at,
            source="PDD_BROWSER_CDP",
            capture_method="NETWORK_RESPONSE",
            parser_version=adapter.parser_version,
            identity_evidence=self._identity_evidence(adapter, parsed.identity),
            field_sources=field_sources,
            payload=payload,
            missing_fields=missing_fields,
            quality=Quality(
                status="VALID",
                identity="MATCHED",
                coverage=coverage.coverage,
                dom_check="MATCHED",
            ),
            coverage=coverage,
        )

    def _metric_window(
        self, scope: Scope, expected: _ExpectedWindow, captured_at: datetime
    ) -> MetricWindow:
        if scope.kind is WindowKind.YESTERDAY:
            end = expected.end_date + timedelta(days=1)
        else:
            end = captured_at
        return MetricWindow(
            kind=scope.kind,
            timezone=scope.timezone,
            start=expected.begin_date,
            end=end,
            window_complete=expected.window_complete,
            source_finalized=None if expected.window_complete else False,
        )

    def _identity_evidence(
        self, adapter: PromotionMetricsAdapterSettings, observed: str
    ) -> IdentityEvidence:
        verification_method: Literal["CONFIG_EXACT_ID", "MERCHANT_PAGE_STATE_SHA256"] = (
            "MERCHANT_PAGE_STATE_SHA256"
            if self.connection.expected_platform_store_id_sha256
            else "CONFIG_EXACT_ID"
        )
        return IdentityEvidence(
            expected_platform_store_id=(
                self.connection.expected_platform_store_id
                or f"sha256:{self.connection.expected_platform_store_id_sha256}"
            ),
            observed_platform_store_id=observed,
            evidence_source="NETWORK_RESPONSE",
            independent_verification_method=verification_method,
            independent_verification_reference=adapter.identity_verification_reference,
            response_field_path=adapter.identity_platform_store_id_path,
        )

    def _enforce_min_interval(self) -> None:
        directory = safe_child(self.runtime_root, "rate_limits")
        directory.mkdir(parents=True, exist_ok=True)
        path = safe_child(directory, f"{self.connection.connection_id}.json")
        now = utc_now()
        if path.exists():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                previous = datetime.fromisoformat(str(value["started_at"]))
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


__all__ = ["PromotionMetricsCdpCollector"]
