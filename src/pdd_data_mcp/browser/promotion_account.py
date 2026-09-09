from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from playwright.async_api import Browser, Error, Page, Response

from pdd_data_mcp.browser.cdp import (
    CdpConnector,
    read_locator_value,
    select_page_by_url,
)
from pdd_data_mcp.browser.core_parsing import parse_response_identity
from pdd_data_mcp.browser.parsing import parse_dom_money, verify_store_identity
from pdd_data_mcp.browser.promotion_account_parsing import (
    PromotionAccountHourlyEvidence,
    parse_promotion_account_hourly_evidence,
    parse_promotion_account_response,
)
from pdd_data_mcp.config import (
    CollectionSettings,
    ConnectionSettings,
    PromotionAccountAdapterSettings,
)
from pdd_data_mcp.contracts.models import (
    Coverage,
    CoverageStatus,
    DatasetType,
    IdentityEvidence,
    MetricWindow,
    PromotionAccountMetricPayload,
    PromotionAccountTimeEvidence,
    Quality,
    Scope,
    SnapshotDraft,
    WindowKind,
)
from pdd_data_mcp.errors import CollectionRejected
from pdd_data_mcp.security import safe_child
from pdd_data_mcp.utils import canonical_json, scope_key, utc_now

Trigger = Callable[[], Awaitable[None]]

_ZONE = ZoneInfo("Asia/Shanghai")
_TARGET_PAGE_URL = "https://yingxiao.pinduoduo.com/mains/promotionOverview"
_MAIN_HOST = "yingxiao.pinduoduo.com"
_MAIN_PATH = "/mms-gateway/poseidon/api/report/queryHourlyRangeReport"
_IDENTITY_HOST = "yingxiao.pinduoduo.com"
_IDENTITY_PATH = "/mms-gateway/venus/api/user/info"
_REQUEST_CONTRACT_VERSION = "PROMOTION_ACCOUNT_HOURLY_DUAL_V1"
_PARSER_AND_SCOPE_VERSION = "d4-account-v3"
_PLATFORM_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_DATE_EXPLANATION_SELECTOR = "div[class*='ReportDateExplain_content__']"
_DATE_EXPLANATION_PATTERN = re.compile(
    r"\*今日截至(?P<today_hour>[01]\d|2[0-3]):(?P<today_minute>[0-5]\d)的数据\uff1b"
    r"昨日截至(?P<yesterday_hour>[01]\d|2[0-3]):"
    r"(?P<yesterday_minute>[0-5]\d)的数据"
)
_TODAY_REQUEST_KEYS = frozenset(
    {
        "blockTypes",
        "clientType",
        "crawlerInfo",
        "endDate",
        "endDayHour",
        "entityId",
        "queryDimensionType",
        "reportPromotionType",
        "returnLastUpdateTime",
        "startDate",
    }
)
_YESTERDAY_REQUEST_KEYS = _TODAY_REQUEST_KEYS - {"returnLastUpdateTime"}
_EVIDENCE_FIELDS = {
    "business_success_path": "success",
    "identity_business_success_path": "success",
    "identity_platform_store_id_path": "result.mallId",
    "daily_report_list_path": "result.dailyReportList",
    "summary_path": "result.sumReport",
    "daily_business_date_path": "date",
    "request_date_format": "PDD_MIDNIGHT_SECONDS",
    "response_date_format": "PDD_MIDNIGHT_SECONDS",
    "result_source_updated_at_path": "result.reportLastUpdateTime",
    "secondary_source_updated_at_path": "result.lastUpdateTime",
    "dom_report_date_explanation_selector": _DATE_EXPLANATION_SELECTOR,
    "request_entity_id_field": "entityId",
    "request_start_date_field": "startDate",
    "request_end_date_field": "endDate",
    "request_query_dimension_type_field": "queryDimensionType",
    "request_report_promotion_type_field": "reportPromotionType",
    "request_client_type_field": "clientType",
    "request_end_day_hour_field": "endDayHour",
    "request_return_last_update_time_field": "returnLastUpdateTime",
    "request_block_types_field": "blockTypes",
    "request_crawler_info_field": "crawlerInfo",
}


@dataclass(frozen=True)
class _RequestEvidence:
    kind: WindowKind
    start_date: date
    end_date: date
    end_day_hour: int
    request_identity: str


@dataclass(frozen=True)
class _Captured:
    main_raw: dict[WindowKind, bytes]
    requests: dict[WindowKind, _RequestEvidence]
    identity_raw: bytes
    captured_at: datetime
    trigger_before: datetime
    trigger_after: datetime


@dataclass(frozen=True)
class _DomCutoffs:
    today: time
    yesterday: time


class PromotionAccountCdpCollector:
    """Evidence-bound D4 account collector for the simultaneous day reports."""

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
        reference_local = utc_now().astimezone(_ZONE)
        adapter = self.connection.promotion_account_adapter
        self._validate_request(
            store_id=store_id,
            dataset_type=dataset_type,
            scope=scope,
            adapter=adapter,
            current_date=reference_local.date(),
        )
        self._enforce_min_interval()
        started_at = utc_now()
        session = await self.connector.connect(self.connection, self.collection.connect_timeout_ms)
        try:
            page = await self._select_safe_page(session.browser, adapter)
            captured = await self._capture_dual_reports(
                page,
                adapter,
                current_date=reference_local.date(),
                trigger=self._reload(page),
            )
            identity = parse_response_identity(
                captured.identity_raw,
                adapter,
                expected_store_id=self.connection.expected_platform_store_id,
                expected_store_id_sha256=self.connection.expected_platform_store_id_sha256,
            )
            if any(
                evidence.request_identity != identity for evidence in captured.requests.values()
            ):
                raise CollectionRejected(
                    "IDENTITY_MISMATCH", "PROMOTION_ACCOUNT_IDENTITY_GATES_MISMATCH"
                )

            today_date = reference_local.date()
            yesterday_date = today_date - timedelta(days=1)
            today = parse_promotion_account_response(
                captured.main_raw[WindowKind.TODAY],
                observed_at=captured.captured_at,
                expected_business_date=today_date,
                window_kind=WindowKind.TODAY,
            )
            yesterday = parse_promotion_account_response(
                captured.main_raw[WindowKind.YESTERDAY],
                observed_at=captured.captured_at,
                expected_business_date=yesterday_date,
                window_kind=WindowKind.YESTERDAY,
            )
            hourly_by_kind = {
                kind: parse_promotion_account_hourly_evidence(
                    captured.main_raw[kind],
                    expected_end_day_hour=captured.requests[kind].end_day_hour,
                )
                for kind in (WindowKind.TODAY, WindowKind.YESTERDAY)
            }
            dom_cutoffs = await self._crosscheck_dom(
                page,
                adapter,
                today=today,
                yesterday=yesterday,
                requests=captured.requests,
            )
            selected = today if scope.kind is WindowKind.TODAY else yesterday
            return self._build_draft(
                requested_at=requested_at,
                started_at=started_at,
                store_id=store_id,
                dataset_type=dataset_type,
                scope=scope,
                limit=limit,
                batch_id=batch_id,
                adapter=adapter,
                identity=identity,
                captured_at=captured.captured_at,
                today=today,
                selected=selected,
                requests=captured.requests,
                hourly_by_kind=hourly_by_kind,
                dom_cutoffs=dom_cutoffs,
            )
        finally:
            await session.disconnect()

    def _validate_request(
        self,
        *,
        store_id: str,
        dataset_type: DatasetType,
        scope: Scope,
        adapter: PromotionAccountAdapterSettings,
        current_date: date,
    ) -> None:
        if dataset_type is not DatasetType.PROMOTION_OVERVIEW:
            raise CollectionRejected("DATASET_UNVERIFIED", "PROMOTION_ACCOUNT_DATASET_NOT_ADAPTED")
        if not adapter.verified:
            raise CollectionRejected("ADAPTER_UNVERIFIED", "PROMOTION_ACCOUNT_ADAPTER_NOT_VERIFIED")
        self._validate_evidence_adapter(adapter)
        if store_id != self.connection.store_id:
            raise CollectionRejected("IDENTITY_MISMATCH", "INTERNAL_STORE_ID_MISMATCH")
        if (
            scope.timezone != "Asia/Shanghai"
            or scope.object_type != "ACCOUNT_ALL"
            or scope.filters
            or scope.currency != "CNY"
            or scope.attribution != "PLATFORM_DEFAULT"
            or scope.version != _PARSER_AND_SCOPE_VERSION
        ):
            raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "PROMOTION_ACCOUNT_SCOPE_NOT_ADAPTED")
        if scope.kind not in {WindowKind.TODAY, WindowKind.YESTERDAY}:
            raise CollectionRejected(
                "TIME_SCOPE_UNVERIFIED", "PROMOTION_ACCOUNT_WINDOW_NOT_ADAPTED"
            )
        expected_date = (
            current_date if scope.kind is WindowKind.TODAY else current_date - timedelta(days=1)
        )
        if scope.business_date != expected_date:
            raise CollectionRejected(
                "TIME_SCOPE_UNVERIFIED", "PROMOTION_ACCOUNT_BUSINESS_DATE_MISMATCH"
            )

    def _validate_evidence_adapter(self, adapter: PromotionAccountAdapterSettings) -> None:
        if (
            adapter.data_source != "NETWORK_RESPONSE"
            or adapter.identity_source != "IDENTITY_RESPONSE"
            or adapter.trigger != "RELOAD"
            or adapter.business_success_value is not True
            or adapter.identity_business_success_value is not True
            or adapter.request_contract_version != _REQUEST_CONTRACT_VERSION
            or adapter.parser_version != _PARSER_AND_SCOPE_VERSION
            or adapter.request_end_day_hour_semantics
            != "INCLUSIVE_HOURLY_ROW_INDEX_WITH_DOM_CUTOFF"
            or adapter.dom_spend_unit != "CNY"
            or set(adapter.supported_windows) != {"TODAY", "YESTERDAY"}
            or not adapter.dom_today_spend_selector
            or not adapter.dom_yesterday_spend_selector
            or adapter.dom_today_spend_selector == adapter.dom_yesterday_spend_selector
        ):
            raise CollectionRejected(
                "ADAPTER_UNVERIFIED", "PROMOTION_ACCOUNT_EVIDENCE_CONTRACT_MISMATCH"
            )
        endpoints: dict[str, tuple[object, object]] = {
            "target_page_url": (adapter.target_page_url, _TARGET_PAGE_URL),
            "response_host": (adapter.response_host, _MAIN_HOST),
            "response_path": (adapter.response_path, _MAIN_PATH),
            "response_method": (adapter.response_method, "POST"),
            "response_http_status": (adapter.response_http_status, 200),
            "identity_response_host": (adapter.identity_response_host, _IDENTITY_HOST),
            "identity_response_path": (adapter.identity_response_path, _IDENTITY_PATH),
            "identity_response_method": (adapter.identity_response_method, "POST"),
            "identity_response_http_status": (adapter.identity_response_http_status, 200),
        }
        for field, (actual, expected) in endpoints.items():
            if actual != expected:
                raise CollectionRejected(
                    "ADAPTER_UNVERIFIED", f"PROMOTION_ACCOUNT_ENDPOINT_MISMATCH:{field}"
                )
        for field, expected in _EVIDENCE_FIELDS.items():
            if getattr(adapter, field) != expected:
                raise CollectionRejected(
                    "ADAPTER_UNVERIFIED", f"PROMOTION_ACCOUNT_EVIDENCE_FIELD_MISMATCH:{field}"
                )

    async def _select_safe_page(
        self, browser: Browser, adapter: PromotionAccountAdapterSettings
    ) -> Page:
        page = await select_page_by_url(
            browser,
            adapter.target_page_url,
            login_selector=adapter.login_selector,
            captcha_selector=adapter.captcha_selector,
            error_selector=adapter.error_selector,
            page_code="PROMOTION_ACCOUNT_PAGE",
        )
        parsed = urlsplit(page.url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != _MAIN_HOST
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.path != "/mains/promotionOverview"
            or parsed.query
            or parsed.fragment
        ):
            raise CollectionRejected(
                "TIME_SCOPE_UNVERIFIED", "PROMOTION_ACCOUNT_PAGE_SCOPE_UNVERIFIED"
            )
        return page

    def _reload(self, page: Page) -> Trigger:
        async def trigger() -> None:
            try:
                await page.reload(
                    wait_until="domcontentloaded",
                    timeout=self.collection.collection_timeout_ms,
                )
            except Error as exc:
                raise CollectionRejected(
                    "CAPTURE_TIMEOUT", "PROMOTION_ACCOUNT_RELOAD_FAILED"
                ) from exc

        return trigger

    def _matches(
        self,
        response: Response,
        adapter: PromotionAccountAdapterSettings,
        *,
        identity: bool,
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
            and parsed.username is None
            and parsed.password is None
            and parsed.port is None
            and parsed.path == path
            and not parsed.query
            and not parsed.fragment
            and response.request.method == method
            and response.status == status
            and content_type == "application/json"
        )

    def _request_date(self, request: dict[str, Any], field: str) -> date:
        value = request.get(field)
        if not isinstance(value, str) or len(value) != 19:
            raise CollectionRejected(
                "TIME_SCOPE_UNVERIFIED", "PROMOTION_ACCOUNT_REQUEST_DATE_INVALID"
            )
        try:
            parsed = datetime.strptime(value, _PLATFORM_DATE_FORMAT)
        except ValueError as exc:
            raise CollectionRejected(
                "TIME_SCOPE_UNVERIFIED", "PROMOTION_ACCOUNT_REQUEST_DATE_INVALID"
            ) from exc
        if parsed.strftime(_PLATFORM_DATE_FORMAT) != value or parsed.time() != time.min:
            raise CollectionRejected(
                "TIME_SCOPE_UNVERIFIED", "PROMOTION_ACCOUNT_REQUEST_DATE_NOT_CANONICAL"
            )
        return parsed.date()

    def _validate_main_request(
        self,
        response: Response,
        adapter: PromotionAccountAdapterSettings,
        current_date: date,
    ) -> _RequestEvidence:
        try:
            request = response.request.post_data_json
        except Exception as exc:
            raise CollectionRejected(
                "TIME_SCOPE_UNVERIFIED", "PROMOTION_ACCOUNT_REQUEST_BODY_UNAVAILABLE"
            ) from exc
        if not isinstance(request, dict):
            raise CollectionRejected(
                "TIME_SCOPE_UNVERIFIED", "PROMOTION_ACCOUNT_REQUEST_BODY_NOT_OBJECT"
            )
        keys = frozenset(request)
        if keys == _TODAY_REQUEST_KEYS:
            kind = WindowKind.TODAY
        elif keys == _YESTERDAY_REQUEST_KEYS:
            kind = WindowKind.YESTERDAY
        else:
            raise CollectionRejected(
                "TIME_SCOPE_UNVERIFIED", "PROMOTION_ACCOUNT_REQUEST_KEYS_MISMATCH"
            )

        expected_date = (
            current_date if kind is WindowKind.TODAY else current_date - timedelta(days=1)
        )
        start_date = self._request_date(request, adapter.request_start_date_field)
        end_date = self._request_date(request, adapter.request_end_date_field)
        if start_date != expected_date or end_date != expected_date:
            raise CollectionRejected(
                "TIME_SCOPE_UNVERIFIED", "PROMOTION_ACCOUNT_REQUEST_DATE_MISMATCH"
            )

        block_types = request.get(adapter.request_block_types_field)
        if (
            type(block_types) is not list
            or len(block_types) != 1
            or type(block_types[0]) is not int
            or block_types[0] != 1
        ):
            raise CollectionRejected(
                "TIME_SCOPE_UNVERIFIED",
                "PROMOTION_ACCOUNT_REQUEST_CONSTANT_MISMATCH:blockTypes",
            )
        constants: tuple[tuple[str, object], ...] = (
            (adapter.request_client_type_field, 1),
            (adapter.request_query_dimension_type_field, 0),
            (adapter.request_report_promotion_type_field, 9),
        )
        for field, expected in constants:
            actual = request.get(field)
            if type(actual) is not type(expected) or actual != expected:
                raise CollectionRejected(
                    "TIME_SCOPE_UNVERIFIED",
                    f"PROMOTION_ACCOUNT_REQUEST_CONSTANT_MISMATCH:{field}",
                )
        if kind is WindowKind.TODAY:
            actual = request.get(adapter.request_return_last_update_time_field)
            if type(actual) is not bool or actual is not True:
                raise CollectionRejected(
                    "TIME_SCOPE_UNVERIFIED",
                    "PROMOTION_ACCOUNT_REQUEST_CONSTANT_MISMATCH:returnLastUpdateTime",
                )

        crawler_info = request.get(adapter.request_crawler_info_field)
        if (
            type(crawler_info) is not str
            or not crawler_info.strip()
            or len(crawler_info) > adapter.request_crawler_info_max_length
        ):
            raise CollectionRejected(
                "TIME_SCOPE_UNVERIFIED", "PROMOTION_ACCOUNT_REQUEST_CRAWLER_INFO_INVALID"
            )
        end_day_hour = request.get(adapter.request_end_day_hour_field)
        if type(end_day_hour) is not int or not 0 <= end_day_hour <= 23:
            raise CollectionRejected(
                "TIME_SCOPE_UNVERIFIED", "PROMOTION_ACCOUNT_REQUEST_END_HOUR_INVALID"
            )
        request_identity = verify_store_identity(
            request.get(adapter.request_entity_id_field),
            self.connection.expected_platform_store_id,
            self.connection.expected_platform_store_id_sha256,
        )
        return _RequestEvidence(
            kind=kind,
            start_date=start_date,
            end_date=end_date,
            end_day_hour=end_day_hour,
            request_identity=request_identity,
        )

    async def _capture_dual_reports(
        self,
        page: Page,
        adapter: PromotionAccountAdapterSettings,
        *,
        current_date: date,
        trigger: Trigger,
    ) -> _Captured:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[
            tuple[dict[WindowKind, bytes], dict[WindowKind, _RequestEvidence], bytes, datetime]
        ] = loop.create_future()
        bodies: dict[WindowKind, bytes] = {}
        requests: dict[WindowKind, _RequestEvidence] = {}
        identity_raw: bytes | None = None
        scheduled: set[str] = set()
        tasks: set[asyncio.Task[None]] = set()
        active = True

        async def consume_main(response: Response, evidence: _RequestEvidence) -> None:
            try:
                raw = await self._bounded_body(response)
                bodies[evidence.kind] = raw
                requests[evidence.kind] = evidence
                finish_if_complete()
            except (Error, OSError, ValueError, CollectionRejected) as exc:
                fail(exc)

        async def consume_identity(response: Response) -> None:
            nonlocal identity_raw
            try:
                identity_raw = await self._bounded_body(response)
                finish_if_complete()
            except (Error, OSError, ValueError, CollectionRejected) as exc:
                fail(exc)

        def finish_if_complete() -> None:
            if (
                active
                and not future.done()
                and set(bodies) == {WindowKind.TODAY, WindowKind.YESTERDAY}
                and identity_raw is not None
            ):
                future.set_result((dict(bodies), dict(requests), identity_raw, utc_now()))

        def fail(exc: BaseException) -> None:
            if active and not future.done():
                future.set_exception(exc)

        def schedule(task: asyncio.Task[None]) -> None:
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        def on_response(response: Response) -> None:
            if not active or future.done() or len(tasks) >= self.collection.max_inflight_responses:
                return
            if self._matches(response, adapter, identity=True):
                if "identity" not in scheduled:
                    scheduled.add("identity")
                    schedule(asyncio.create_task(consume_identity(response)))
                return
            if not self._matches(response, adapter, identity=False):
                return
            try:
                evidence = self._validate_main_request(response, adapter, current_date)
            except CollectionRejected as exc:
                fail(exc)
                return
            scheduled_key = evidence.kind.value
            if scheduled_key in scheduled:
                fail(
                    CollectionRejected(
                        "DATA_MISMATCH", f"DUPLICATE_PROMOTION_ACCOUNT_RESPONSE:{scheduled_key}"
                    )
                )
                return
            scheduled.add(scheduled_key)
            schedule(asyncio.create_task(consume_main(response, evidence)))

        page.on("response", on_response)
        trigger_before = utc_now().astimezone(_ZONE)
        try:
            await trigger()
            trigger_after = utc_now().astimezone(_ZONE)
            if trigger_before.date() != trigger_after.date():
                raise CollectionRejected(
                    "TIME_SCOPE_UNVERIFIED", "PROMOTION_ACCOUNT_CAPTURE_CROSSED_DATE_BOUNDARY"
                )
            try:
                raw_by_kind, request_by_kind, identity, captured_at = await asyncio.wait_for(
                    future, timeout=self.collection.collection_timeout_ms / 1000
                )
            except TimeoutError as exc:
                raise CollectionRejected(
                    "CAPTURE_TIMEOUT", "PROMOTION_ACCOUNT_DUAL_RESPONSE_TIMEOUT"
                ) from exc
            capture_dates = {
                current_date,
                trigger_before.date(),
                trigger_after.date(),
                captured_at.astimezone(_ZONE).date(),
            }
            if capture_dates != {current_date}:
                raise CollectionRejected(
                    "TIME_SCOPE_UNVERIFIED",
                    "PROMOTION_ACCOUNT_CAPTURE_CROSSED_DATE_BOUNDARY",
                )
            latest_capture_hour = max(trigger_before.hour, trigger_after.hour)
            if any(
                evidence.end_day_hour > latest_capture_hour for evidence in request_by_kind.values()
            ):
                raise CollectionRejected(
                    "TIME_SCOPE_UNVERIFIED", "PROMOTION_ACCOUNT_REQUEST_END_HOUR_MISMATCH"
                )
            return _Captured(
                main_raw=raw_by_kind,
                requests=request_by_kind,
                identity_raw=identity,
                captured_at=captured_at,
                trigger_before=trigger_before,
                trigger_after=trigger_after,
            )
        finally:
            active = False
            page.remove_listener("response", on_response)
            if tasks:
                _, pending = await asyncio.wait(tasks, timeout=2)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

    async def _bounded_body(self, response: Response) -> bytes:
        content_length = response.headers.get("content-length")
        if (
            content_length is not None
            and int(content_length) > self.collection.max_browser_response_bytes
        ):
            raise CollectionRejected("PLATFORM_ERROR", "BROWSER_RESPONSE_TOO_LARGE")
        raw = await response.body()
        if len(raw) > self.collection.max_browser_response_bytes:
            raise CollectionRejected("PLATFORM_ERROR", "BROWSER_RESPONSE_TOO_LARGE")
        return raw

    async def _crosscheck_dom(
        self,
        page: Page,
        adapter: PromotionAccountAdapterSettings,
        *,
        today: PromotionAccountMetricPayload,
        yesterday: PromotionAccountMetricPayload,
        requests: dict[WindowKind, _RequestEvidence],
    ) -> _DomCutoffs:
        today_spend = today.metrics.spend_cents
        yesterday_spend = yesterday.metrics.spend_cents
        if today_spend is None or yesterday_spend is None:
            raise CollectionRejected("DATA_MISMATCH", "PROMOTION_ACCOUNT_DOM_SPEND_SOURCE_MISSING")
        today_text = await read_locator_value(
            page,
            adapter.dom_today_spend_selector,
            wait_timeout_ms=self.collection.collection_timeout_ms,
            failure_status="DATA_MISMATCH",
            failure_code="PROMOTION_ACCOUNT_TODAY_DOM_SPEND_MISSING",
        )
        yesterday_text = await read_locator_value(
            page,
            adapter.dom_yesterday_spend_selector,
            wait_timeout_ms=self.collection.collection_timeout_ms,
            failure_status="DATA_MISMATCH",
            failure_code="PROMOTION_ACCOUNT_YESTERDAY_DOM_SPEND_MISSING",
        )
        explanation = await read_locator_value(
            page,
            adapter.dom_report_date_explanation_selector,
            wait_timeout_ms=self.collection.collection_timeout_ms,
            failure_status="TIME_SCOPE_UNVERIFIED",
            failure_code="PROMOTION_ACCOUNT_DOM_DATE_EXPLANATION_MISSING",
        )
        today_dom = parse_dom_money(today_text, adapter.dom_spend_unit)
        yesterday_dom = parse_dom_money(yesterday_text, adapter.dom_spend_unit)
        if today_dom.precision != "EXACT" or yesterday_dom.precision != "EXACT":
            raise CollectionRejected("UNIT_UNVERIFIED", "PROMOTION_ACCOUNT_DOM_SPEND_NOT_EXACT")
        if (
            today_dom.displayed_cents != today_spend
            or yesterday_dom.displayed_cents != yesterday_spend
        ):
            raise CollectionRejected(
                "DATA_MISMATCH", "PROMOTION_ACCOUNT_NETWORK_DOM_SPEND_MISMATCH"
            )
        compact_explanation = "".join(explanation.split())
        match = _DATE_EXPLANATION_PATTERN.fullmatch(compact_explanation)
        if match is None:
            raise CollectionRejected(
                "TIME_SCOPE_UNVERIFIED",
                "PROMOTION_ACCOUNT_DOM_DATE_EXPLANATION_INVALID",
            )
        cutoffs = _DomCutoffs(
            today=time(
                hour=int(match.group("today_hour")),
                minute=int(match.group("today_minute")),
            ),
            yesterday=time(
                hour=int(match.group("yesterday_hour")),
                minute=int(match.group("yesterday_minute")),
            ),
        )
        if today.source_updated_at is None:
            raise CollectionRejected(
                "TIME_SCOPE_UNVERIFIED", "PROMOTION_ACCOUNT_TODAY_SOURCE_CUTOFF_MISSING"
            )
        today_source_local = today.source_updated_at.astimezone(_ZONE)
        if (
            today_source_local.date() != today.business_date
            or today_source_local.hour != cutoffs.today.hour
            or today_source_local.minute != cutoffs.today.minute
        ):
            raise CollectionRejected(
                "TIME_SCOPE_UNVERIFIED",
                "PROMOTION_ACCOUNT_TODAY_DOM_SOURCE_CUTOFF_MISMATCH",
            )
        if requests[WindowKind.YESTERDAY].end_day_hour != cutoffs.yesterday.hour:
            raise CollectionRejected(
                "TIME_SCOPE_UNVERIFIED",
                "PROMOTION_ACCOUNT_YESTERDAY_DOM_REQUEST_CUTOFF_MISMATCH",
            )
        if cutoffs.yesterday.minute != 59:
            raise CollectionRejected(
                "TIME_SCOPE_UNVERIFIED",
                "PROMOTION_ACCOUNT_YESTERDAY_CUTOFF_NOT_END_OF_HOUR",
            )
        return cutoffs

    def _metric_window(
        self,
        *,
        scope: Scope,
        today: PromotionAccountMetricPayload,
        selected_hourly: PromotionAccountHourlyEvidence,
    ) -> MetricWindow:
        today_start = datetime.combine(today.business_date, time.min, tzinfo=_ZONE)
        if today.source_updated_at is None:
            raise CollectionRejected(
                "TIME_SCOPE_UNVERIFIED", "PROMOTION_ACCOUNT_TODAY_SOURCE_CUTOFF_MISSING"
            )
        today_cutoff = today.source_updated_at.astimezone(_ZONE)
        if (
            today_cutoff.date() != today.business_date
            or today_cutoff <= today_start
            or today.source_updated_at > today.observed_at
        ):
            raise CollectionRejected(
                "TIME_SCOPE_UNVERIFIED", "PROMOTION_ACCOUNT_CUTOFF_OUTSIDE_TODAY"
            )
        if scope.kind is WindowKind.TODAY:
            start = today_start
            end = today_cutoff
        else:
            start = datetime.combine(scope.business_date, time.min, tzinfo=_ZONE)
            end = start + timedelta(hours=selected_hourly.last_hour + 1)
        if end <= start:
            raise CollectionRejected(
                "TIME_SCOPE_UNVERIFIED", "PROMOTION_ACCOUNT_CUTOFF_WINDOW_EMPTY"
            )
        return MetricWindow(
            kind=scope.kind,
            timezone=scope.timezone,
            start=start,
            end=end,
            window_complete=False,
            source_finalized=False,
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
        adapter: PromotionAccountAdapterSettings,
        identity: str,
        captured_at: datetime,
        today: PromotionAccountMetricPayload,
        selected: PromotionAccountMetricPayload,
        requests: dict[WindowKind, _RequestEvidence],
        hourly_by_kind: dict[WindowKind, PromotionAccountHourlyEvidence],
        dom_cutoffs: _DomCutoffs,
    ) -> SnapshotDraft:
        missing_fields = [
            f"metrics.{field}:{reason}"
            for field, reason in sorted(selected.metrics.missing_reasons.items())
        ]
        if selected.source_updated_at_missing_reason is not None:
            missing_fields.append("source_updated_at:" + selected.source_updated_at_missing_reason)
        field_sources: dict[str, Literal["NETWORK_RESPONSE", "DOM"]] = {
            "business_date": "NETWORK_RESPONSE",
            **{
                f"metrics.{field}": "NETWORK_RESPONSE"
                for field in selected.metrics.model_fields_set
                if field != "missing_reasons" and getattr(selected.metrics, field) is not None
            },
        }
        if selected.source_updated_at is not None:
            field_sources["source_updated_at"] = "NETWORK_RESPONSE"
        if scope.kind is WindowKind.TODAY:
            capture_method: Literal["NETWORK_RESPONSE", "MIXED"] = "NETWORK_RESPONSE"
            field_sources["metric_window.end"] = "NETWORK_RESPONSE"
        else:
            capture_method = "MIXED"
            field_sources["metric_window.end"] = "DOM"
        request_evidence = requests[scope.kind]
        hourly_evidence = hourly_by_kind[scope.kind]
        time_evidence = PromotionAccountTimeEvidence(
            window_kind=scope.kind,
            request_start_date=request_evidence.start_date,
            request_end_date=request_evidence.end_date,
            request_end_day_hour=request_evidence.end_day_hour,
            response_business_date=selected.business_date,
            page_today_cutoff_hhmm=dom_cutoffs.today.strftime("%H:%M"),
            page_yesterday_cutoff_hhmm=dom_cutoffs.yesterday.strftime("%H:%M"),
            response_hourly_row_count=hourly_evidence.row_count,
            response_first_hour=hourly_evidence.first_hour,
            response_last_hour=hourly_evidence.last_hour,
        )
        coverage = Coverage(
            total_observed=1,
            captured=1,
            limit=limit,
            pages_read=1,
            coverage=CoverageStatus.COMPLETE,
            truncated=False,
        )
        return SnapshotDraft(
            batch_id=batch_id,
            store_id=store_id,
            dataset_type=dataset_type,
            scope=scope,
            scope_key=scope_key(scope),
            requested_at=requested_at,
            capture_started_at=started_at,
            capture_finished_at=utc_now(),
            captured_at=captured_at,
            metric_window=self._metric_window(
                scope=scope,
                today=today,
                selected_hourly=hourly_evidence,
            ),
            source_updated_at=selected.source_updated_at,
            promotion_account_time_evidence=time_evidence,
            source="PDD_BROWSER_CDP",
            capture_method=capture_method,
            parser_version=adapter.parser_version,
            identity_evidence=self._identity_evidence(adapter, identity),
            field_sources=field_sources,
            payload=selected.model_dump(mode="json"),
            missing_fields=missing_fields,
            quality=Quality(
                status="VALID",
                identity="MATCHED",
                coverage=CoverageStatus.COMPLETE,
                dom_check="MATCHED",
            ),
            coverage=coverage,
        )

    def _identity_evidence(
        self, adapter: PromotionAccountAdapterSettings, observed: str
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


__all__ = ["PromotionAccountCdpCollector"]
