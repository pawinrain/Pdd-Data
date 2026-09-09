from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from pdd_data_mcp.browser.dispatcher import RealDatasetCollector
from pdd_data_mcp.browser.promotion_metrics import PromotionMetricsCdpCollector
from pdd_data_mcp.config import (
    CollectionSettings,
    ConnectionSettings,
    PromotionMetricsAdapterSettings,
)
from pdd_data_mcp.contracts.models import CoverageStatus, DatasetType, Scope, WindowKind
from pdd_data_mcp.errors import CollectionRejected

PAGE_URL = "https://yingxiao.pinduoduo.com/goods/promotion/list"
MAIN_HOST = "yingxiao.pinduoduo.com"
MAIN_PATH = "/mms-gateway/venus/api/goods/promotion/v3/list"
IDENTITY_HOST = "yingxiao.pinduoduo.com"
IDENTITY_PATH = "/mms-gateway/venus/api/user/userInfo"
DOM_ROWS = "tbody tr"


class FakeRequest:
    method = "POST"

    def __init__(self, post_data_json: object) -> None:
        self.post_data_json = post_data_json


class FakeResponse:
    def __init__(
        self,
        body: dict[str, Any],
        *,
        host: str = MAIN_HOST,
        path: str,
        request_body: object | None = None,
        content_type: str = "application/json; charset=utf-8",
    ) -> None:
        self.url = f"https://{host}{path}"
        self.status = 200
        self.headers = {"content-type": content_type}
        self.request = FakeRequest(request_body)
        self._body = json.dumps(body, separators=(",", ":")).encode()
        self.body_reads = 0

    async def body(self) -> bytes:
        self.body_reads += 1
        await asyncio.sleep(0)
        return self._body


class FakeLocator:
    def __init__(
        self,
        *,
        texts: list[str] | None = None,
        click: Any | None = None,
        visible: bool = True,
        enabled: bool = True,
    ) -> None:
        self.texts = list(texts or [])
        self._click = click
        self.visible = visible
        self.enabled = enabled
        self.clicks = 0

    async def count(self) -> int:
        if self._click is not None:
            return 1
        return len(self.texts)

    async def is_visible(self) -> bool:
        return self.visible

    async def is_enabled(self) -> bool:
        return self.enabled

    async def click(self, **kwargs: Any) -> None:
        del kwargs
        self.clicks += 1
        if self._click is None:
            raise AssertionError("locator is not clickable")
        await self._click()

    async def all_text_contents(self) -> list[str]:
        return list(self.texts)


@dataclass
class Action:
    responses: list[FakeResponse]
    dom_rows: list[str]


class FakePage:
    def __init__(self, actions: list[Action]) -> None:
        self.url = PAGE_URL
        self.actions = actions
        self.action_index = 0
        self.listeners: list[Any] = []
        self.reload_count = 0
        self.row_locator = FakeLocator(texts=[])
        self.quick_locators: dict[str, FakeLocator] = {}
        for index in range(5):
            test_id = f"DateAreaQuickOption_{index}"
            self.quick_locators[f'[data-testid="{test_id}"]'] = FakeLocator(
                click=self._dispatch_next
            )

    def is_closed(self) -> bool:
        return False

    def locator(self, selector: str) -> FakeLocator:
        if selector == DOM_ROWS:
            return self.row_locator
        return self.quick_locators.get(selector, FakeLocator())

    def on(self, event: str, callback: Any) -> None:
        assert event == "response"
        self.listeners.append(callback)

    def remove_listener(self, event: str, callback: Any) -> None:
        assert event == "response"
        self.listeners.remove(callback)

    async def reload(self, **kwargs: Any) -> None:
        del kwargs
        self.reload_count += 1
        await self._dispatch_next()

    async def _dispatch_next(self) -> None:
        if self.action_index >= len(self.actions):
            raise AssertionError("unexpected browser action")
        action = self.actions[self.action_index]
        self.action_index += 1
        self.row_locator.texts = list(action.dom_rows)
        for response in action.responses:
            for callback in list(self.listeners):
                callback(response)
        await asyncio.sleep(0)
        await asyncio.sleep(0)


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.pages = [page]


class FakeBrowser:
    def __init__(self, page: FakePage) -> None:
        self.contexts = [FakeContext(page)]


class FakeSession:
    def __init__(self, page: FakePage) -> None:
        self.browser = FakeBrowser(page)
        self.disconnected = False

    async def disconnect(self) -> None:
        self.disconnected = True


class FakeConnector:
    def __init__(self, session: FakeSession) -> None:
        self.session = session
        self.connect_count = 0

    async def connect(self, connection: ConnectionSettings, timeout_ms: int) -> FakeSession:
        del connection, timeout_ms
        self.connect_count += 1
        return self.session


def local_today() -> date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def wrapped(unit: str, value: object) -> dict[str, object]:
    return {"unit": unit, "unitCode": 1, "value": value}


def report() -> dict[str, object]:
    return {
        "spend": wrapped("YUAN", "12.34"),
        "orderSpend": wrapped("YUAN", "56.78"),
        "orderSpendRoiUnified": wrapped("PER_ONE", "4.6"),
        "orderSpendNetRoi": wrapped("PER_ONE", "4.1"),
        "netOrderNum": 2,
        "orderNum": 3,
        "gmv": wrapped("YUAN", "60.00"),
        "netGmv": wrapped("YUAN", "50.00"),
        "impression": 1000,
        "click": 40,
        "settlementRoi": wrapped("PER_ONE", "3.2"),
        "settlementOrder": 1,
    }


def promotion_row(index: int, *, mall_id: int = 1001) -> dict[str, object]:
    return {
        "adId": 10_000 + index,
        "planId": 20_000 + index,
        "goodsId": 30_000 + index,
        "mallId": mall_id,
        "goodsInfo": {"goodsName": f"Product-{index:04d}"},
        "reportInfo": report(),
        "maxCost": wrapped("YUAN", "100.00"),
        "targetRoi": wrapped("PER_ONE", "3.5"),
        "agentBid": None,
        "adStatus": 1,
    }


def promotion_payload(
    count: int,
    *,
    changed_report: dict[str, object] | None = None,
    source_updated_at: datetime | None = None,
) -> dict[str, Any]:
    rows = [promotion_row(index) for index in range(1, count + 1)]
    if changed_report is not None and rows:
        rows[0]["reportInfo"] = changed_report
    return {
        "success": True,
        "result": {
            "adInfos": rows,
            "sumReportInfo": report(),
            "reportLastUpdateTime": int(
                (source_updated_at or datetime.now(UTC)).timestamp() * 1000
            ),
        },
    }


def identity_payload(mall_id: int = 1001) -> dict[str, Any]:
    return {"success": True, "result": {"mall": {"mallId": mall_id}}}


def expected_dates(kind: WindowKind) -> tuple[str, str]:
    current = local_today()
    if kind is WindowKind.YESTERDAY:
        previous = current - timedelta(days=1)
        return previous.isoformat(), previous.isoformat()
    return current.isoformat(), current.isoformat()


def request_body(kind: WindowKind, *, page_size: int = 50) -> dict[str, object]:
    begin, end = expected_dates(kind)
    return {
        "beginDate": begin,
        "blockType": 3,
        "clientType": 1,
        "crawlerInfo": "opaque-fixture",
        "endDate": end,
        "filter": {},
        "orderBy": 9999,
        "pageNumber": 1,
        "pageSize": page_size,
        "scenesMode": 1,
        "showGoodsPromotionHistoryReport": False,
        "sortBy": 9999,
        "withTagsInfo": True,
    }


def main_response(
    count: int,
    *,
    kind: WindowKind = WindowKind.TODAY,
    request: object | None = None,
    changed_report: dict[str, object] | None = None,
    source_updated_at: datetime | None = None,
) -> FakeResponse:
    return FakeResponse(
        promotion_payload(
            count,
            changed_report=changed_report,
            source_updated_at=source_updated_at,
        ),
        path=MAIN_PATH,
        request_body=request_body(kind) if request is None else request,
    )


def identity_response(mall_id: int = 1001) -> FakeResponse:
    return FakeResponse(
        identity_payload(mall_id),
        host=IDENTITY_HOST,
        path=IDENTITY_PATH,
        request_body={},
    )


def dom_rows(count: int) -> list[str]:
    return [f"状态 推广中 Product-{index:04d} 数据" for index in range(1, count + 1)]


def adapter() -> PromotionMetricsAdapterSettings:
    windows = ["TODAY"]
    return PromotionMetricsAdapterSettings.model_validate(
        {
            "verified": True,
            "data_source": "NETWORK_RESPONSE",
            "request_contract_version": "PROMOTED_PRODUCT_LIST_UNFILTERED_V1",
            "request_crawler_info_max_length": 4096,
            "target_page_url": PAGE_URL,
            "response_host": MAIN_HOST,
            "response_path": MAIN_PATH,
            "response_method": "POST",
            "response_http_status": 200,
            "business_success_path": "success",
            "business_success_value": True,
            "identity_source": "IDENTITY_RESPONSE",
            "identity_response_host": IDENTITY_HOST,
            "identity_response_path": IDENTITY_PATH,
            "identity_response_method": "POST",
            "identity_response_http_status": 200,
            "identity_business_success_path": "success",
            "identity_business_success_value": True,
            "identity_platform_store_id_path": "result.mall.mallId",
            "identity_verification_reference": "fixture-independent-identity-v1",
            "parser_version": "pdd-promoted-product-v3/1.0.0",
            "trigger": "RELOAD",
            "supported_windows": windows,
            "quick_option_testids": {
                window: f"DateAreaQuickOption_{index}" for index, window in enumerate(windows)
            },
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
            "platform_page_size": 50,
            "pagination_terminal_rule": "SHORT_PAGE",
            "dom_row_selector": DOM_ROWS,
            "reload_resets_to_today": True,
            "configuration_current_only": True,
        }
    )


def connection(
    *, use_sha256: bool = False, adapter_value: object | None = None
) -> ConnectionSettings:
    identity: dict[str, str] = (
        {"expected_platform_store_id_sha256": hashlib.sha256(b"1001").hexdigest()}
        if use_sha256
        else {"expected_platform_store_id": "1001"}
    )
    return ConnectionSettings.model_validate(
        {
            "connection_id": "conn_current",
            "store_id": "st_current",
            "cdp_endpoint": "http://127.0.0.1:9222",
            **identity,
            "real_collection_enabled": True,
            "promotion_metrics_adapter": adapter_value or adapter(),
        }
    )


def make_collector(
    tmp_path: Path,
    actions: list[Action],
    *,
    connection_value: ConnectionSettings | None = None,
) -> tuple[PromotionMetricsCdpCollector, FakePage, FakeSession, FakeConnector]:
    page = FakePage(actions)
    session = FakeSession(page)
    connector = FakeConnector(session)
    instance = PromotionMetricsCdpCollector(
        connection=connection_value or connection(),
        collection=CollectionSettings(min_interval_seconds=0, collection_timeout_ms=1000),
        runtime_root=tmp_path,
        connector=connector,  # type: ignore[arg-type]
    )
    return instance, page, session, connector


def scope(dataset: DatasetType, kind: WindowKind) -> Scope:
    business_date = (
        local_today() - timedelta(days=1) if kind is WindowKind.YESTERDAY else local_today()
    )
    return Scope(
        kind=kind,
        business_date=business_date,
        object_type=(
            "ACCOUNT_ALL" if dataset is DatasetType.PROMOTION_OVERVIEW else "PROMOTED_PRODUCT_ALL"
        ),
    )


def collect(
    instance: PromotionMetricsCdpCollector,
    dataset: DatasetType,
    kind: WindowKind,
    *,
    limit: int = 50,
) -> Any:
    return asyncio.run(
        instance.collect(
            store_id="st_current",
            dataset_type=dataset,
            scope=scope(dataset, kind),
            limit=limit,
        )
    )


def baseline_action(count: int, *extra: FakeResponse) -> Action:
    return Action(
        [*extra, main_response(count), identity_response()],
        dom_rows(count),
    )


def test_product_metrics_uses_exact_responses_and_never_reads_unknown_body(
    tmp_path: Path,
) -> None:
    unknown = FakeResponse({"secret": "not read"}, path="/api/unknown", request_body={})
    instance, page, session, _ = make_collector(tmp_path, [baseline_action(3, unknown)])
    draft = collect(instance, DatasetType.PRODUCT_METRICS, WindowKind.TODAY)

    assert unknown.body_reads == 0
    assert page.listeners == []
    assert session.disconnected is True
    assert draft.dataset_type is DatasetType.PRODUCT_METRICS
    assert draft.metric_window is not None and draft.metric_window.window_complete is False
    assert draft.payload[0]["metrics"]["spend_cents"] == 1234
    assert draft.payload[0]["metrics"]["gmv_cents"] == 6000
    assert "Product" not in json.dumps(draft.payload)


def test_product_metrics_rejects_source_update_outside_today_window(tmp_path: Path) -> None:
    zone = ZoneInfo("Asia/Shanghai")
    before_today = datetime.combine(local_today(), datetime.min.time(), tzinfo=zone) - timedelta(
        seconds=1
    )
    main = main_response(1, source_updated_at=before_today)
    instance, page, session, _ = make_collector(
        tmp_path,
        [Action([main, identity_response()], dom_rows(1))],
    )

    with pytest.raises(CollectionRejected) as caught:
        collect(instance, DatasetType.PRODUCT_METRICS, WindowKind.TODAY)

    assert caught.value.status == "DATA_MISMATCH"
    assert caught.value.error_code == "PROMOTION_SOURCE_UPDATE_OUTSIDE_METRIC_WINDOW"
    assert main.body_reads == 1
    assert page.listeners == []
    assert session.disconnected is True


def test_product_list_collector_rejects_account_overview_before_connect(tmp_path: Path) -> None:
    instance, _, _, connector = make_collector(tmp_path, [])

    with pytest.raises(CollectionRejected) as caught:
        collect(instance, DatasetType.PROMOTION_OVERVIEW, WindowKind.TODAY)

    assert caught.value.error_code == "PROMOTION_DATASET_NOT_ADAPTED"
    assert connector.connect_count == 0


class _RoutingStub:
    def __init__(self, marker: str) -> None:
        self.marker = marker
        self.calls: list[dict[str, Any]] = []

    async def collect(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.marker


def test_dispatcher_never_routes_overview_to_product_list_collector() -> None:
    stage_c = _RoutingStub("stage-c")
    account = _RoutingStub("account-v2")
    product_list = _RoutingStub("product-list")
    core = _RoutingStub("core")
    dispatcher = RealDatasetCollector(
        promotion=stage_c,  # type: ignore[arg-type]
        promotion_account=account,  # type: ignore[arg-type]
        promotion_metrics=product_list,  # type: ignore[arg-type]
        core=core,  # type: ignore[arg-type]
    )

    result = asyncio.run(
        dispatcher.collect(
            store_id="st_current",
            dataset_type=DatasetType.PROMOTION_OVERVIEW,
            scope=scope(DatasetType.PROMOTION_OVERVIEW, WindowKind.TODAY),
            limit=50,
        )
    )

    assert result == "stage-c"
    assert len(stage_c.calls) == 1
    assert account.calls == []
    assert product_list.calls == []
    assert core.calls == []


def test_listener_cleanup_and_disconnect_on_unit_failure(tmp_path: Path) -> None:
    changed = report()
    changed["spend"] = wrapped("PER_ONE", "1")
    main = main_response(1, changed_report=changed)
    instance, page, session, _ = make_collector(
        tmp_path, [Action([main, identity_response()], dom_rows(1))]
    )
    with pytest.raises(CollectionRejected) as caught:
        collect(instance, DatasetType.PRODUCT_METRICS, WindowKind.TODAY)
    assert caught.value.status == "UNIT_UNVERIFIED"
    assert page.listeners == []
    assert session.disconnected is True


def test_request_date_and_pagination_are_strictly_matched(tmp_path: Path) -> None:
    wrong_date = request_body(WindowKind.TODAY)
    wrong_date["beginDate"] = (local_today() - timedelta(days=1)).isoformat()
    instance, page, session, _ = make_collector(
        tmp_path,
        [
            Action(
                [main_response(1, request=wrong_date), identity_response()],
                dom_rows(1),
            )
        ],
    )
    with pytest.raises(CollectionRejected) as caught:
        collect(instance, DatasetType.PRODUCT_METRICS, WindowKind.TODAY)
    assert caught.value.error_code == "PROMOTION_REQUEST_DATE_MISMATCH"
    assert page.listeners == [] and session.disconnected

    wrong_page = request_body(WindowKind.TODAY)
    wrong_page["pageNumber"] = 2
    instance, _, _, _ = make_collector(
        tmp_path / "second",
        [Action([main_response(1, request=wrong_page), identity_response()], dom_rows(1))],
    )
    with pytest.raises(CollectionRejected) as caught:
        collect(instance, DatasetType.PRODUCT_METRICS, WindowKind.TODAY)
    assert caught.value.error_code == "PROMOTION_REQUEST_PAGINATION_MISMATCH"


def test_request_with_unverified_filter_is_rejected(tmp_path: Path) -> None:
    filtered = request_body(WindowKind.TODAY)
    filtered["filter"] = {"adStatus": [2]}
    instance, page, session, _ = make_collector(
        tmp_path,
        [Action([main_response(1, request=filtered), identity_response()], dom_rows(1))],
    )

    with pytest.raises(CollectionRejected) as caught:
        collect(instance, DatasetType.PRODUCT_METRICS, WindowKind.TODAY)

    assert caught.value.error_code == "PROMOTION_REQUEST_FILTERS_UNVERIFIED"
    assert page.listeners == [] and session.disconnected


@pytest.mark.parametrize(
    ("field", "drifted"),
    [
        ("blockType", 4),
        ("clientType", 2),
        ("orderBy", 1),
        ("scenesMode", 2),
        ("showGoodsPromotionHistoryReport", True),
        ("sortBy", 1),
        ("withTagsInfo", False),
    ],
)
def test_request_scope_constants_are_exact(tmp_path: Path, field: str, drifted: object) -> None:
    changed = request_body(WindowKind.TODAY)
    changed[field] = drifted
    instance, page, session, _ = make_collector(
        tmp_path,
        [Action([main_response(1, request=changed), identity_response()], dom_rows(1))],
    )

    with pytest.raises(CollectionRejected) as caught:
        collect(instance, DatasetType.PRODUCT_METRICS, WindowKind.TODAY)

    assert caught.value.error_code == f"PROMOTION_REQUEST_CONSTANT_MISMATCH:{field}"
    assert page.listeners == [] and session.disconnected


@pytest.mark.parametrize("crawler_info", ["", 1, None])
def test_request_crawler_info_requires_bounded_nonempty_string(
    tmp_path: Path, crawler_info: object
) -> None:
    changed = request_body(WindowKind.TODAY)
    changed["crawlerInfo"] = crawler_info
    instance, _, _, _ = make_collector(
        tmp_path,
        [Action([main_response(1, request=changed), identity_response()], dom_rows(1))],
    )

    with pytest.raises(CollectionRejected) as caught:
        collect(instance, DatasetType.PRODUCT_METRICS, WindowKind.TODAY)

    assert caught.value.error_code == "PROMOTION_REQUEST_CRAWLER_INFO_INVALID"


def test_request_extra_key_and_page_query_are_rejected(tmp_path: Path) -> None:
    changed = request_body(WindowKind.TODAY)
    changed["search"] = ""
    instance, _, _, _ = make_collector(
        tmp_path,
        [Action([main_response(1, request=changed), identity_response()], dom_rows(1))],
    )
    with pytest.raises(CollectionRejected) as caught:
        collect(instance, DatasetType.PRODUCT_METRICS, WindowKind.TODAY)
    assert caught.value.error_code == "PROMOTION_REQUEST_FILTERS_UNVERIFIED"

    instance, page, session, _ = make_collector(tmp_path / "query", [])
    page.url = f"{PAGE_URL}?keyword=hidden"
    with pytest.raises(CollectionRejected) as caught:
        collect(instance, DatasetType.PRODUCT_METRICS, WindowKind.TODAY)
    assert caught.value.error_code == "PROMOTION_PAGE_SCOPE_UNVERIFIED"
    assert session.disconnected


@pytest.mark.parametrize(
    ("field", "drifted", "error_code"),
    [
        ("platform_page_size", 100, "PROMOTION_REQUEST_CONTRACT_NOT_ADAPTED"),
        (
            "supported_windows",
            ["TODAY", "LAST_7_DAYS"],
            "PROMOTION_WINDOWS_NOT_EVIDENCED",
        ),
    ],
)
def test_unevidenced_page_size_or_window_is_rejected_before_connection(
    tmp_path: Path, field: str, drifted: object, error_code: str
) -> None:
    values: dict[str, object] = {field: drifted}
    if field == "supported_windows":
        values["quick_option_testids"] = {
            "TODAY": "DateAreaQuickOption_0",
            "LAST_7_DAYS": "DateAreaQuickOption_2",
        }
    changed_adapter = adapter().model_copy(update=values)
    bound = connection().model_copy(update={"promotion_metrics_adapter": changed_adapter})
    instance, _, _, connector = make_collector(tmp_path, [], connection_value=bound)

    with pytest.raises(CollectionRejected) as caught:
        collect(instance, DatasetType.PRODUCT_METRICS, WindowKind.TODAY)

    assert caught.value.error_code == error_code
    assert connector.connect_count == 0


@pytest.mark.parametrize(
    ("field", "drifted"),
    [
        ("supported_windows", ["TODAY", "YESTERDAY"]),
        ("platform_page_size", 100),
        ("response_path", "/mms-gateway/venus/api/goods/promotion/v2/list"),
        ("parser_version", "unverified-parser/1.0.0"),
    ],
)
def test_verified_adapter_config_cannot_advertise_unevidenced_contract(
    field: str, drifted: object
) -> None:
    values = adapter().model_dump(mode="python")
    values[field] = drifted
    if field == "supported_windows":
        values["quick_option_testids"] = {
            "TODAY": "DateAreaQuickOption_0",
            "YESTERDAY": "DateAreaQuickOption_1",
        }
    with pytest.raises(ValueError):
        PromotionMetricsAdapterSettings.model_validate(values)


def test_configuration_is_current_point_in_time_and_keeps_null_reason(tmp_path: Path) -> None:
    instance, _, _, _ = make_collector(tmp_path, [baseline_action(1)])
    draft = collect(instance, DatasetType.PROMOTION_CONFIGURATION, WindowKind.POINT_IN_TIME)
    assert draft.metric_window is None
    assert draft.source_updated_at is None
    assert draft.payload[0]["agent_bid"] is None
    assert draft.payload[0]["missing_reasons"] == {"agent_bid": "SOURCE_VALUE_NULL"}
    assert draft.missing_fields == ["records[0].agent_bid:SOURCE_VALUE_NULL"]

    instance, _, _, connector = make_collector(tmp_path / "invalid", [])
    with pytest.raises(CollectionRejected) as caught:
        collect(instance, DatasetType.PROMOTION_CONFIGURATION, WindowKind.TODAY)
    assert caught.value.error_code == "PROMOTION_CONFIGURATION_REQUIRES_CURRENT_POINT"
    assert connector.connect_count == 0


def test_dom_name_must_map_to_one_unique_row(tmp_path: Path) -> None:
    main = main_response(2)
    action = Action([main, identity_response()], ["Product-0001 Product-0002"])
    instance, page, session, _ = make_collector(tmp_path, [action])
    with pytest.raises(CollectionRejected) as caught:
        collect(instance, DatasetType.PRODUCT_METRICS, WindowKind.TODAY)
    assert caught.value.error_code == "PROMOTION_NETWORK_DOM_PRODUCT_MISMATCH"
    assert page.listeners == [] and session.disconnected


@pytest.mark.parametrize(
    ("row_count", "limit", "coverage", "truncated", "stop_reason", "total"),
    [
        (3, 50, CoverageStatus.COMPLETE, False, None, 3),
        (3, 2, CoverageStatus.TRUNCATED, True, "CAPTURE_LIMIT_REACHED", 3),
        (50, 50, CoverageStatus.TRUNCATED, True, "FULL_PAGE_REQUIRES_PAGINATION", None),
    ],
)
def test_short_page_limit_and_full_page_50_semantics(
    tmp_path: Path,
    row_count: int,
    limit: int,
    coverage: CoverageStatus,
    truncated: bool,
    stop_reason: str | None,
    total: int | None,
) -> None:
    instance, _, _, _ = make_collector(tmp_path, [baseline_action(row_count)])
    draft = collect(instance, DatasetType.PRODUCT_METRICS, WindowKind.TODAY, limit=limit)
    assert draft.coverage.coverage is coverage
    assert draft.coverage.truncated is truncated
    assert draft.coverage.stop_reason == stop_reason
    assert draft.coverage.total_observed == total
    assert draft.coverage.captured == min(row_count, limit)


def test_product_yesterday_is_not_enabled_after_repeated_real_timeout(tmp_path: Path) -> None:
    instance, _, session, connector = make_collector(tmp_path, [])
    with pytest.raises(CollectionRejected) as caught:
        collect(instance, DatasetType.PRODUCT_METRICS, WindowKind.YESTERDAY)
    assert caught.value.error_code == "PROMOTION_WINDOW_NOT_SUPPORTED"
    assert connector.connect_count == 0
    assert session.disconnected is False


def test_product_capture_crossing_shanghai_midnight_is_rejected(tmp_path: Path) -> None:
    instance, _, _, _ = make_collector(tmp_path, [])
    next_day = datetime.combine(
        local_today() + timedelta(days=1),
        datetime.min.time(),
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    with pytest.raises(CollectionRejected) as caught:
        instance._validate_capture_date(next_day, local_today(), "Asia/Shanghai")
    assert caught.value.error_code == "PROMOTION_CAPTURE_CROSSED_DATE_BOUNDARY"


def test_campaign_metrics_is_rejected_before_browser_connection(tmp_path: Path) -> None:
    instance, _, session, connector = make_collector(tmp_path, [])
    with pytest.raises(CollectionRejected) as caught:
        collect(instance, DatasetType.CAMPAIGN_METRICS, WindowKind.TODAY)
    assert caught.value.status == "DATASET_UNVERIFIED"
    assert connector.connect_count == 0
    assert session.disconnected is False


def test_independent_identity_and_every_row_identity_must_both_match(tmp_path: Path) -> None:
    main = main_response(1)
    instance, page, session, _ = make_collector(
        tmp_path, [Action([main, identity_response(1002)], dom_rows(1))]
    )
    with pytest.raises(CollectionRejected) as caught:
        collect(instance, DatasetType.PRODUCT_METRICS, WindowKind.TODAY)
    assert caught.value.status == "IDENTITY_MISMATCH"
    assert main.body_reads == 1
    assert page.listeners == [] and session.disconnected


def test_sha256_binding_records_merchant_page_verification_method(tmp_path: Path) -> None:
    bound = connection(use_sha256=True)
    instance, _, _, _ = make_collector(tmp_path, [baseline_action(1)], connection_value=bound)
    draft = collect(instance, DatasetType.PRODUCT_METRICS, WindowKind.TODAY)
    assert draft.identity_evidence is not None
    assert draft.identity_evidence.independent_verification_method == "MERCHANT_PAGE_STATE_SHA256"
    assert draft.identity_evidence.observed_platform_store_id.startswith("sha256:")


@pytest.mark.parametrize(
    ("field", "drifted"),
    [
        ("response_path", "/mms-gateway/venus/api/goods/promotion/v2/list"),
        ("identity_response_path", "/mms-gateway/venus/api/user/info"),
        ("response_host", "mms.pinduoduo.com"),
    ],
)
def test_endpoint_drift_is_rejected_before_connection(
    tmp_path: Path, field: str, drifted: object
) -> None:
    drifted_adapter = adapter().model_copy(update={field: drifted})
    bound = connection().model_copy(update={"promotion_metrics_adapter": drifted_adapter})
    instance, _, _, connector = make_collector(tmp_path, [], connection_value=bound)
    with pytest.raises(CollectionRejected) as caught:
        collect(instance, DatasetType.PRODUCT_METRICS, WindowKind.TODAY)
    assert caught.value.status == "ADAPTER_UNVERIFIED"
    assert caught.value.error_code == f"PROMOTION_EVIDENCE_ENDPOINT_MISMATCH:{field}"
    assert connector.connect_count == 0
