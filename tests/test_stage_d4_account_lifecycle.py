from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

import pdd_data_mcp.browser.promotion_account as account_module
from pdd_data_mcp.browser.promotion_account import PromotionAccountCdpCollector
from pdd_data_mcp.config import (
    CollectionSettings,
    ConnectionSettings,
    PromotionAccountAdapterSettings,
)
from pdd_data_mcp.contracts.models import DatasetType, Scope, WindowKind
from pdd_data_mcp.errors import CollectionRejected
from pdd_data_mcp.validation import SnapshotValidator

PAGE_URL = "https://yingxiao.pinduoduo.com/mains/promotionOverview"
MAIN_HOST = "yingxiao.pinduoduo.com"
MAIN_PATH = "/mms-gateway/poseidon/api/report/queryHourlyRangeReport"
IDENTITY_PATH = "/mms-gateway/venus/api/user/info"
TODAY_DOM = "[data-testid='account-spend-today']"
YESTERDAY_DOM = "[data-testid='account-spend-yesterday']"
DATE_EXPLANATION_DOM = "div[class*='ReportDateExplain_content__']"
ZONE = ZoneInfo("Asia/Shanghai")
FIXED_LOCAL = datetime(2026, 9, 8, 14, 45, 30, tzinfo=ZONE)
FIXED_UTC = FIXED_LOCAL.astimezone(UTC)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(account_module, "utc_now", lambda: FIXED_UTC)


class FakeRequest:
    def __init__(self, post_data_json: object, *, method: str = "POST") -> None:
        self.method = method
        self.post_data_json = post_data_json


class FakeResponse:
    def __init__(
        self,
        body: dict[str, Any],
        *,
        path: str,
        request_body: object,
        host: str = MAIN_HOST,
        query: str = "",
        method: str = "POST",
        status: int = 200,
        content_type: str = "application/json; charset=utf-8",
    ) -> None:
        self.url = f"https://{host}{path}{query}"
        self.status = status
        self.headers = {"content-type": content_type}
        self.request = FakeRequest(request_body, method=method)
        self._raw = json.dumps(body, separators=(",", ":")).encode()
        self.body_reads = 0

    async def body(self) -> bytes:
        self.body_reads += 1
        await asyncio.sleep(0)
        return self._raw


class FakeLocator:
    def __init__(self, text: str | None = None) -> None:
        self.text = text

    async def wait_for(self, **kwargs: Any) -> None:
        del kwargs

    async def count(self) -> int:
        return int(self.text is not None)

    async def text_content(self) -> str | None:
        return self.text

    async def get_attribute(self, attribute: str) -> str | None:
        del attribute
        return None


@dataclass
class Action:
    responses: list[FakeResponse]
    today_dom: str = "12.34"
    yesterday_dom: str = "6.78"
    date_explanation: str = "*\u00a0今日截至 14:30 的数据\uff1b昨日截至 14:59 的数据"


class FakePage:
    def __init__(self, action: Action) -> None:
        self.url = PAGE_URL
        self.action = action
        self.listeners: list[Any] = []
        self.reload_count = 0
        self.locators: dict[str, FakeLocator] = {
            TODAY_DOM: FakeLocator(action.today_dom),
            YESTERDAY_DOM: FakeLocator(action.yesterday_dom),
            DATE_EXPLANATION_DOM: FakeLocator(action.date_explanation),
        }

    def is_closed(self) -> bool:
        return False

    def locator(self, selector: str) -> FakeLocator:
        return self.locators.get(selector, FakeLocator())

    def on(self, event: str, callback: Any) -> None:
        assert event == "response"
        self.listeners.append(callback)

    def remove_listener(self, event: str, callback: Any) -> None:
        assert event == "response"
        self.listeners.remove(callback)

    async def reload(self, **kwargs: Any) -> None:
        del kwargs
        self.reload_count += 1
        for response in self.action.responses:
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


def wrapped(unit: str, value: object) -> dict[str, object]:
    return {"unit": unit, "unitCode": 1, "value": value}


def metrics(spend: str) -> dict[str, object]:
    return {
        "spend": wrapped("YUAN", spend),
        "orderSpend": wrapped("YUAN", "20.00"),
        "orderSpendRoiUnified": wrapped("PER_ONE", "2.5"),
        "orderSpendNetRoi": wrapped("PER_ONE", "2.1"),
        "netOrderNum": 2,
        "orderNum": 3,
        "gmv": wrapped("YUAN", "30.00"),
        "netGmv": wrapped("YUAN", "25.00"),
        "impression": 100,
        "click": 10,
        "settlementRoi": wrapped("PER_ONE", "1.8"),
        "settlementOrder": 1,
    }


def source_timestamp() -> int:
    local = FIXED_LOCAL.replace(minute=30, second=17, microsecond=123000)
    return int(local.timestamp() * 1000)


def account_payload(
    kind: WindowKind,
    *,
    bad_unit: bool = False,
    spend: str | None = None,
    end_day_hour: int = FIXED_LOCAL.hour,
) -> dict[str, Any]:
    business_date = (
        FIXED_LOCAL.date() if kind is WindowKind.TODAY else FIXED_LOCAL.date() - timedelta(days=1)
    )
    report = metrics(spend or ("12.34" if kind is WindowKind.TODAY else "6.78"))
    if bad_unit:
        report["spend"] = wrapped("PER_ONE", "12.34")
    update: int | None = source_timestamp() if kind is WindowKind.TODAY else None
    daily = {
        "date": f"{business_date.isoformat()} 00:00:00",
        **report,
    }
    return {
        "success": True,
        "result": {
            "dailyReportList": [daily],
            "hourlyReportList": [{"hour": hour} for hour in range(end_day_hour + 1)],
            "sumReport": report,
            "reportLastUpdateTime": update,
            "lastUpdateTime": update,
        },
    }


def request_body(
    kind: WindowKind,
    *,
    entity_id: object = 1001,
    end_day_hour: int = FIXED_LOCAL.hour,
) -> dict[str, object]:
    business_date = (
        FIXED_LOCAL.date() if kind is WindowKind.TODAY else FIXED_LOCAL.date() - timedelta(days=1)
    )
    result: dict[str, object] = {
        "blockTypes": [1],
        "clientType": 1,
        "crawlerInfo": "opaque-fixture-never-persisted",
        "endDate": f"{business_date.isoformat()} 00:00:00",
        "endDayHour": end_day_hour,
        "entityId": entity_id,
        "queryDimensionType": 0,
        "reportPromotionType": 9,
        "startDate": f"{business_date.isoformat()} 00:00:00",
    }
    if kind is WindowKind.TODAY:
        result["returnLastUpdateTime"] = True
    return result


def report_response(
    kind: WindowKind,
    *,
    request: object | None = None,
    bad_unit: bool = False,
    spend: str | None = None,
) -> FakeResponse:
    request_value = request_body(kind) if request is None else request
    requested_hour = (
        request_value.get("endDayHour") if isinstance(request_value, dict) else FIXED_LOCAL.hour
    )
    payload_hour = requested_hour if type(requested_hour) is int else FIXED_LOCAL.hour
    return FakeResponse(
        account_payload(
            kind,
            bad_unit=bad_unit,
            spend=spend,
            end_day_hour=payload_hour,
        ),
        path=MAIN_PATH,
        request_body=request_value,
    )


def identity_response(mall_id: int = 1001) -> FakeResponse:
    return FakeResponse(
        {"success": True, "result": {"mallId": mall_id}},
        path=IDENTITY_PATH,
        request_body={},
    )


def adapter() -> PromotionAccountAdapterSettings:
    return PromotionAccountAdapterSettings.model_validate(
        {
            "verified": True,
            "data_source": "NETWORK_RESPONSE",
            "target_page_url": PAGE_URL,
            "response_host": MAIN_HOST,
            "response_path": MAIN_PATH,
            "response_method": "POST",
            "response_http_status": 200,
            "business_success_path": "success",
            "business_success_value": True,
            "identity_source": "IDENTITY_RESPONSE",
            "identity_response_host": MAIN_HOST,
            "identity_response_path": IDENTITY_PATH,
            "identity_response_method": "POST",
            "identity_response_http_status": 200,
            "identity_business_success_path": "success",
            "identity_business_success_value": True,
            "identity_platform_store_id_path": "result.mallId",
            "identity_verification_reference": "account-identity-fixture-v1",
            "parser_version": "d4-account-v3",
            "trigger": "RELOAD",
            "request_contract_version": "PROMOTION_ACCOUNT_HOURLY_DUAL_V1",
            "supported_windows": ["TODAY", "YESTERDAY"],
            "daily_report_list_path": "result.dailyReportList",
            "summary_path": "result.sumReport",
            "daily_business_date_path": "date",
            "request_date_format": "PDD_MIDNIGHT_SECONDS",
            "response_date_format": "PDD_MIDNIGHT_SECONDS",
            "result_source_updated_at_path": "result.reportLastUpdateTime",
            "secondary_source_updated_at_path": "result.lastUpdateTime",
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
            "request_crawler_info_max_length": 4096,
            "request_end_day_hour_semantics": ("INCLUSIVE_HOURLY_ROW_INDEX_WITH_DOM_CUTOFF"),
            "dom_today_spend_selector": TODAY_DOM,
            "dom_yesterday_spend_selector": YESTERDAY_DOM,
            "dom_report_date_explanation_selector": DATE_EXPLANATION_DOM,
            "dom_spend_unit": "CNY",
        }
    )


def connection(
    *,
    use_sha256: bool = False,
    adapter_value: PromotionAccountAdapterSettings | None = None,
) -> ConnectionSettings:
    identity = (
        {"expected_platform_store_id_sha256": hashlib.sha256(b"1001").hexdigest()}
        if use_sha256
        else {"expected_platform_store_id": "1001"}
    )
    return ConnectionSettings.model_validate(
        {
            "connection_id": "conn_account",
            "store_id": "st_current",
            "cdp_endpoint": "http://127.0.0.1:9222",
            **identity,
            "real_collection_enabled": True,
            "promotion_account_adapter": adapter_value or adapter(),
        }
    )


def scope(kind: WindowKind, *, version: str = "d4-account-v3") -> Scope:
    business_date = (
        FIXED_LOCAL.date() if kind is WindowKind.TODAY else FIXED_LOCAL.date() - timedelta(days=1)
    )
    return Scope(
        kind=kind,
        business_date=business_date,
        object_type="ACCOUNT_ALL",
        version=version,
    )


def make_collector(
    tmp_path: Path,
    action: Action,
    *,
    connection_value: ConnectionSettings | None = None,
) -> tuple[PromotionAccountCdpCollector, FakePage, FakeSession, FakeConnector]:
    page = FakePage(action)
    session = FakeSession(page)
    connector = FakeConnector(session)
    instance = PromotionAccountCdpCollector(
        connection=connection_value or connection(),
        collection=CollectionSettings(min_interval_seconds=0, collection_timeout_ms=100),
        runtime_root=tmp_path,
        connector=connector,  # type: ignore[arg-type]
    )
    return instance, page, session, connector


def collect(
    instance: PromotionAccountCdpCollector,
    kind: WindowKind,
    *,
    scope_value: Scope | None = None,
) -> Any:
    return asyncio.run(
        instance.collect(
            store_id="st_current",
            dataset_type=DatasetType.PROMOTION_OVERVIEW,
            scope=scope_value or scope(kind),
            limit=50,
            batch_id="batch_account",
        )
    )


def dual_action(
    *,
    today: FakeResponse | None = None,
    yesterday: FakeResponse | None = None,
    identity: FakeResponse | None = None,
    extra: list[FakeResponse] | None = None,
    today_dom: str = "12.34",
    yesterday_dom: str = "6.78",
    date_explanation: str = ("*\u00a0今日截至 14:30 的数据\uff1b昨日截至 14:59 的数据"),
) -> Action:
    return Action(
        [
            *(extra or []),
            today or report_response(WindowKind.TODAY),
            yesterday or report_response(WindowKind.YESTERDAY),
            identity or identity_response(),
        ],
        today_dom=today_dom,
        yesterday_dom=yesterday_dom,
        date_explanation=date_explanation,
    )


def test_dual_reports_are_exactly_captured_and_unknown_body_is_not_read(
    tmp_path: Path,
) -> None:
    unknown = FakeResponse(
        {"secret": "must-not-be-read"},
        path="/unreviewed/report",
        request_body={},
    )
    today = report_response(WindowKind.TODAY)
    yesterday = report_response(WindowKind.YESTERDAY)
    identity = identity_response()
    instance, page, session, _ = make_collector(
        tmp_path,
        dual_action(
            today=today,
            yesterday=yesterday,
            identity=identity,
            extra=[unknown],
        ),
    )

    draft = collect(instance, WindowKind.TODAY)

    assert unknown.body_reads == 0
    assert today.body_reads == yesterday.body_reads == identity.body_reads == 1
    assert page.reload_count == 1
    assert page.listeners == []
    assert session.disconnected is True
    assert draft.parser_version == "d4-account-v3"
    assert draft.scope.version == "d4-account-v3"
    assert draft.metric_window is not None
    assert draft.metric_window.window_complete is False
    assert draft.metric_window.source_finalized is False
    assert draft.metric_window.start == FIXED_LOCAL.replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    assert draft.metric_window.end == FIXED_LOCAL.replace(minute=30, second=17, microsecond=123000)
    assert draft.capture_method == "NETWORK_RESPONSE"
    assert draft.field_sources["metric_window.end"] == "NETWORK_RESPONSE"
    time_evidence = draft.promotion_account_time_evidence
    assert time_evidence is not None
    assert time_evidence.window_kind is WindowKind.TODAY
    assert time_evidence.request_start_date == FIXED_LOCAL.date()
    assert time_evidence.request_end_date == FIXED_LOCAL.date()
    assert time_evidence.request_end_day_hour == 14
    assert time_evidence.response_business_date == FIXED_LOCAL.date()
    assert time_evidence.page_today_cutoff_hhmm == "14:30"
    assert time_evidence.page_yesterday_cutoff_hhmm == "14:59"
    assert time_evidence.response_hourly_row_count == 15
    assert time_evidence.response_first_hour == 0
    assert time_evidence.response_last_hour == 14
    assert time_evidence.page_semantics == "SAME_PERIOD_COMPARISON"
    assert time_evidence.business_timezone == "Asia/Shanghai"
    assert time_evidence.request_source == "NETWORK_RESPONSE"
    assert time_evidence.response_source == "NETWORK_RESPONSE"
    assert time_evidence.page_source == "DOM"
    serialized_time_evidence = json.dumps(time_evidence.model_dump(mode="json"))
    assert "opaque-fixture" not in serialized_time_evidence
    assert "entityId" not in serialized_time_evidence
    assert isinstance(draft.payload, dict)
    assert draft.payload["entity_granularity"] == "ACCOUNT"
    assert draft.payload["metrics"]["spend_cents"] == 1234
    assert "opaque-fixture" not in json.dumps(draft.payload)
    assert draft.quality.dom_check == "MATCHED"


@pytest.mark.parametrize("forged_cutoff", ["14:29", "13:30"])
def test_validator_rejects_today_dom_cutoff_that_differs_from_source_update(
    tmp_path: Path,
    forged_cutoff: str,
) -> None:
    instance, _, _, _ = make_collector(tmp_path, dual_action())
    draft = collect(instance, WindowKind.TODAY)
    evidence = draft.promotion_account_time_evidence
    assert evidence is not None
    tampered = draft.model_copy(
        update={
            "promotion_account_time_evidence": evidence.model_copy(
                update={"page_today_cutoff_hhmm": forged_cutoff}
            )
        }
    )

    report = SnapshotValidator().validate(tampered, synthetic_allowed=False)

    assert report.valid is False
    assert "account time evidence mismatch" in report.errors


def test_yesterday_uses_its_independent_dom_cutoff_and_preserves_missing_reason(
    tmp_path: Path,
) -> None:
    instance, _, _, _ = make_collector(tmp_path, dual_action())

    draft = collect(instance, WindowKind.YESTERDAY)

    assert draft.metric_window is not None
    assert draft.metric_window.kind is WindowKind.YESTERDAY
    assert draft.metric_window.window_complete is False
    assert draft.metric_window.source_finalized is False
    assert draft.metric_window.start.date() == FIXED_LOCAL.date() - timedelta(days=1)
    assert draft.metric_window.end.date() == FIXED_LOCAL.date() - timedelta(days=1)
    assert draft.metric_window.end.timetz().replace(tzinfo=None) == FIXED_LOCAL.replace(
        hour=15, minute=0, second=0, microsecond=0
    ).timetz().replace(tzinfo=None)
    assert draft.capture_method == "MIXED"
    assert draft.field_sources["metric_window.end"] == "DOM"
    time_evidence = draft.promotion_account_time_evidence
    assert time_evidence is not None
    assert time_evidence.window_kind is WindowKind.YESTERDAY
    assert time_evidence.request_start_date == FIXED_LOCAL.date() - timedelta(days=1)
    assert time_evidence.request_end_date == FIXED_LOCAL.date() - timedelta(days=1)
    assert time_evidence.request_end_day_hour == 14
    assert time_evidence.response_business_date == FIXED_LOCAL.date() - timedelta(days=1)
    assert time_evidence.page_today_cutoff_hhmm == "14:30"
    assert time_evidence.page_yesterday_cutoff_hhmm == "14:59"
    assert time_evidence.response_hourly_row_count == 15
    assert time_evidence.response_first_hour == 0
    assert time_evidence.response_last_hour == 14
    assert time_evidence.page_semantics == "SAME_PERIOD_COMPARISON"
    assert draft.source_updated_at is None
    assert "source_updated_at:SOURCE_VALUE_NULL" in draft.missing_fields
    assert isinstance(draft.payload, dict)
    assert draft.payload["source_updated_at_missing_reason"] == "SOURCE_VALUE_NULL"


def test_non_spend_metric_missing_is_preserved_in_snapshot(tmp_path: Path) -> None:
    body = account_payload(WindowKind.TODAY)
    del body["result"]["dailyReportList"][0]["click"]
    del body["result"]["sumReport"]["click"]
    today = FakeResponse(
        body,
        path=MAIN_PATH,
        request_body=request_body(WindowKind.TODAY),
    )
    instance, _, _, _ = make_collector(tmp_path, dual_action(today=today))

    draft = collect(instance, WindowKind.TODAY)

    assert isinstance(draft.payload, dict)
    assert draft.payload["metrics"]["click_count"] is None
    assert draft.payload["metrics"]["missing_reasons"] == {"click_count": "SOURCE_FIELD_MISSING"}
    assert draft.missing_fields == ["metrics.click_count:SOURCE_FIELD_MISSING"]


def test_both_request_contracts_are_validated_even_when_today_is_requested(
    tmp_path: Path,
) -> None:
    bad_yesterday = request_body(WindowKind.YESTERDAY)
    bad_yesterday["unreviewedFilter"] = ""
    rejected = report_response(WindowKind.YESTERDAY, request=bad_yesterday)
    instance, page, session, _ = make_collector(tmp_path, dual_action(yesterday=rejected))

    with pytest.raises(CollectionRejected) as caught:
        collect(instance, WindowKind.TODAY)

    assert caught.value.error_code == "PROMOTION_ACCOUNT_REQUEST_KEYS_MISMATCH"
    assert rejected.body_reads == 0
    assert page.listeners == [] and session.disconnected


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("blockTypes", [True]),
        ("blockTypes", [1.0]),
        ("clientType", True),
        ("queryDimensionType", False),
        ("reportPromotionType", 9.0),
        ("returnLastUpdateTime", 1),
    ],
)
def test_request_constants_use_exact_json_types(tmp_path: Path, field: str, value: object) -> None:
    changed = request_body(WindowKind.TODAY)
    changed[field] = value
    rejected = report_response(WindowKind.TODAY, request=changed)
    instance, _, session, _ = make_collector(tmp_path, dual_action(today=rejected))

    with pytest.raises(CollectionRejected) as caught:
        collect(instance, WindowKind.TODAY)

    assert caught.value.error_code == f"PROMOTION_ACCOUNT_REQUEST_CONSTANT_MISMATCH:{field}"
    assert rejected.body_reads == 0
    assert session.disconnected


@pytest.mark.parametrize("crawler_info", ["", "   ", None, 1, "x" * 4097])
def test_crawler_info_must_be_a_bounded_nonempty_string(
    tmp_path: Path, crawler_info: object
) -> None:
    changed = request_body(WindowKind.TODAY)
    changed["crawlerInfo"] = crawler_info
    rejected = report_response(WindowKind.TODAY, request=changed)
    instance, page, session, _ = make_collector(tmp_path, dual_action(today=rejected))

    with pytest.raises(CollectionRejected) as caught:
        collect(instance, WindowKind.TODAY)

    assert caught.value.error_code == "PROMOTION_ACCOUNT_REQUEST_CRAWLER_INFO_INVALID"
    assert rejected.body_reads == 0
    assert page.listeners == [] and session.disconnected


def test_dates_and_dual_end_hour_are_exact(tmp_path: Path) -> None:
    bad_date = request_body(WindowKind.TODAY)
    bad_date["startDate"] = f"{(FIXED_LOCAL.date() - timedelta(days=1)).isoformat()} 00:00:00"
    rejected = report_response(WindowKind.TODAY, request=bad_date)
    instance, _, _, _ = make_collector(tmp_path, dual_action(today=rejected))
    with pytest.raises(CollectionRejected) as caught:
        collect(instance, WindowKind.TODAY)
    assert caught.value.error_code == "PROMOTION_ACCOUNT_REQUEST_DATE_MISMATCH"
    assert rejected.body_reads == 0

    wrong_hour = request_body(WindowKind.YESTERDAY, end_day_hour=FIXED_LOCAL.hour - 1)
    rejected = report_response(WindowKind.YESTERDAY, request=wrong_hour)
    instance, page, session, _ = make_collector(tmp_path / "hour", dual_action(yesterday=rejected))
    with pytest.raises(CollectionRejected) as caught:
        collect(instance, WindowKind.TODAY)
    assert caught.value.error_code == ("PROMOTION_ACCOUNT_YESTERDAY_DOM_REQUEST_CUTOFF_MISMATCH")
    assert page.listeners == [] and session.disconnected


@pytest.mark.parametrize(
    "bad_date",
    ["2026-09-08", "2026-09-08 01:00:00", "2026-9-08 00:00:00"],
)
def test_request_dates_require_exact_midnight_seconds(tmp_path: Path, bad_date: str) -> None:
    changed = request_body(WindowKind.TODAY)
    changed["startDate"] = bad_date
    rejected = report_response(WindowKind.TODAY, request=changed)
    instance, page, session, _ = make_collector(tmp_path, dual_action(today=rejected))

    with pytest.raises(CollectionRejected) as caught:
        collect(instance, WindowKind.TODAY)

    assert caught.value.error_code in {
        "PROMOTION_ACCOUNT_REQUEST_DATE_INVALID",
        "PROMOTION_ACCOUNT_REQUEST_DATE_NOT_CANONICAL",
    }
    assert rejected.body_reads == 0
    assert page.listeners == [] and session.disconnected


def test_hour_boundary_accepts_independent_not_later_request_hours(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = FIXED_LOCAL.replace(hour=14, minute=59, second=59)
    after = FIXED_LOCAL.replace(hour=15, minute=0, second=0)
    values = [before.astimezone(UTC)] * 5 + [after.astimezone(UTC)] * 20

    def ticking_now() -> datetime:
        return values.pop(0) if len(values) > 1 else values[0]

    monkeypatch.setattr(account_module, "utc_now", ticking_now)
    today = report_response(
        WindowKind.TODAY,
        request=request_body(WindowKind.TODAY, end_day_hour=15),
    )
    yesterday = report_response(
        WindowKind.YESTERDAY,
        request=request_body(WindowKind.YESTERDAY, end_day_hour=14),
    )
    instance, _, _, _ = make_collector(tmp_path, dual_action(today=today, yesterday=yesterday))

    draft = collect(instance, WindowKind.TODAY)

    assert draft.quality.status == "VALID"


def test_today_and_yesterday_cutoffs_can_have_different_hours(tmp_path: Path) -> None:
    yesterday = report_response(
        WindowKind.YESTERDAY,
        request=request_body(WindowKind.YESTERDAY, end_day_hour=13),
    )
    instance, _, _, _ = make_collector(
        tmp_path,
        dual_action(
            yesterday=yesterday,
            date_explanation="*今日截至14:30的数据\uff1b昨日截至13:59的数据",
        ),
    )

    draft = collect(instance, WindowKind.YESTERDAY)

    assert draft.metric_window is not None
    assert (draft.metric_window.end.hour, draft.metric_window.end.minute) == (14, 0)


def test_today_source_cutoff_cannot_be_after_capture(tmp_path: Path) -> None:
    body = account_payload(WindowKind.TODAY)
    future_update = int(FIXED_LOCAL.replace(minute=50, second=0, microsecond=0).timestamp() * 1000)
    body["result"]["reportLastUpdateTime"] = future_update
    body["result"]["lastUpdateTime"] = future_update
    today = FakeResponse(
        body,
        path=MAIN_PATH,
        request_body=request_body(WindowKind.TODAY),
    )
    instance, page, session, _ = make_collector(
        tmp_path,
        dual_action(
            today=today,
            date_explanation="*今日截至14:50的数据\uff1b昨日截至14:59的数据",
        ),
    )

    with pytest.raises(CollectionRejected) as caught:
        collect(instance, WindowKind.TODAY)

    assert caught.value.error_code == "PROMOTION_ACCOUNT_CUTOFF_OUTSIDE_TODAY"
    assert page.listeners == [] and session.disconnected


def test_delayed_response_cannot_cross_shanghai_business_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def crossing_clock() -> datetime:
        nonlocal calls
        calls += 1
        if calls >= 7:
            return FIXED_UTC + timedelta(days=1)
        return FIXED_UTC

    monkeypatch.setattr(account_module, "utc_now", crossing_clock)
    today = report_response(WindowKind.TODAY)
    original_body = today.body

    async def delayed_body() -> bytes:
        for _ in range(8):
            await asyncio.sleep(0)
        return await original_body()

    today.body = delayed_body  # type: ignore[method-assign]
    instance, page, session, _ = make_collector(
        tmp_path,
        dual_action(today=today),
    )

    with pytest.raises(CollectionRejected) as caught:
        collect(instance, WindowKind.TODAY)

    assert caught.value.error_code == ("PROMOTION_ACCOUNT_CAPTURE_CROSSED_DATE_BOUNDARY")
    assert page.listeners == [] and session.disconnected


def test_request_entity_and_independent_identity_support_sha256(tmp_path: Path) -> None:
    bound = connection(use_sha256=True)
    instance, _, _, _ = make_collector(tmp_path, dual_action(), connection_value=bound)

    draft = collect(instance, WindowKind.TODAY)

    assert draft.identity_evidence is not None
    assert draft.identity_evidence.independent_verification_method == ("MERCHANT_PAGE_STATE_SHA256")
    assert draft.identity_evidence.observed_platform_store_id.startswith("sha256:")


def test_wrong_request_entity_is_rejected_before_body_read(tmp_path: Path) -> None:
    changed = request_body(WindowKind.TODAY, entity_id=1002)
    rejected = report_response(WindowKind.TODAY, request=changed)
    instance, page, session, _ = make_collector(tmp_path, dual_action(today=rejected))

    with pytest.raises(CollectionRejected) as caught:
        collect(instance, WindowKind.TODAY)

    assert caught.value.status == "IDENTITY_MISMATCH"
    assert rejected.body_reads == 0
    assert page.listeners == [] and session.disconnected


def test_identity_response_must_match_both_requests(tmp_path: Path) -> None:
    instance, page, session, _ = make_collector(
        tmp_path, dual_action(identity=identity_response(1002))
    )

    with pytest.raises(CollectionRejected) as caught:
        collect(instance, WindowKind.TODAY)

    assert caught.value.status == "IDENTITY_MISMATCH"
    assert page.listeners == [] and session.disconnected


def test_dom_positions_must_each_match_the_corresponding_spend(tmp_path: Path) -> None:
    instance, page, session, _ = make_collector(
        tmp_path, dual_action(today_dom="6.78", yesterday_dom="12.34")
    )

    with pytest.raises(CollectionRejected) as caught:
        collect(instance, WindowKind.TODAY)

    assert caught.value.error_code == "PROMOTION_ACCOUNT_NETWORK_DOM_SPEND_MISMATCH"
    assert page.listeners == [] and session.disconnected


@pytest.mark.parametrize(
    ("explanation", "error_code"),
    [
        (
            "今日截至14:30的数据\uff1b昨日截至14:59的数据",
            "PROMOTION_ACCOUNT_DOM_DATE_EXPLANATION_INVALID",
        ),
        (
            "*今日截至14:31的数据\uff1b昨日截至14:59的数据",
            "PROMOTION_ACCOUNT_TODAY_DOM_SOURCE_CUTOFF_MISMATCH",
        ),
        (
            "*今日截至14:30的数据\uff1b昨日截至13:59的数据",
            "PROMOTION_ACCOUNT_YESTERDAY_DOM_REQUEST_CUTOFF_MISMATCH",
        ),
        (
            "*今日截至14:30的数据\uff1b昨日截至14:16的数据",
            "PROMOTION_ACCOUNT_YESTERDAY_CUTOFF_NOT_END_OF_HOUR",
        ),
    ],
)
def test_dom_date_explanation_is_strict_and_binds_both_cutoffs(
    tmp_path: Path, explanation: str, error_code: str
) -> None:
    instance, page, session, _ = make_collector(
        tmp_path,
        dual_action(date_explanation=explanation),
    )

    with pytest.raises(CollectionRejected) as caught:
        collect(instance, WindowKind.TODAY)

    assert caught.value.error_code == error_code
    assert page.listeners == [] and session.disconnected


def test_equal_today_and_yesterday_spend_is_legitimate(tmp_path: Path) -> None:
    yesterday = report_response(WindowKind.YESTERDAY, spend="12.34")
    instance, _, _, _ = make_collector(
        tmp_path,
        dual_action(yesterday=yesterday, today_dom="12.34", yesterday_dom="12.34"),
    )

    draft = collect(instance, WindowKind.YESTERDAY)

    assert isinstance(draft.payload, dict)
    assert draft.payload["metrics"]["spend_cents"] == 1234


def test_parser_failure_still_removes_listener_and_disconnects(tmp_path: Path) -> None:
    broken = report_response(WindowKind.TODAY, bad_unit=True)
    instance, page, session, _ = make_collector(tmp_path, dual_action(today=broken))

    with pytest.raises(CollectionRejected) as caught:
        collect(instance, WindowKind.TODAY)

    assert caught.value.status == "UNIT_UNVERIFIED"
    assert page.listeners == [] and session.disconnected


@pytest.mark.parametrize(
    ("kind", "version", "error_code"),
    [
        (WindowKind.LAST_7_DAYS, "d4-account-v3", "PROMOTION_ACCOUNT_WINDOW_NOT_ADAPTED"),
        (WindowKind.TODAY, "1", "PROMOTION_ACCOUNT_SCOPE_NOT_ADAPTED"),
    ],
)
def test_window_and_scope_version_are_rejected_before_connection(
    tmp_path: Path, kind: WindowKind, version: str, error_code: str
) -> None:
    instance, _, session, connector = make_collector(tmp_path, Action([]))
    scope_value = Scope(
        kind=kind,
        business_date=FIXED_LOCAL.date(),
        object_type="ACCOUNT_ALL",
        version=version,
    )

    with pytest.raises(CollectionRejected) as caught:
        collect(instance, kind, scope_value=scope_value)

    assert caught.value.error_code == error_code
    assert connector.connect_count == 0
    assert session.disconnected is False


@pytest.mark.parametrize(
    ("field", "value", "error_code"),
    [
        (
            "response_path",
            "/mms-gateway/poseidon/api/report/another",
            "PROMOTION_ACCOUNT_ENDPOINT_MISMATCH:response_path",
        ),
        (
            "dom_today_spend_selector",
            "",
            "PROMOTION_ACCOUNT_EVIDENCE_CONTRACT_MISMATCH",
        ),
        (
            "dom_report_date_explanation_selector",
            "[data-testid='unverified-date-explanation']",
            ("PROMOTION_ACCOUNT_EVIDENCE_FIELD_MISMATCH:dom_report_date_explanation_selector"),
        ),
    ],
)
def test_endpoint_or_dom_evidence_drift_is_rejected_before_connection(
    tmp_path: Path, field: str, value: object, error_code: str
) -> None:
    drifted = adapter().model_copy(update={field: value})
    bound = connection().model_copy(update={"promotion_account_adapter": drifted})
    instance, _, _, connector = make_collector(tmp_path, Action([]), connection_value=bound)

    with pytest.raises(CollectionRejected) as caught:
        collect(instance, WindowKind.TODAY)

    assert caught.value.error_code == error_code
    assert connector.connect_count == 0


def test_page_query_is_rejected_and_session_only_disconnects(tmp_path: Path) -> None:
    instance, page, session, _ = make_collector(tmp_path, Action([]))
    page.url = PAGE_URL + "?hidden=1"

    with pytest.raises(CollectionRejected) as caught:
        collect(instance, WindowKind.TODAY)

    assert caught.value.error_code == "PROMOTION_ACCOUNT_PAGE_SCOPE_UNVERIFIED"
    assert session.disconnected is True
