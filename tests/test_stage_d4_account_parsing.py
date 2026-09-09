from __future__ import annotations

import copy
import json
from datetime import UTC, date, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from pdd_data_mcp.browser.promotion_account_parsing import (
    parse_promotion_account_hourly_evidence,
    parse_promotion_account_response,
)
from pdd_data_mcp.config import PromotionAccountAdapterSettings
from pdd_data_mcp.contracts.models import PromotionAccountMetricPayload, WindowKind
from pdd_data_mcp.errors import CollectionRejected

BUSINESS_DATE = date(2026, 9, 8)
OBSERVED_AT = datetime(2026, 9, 8, 7, 12, tzinfo=UTC)
SOURCE_UPDATED_MS = 1_788_857_400_123


def wrapped(unit: str, value: object) -> dict[str, object]:
    return {"unit": unit, "unitCode": 1, "value": value}


def metrics() -> dict[str, object]:
    return {
        "spend": wrapped("YUAN", "12.34"),
        "orderSpend": wrapped("YUAN", "56.70"),
        "gmv": wrapped("YUAN", 100),
        "netGmv": wrapped("YUAN", "90.01"),
        "orderSpendRoiUnified": wrapped("PER_ONE", "2.5000"),
        "orderSpendNetRoi": wrapped("PER_ONE", 2),
        "settlementRoi": wrapped("PER_ONE", "1.2300"),
        "netOrderNum": 3,
        "orderNum": 4,
        "impression": 500,
        "click": 25,
        "settlementOrder": 2,
    }


def response(
    *,
    business_date: str = "2026-09-08 00:00:00",
    outer_update: object = SOURCE_UPDATED_MS,
    secondary_update: object = SOURCE_UPDATED_MS,
    daily_metrics: dict[str, object] | None = None,
    summary_metrics: dict[str, object] | None = None,
    success: object = True,
) -> dict[str, Any]:
    daily_values = metrics() if daily_metrics is None else daily_metrics
    summary_values = copy.deepcopy(daily_values) if summary_metrics is None else summary_metrics
    return {
        "success": success,
        "result": {
            "dailyReportList": [
                {
                    "date": business_date,
                    **daily_values,
                }
            ],
            "sumReport": summary_values,
            "reportLastUpdateTime": outer_update,
            "lastUpdateTime": secondary_update,
        },
    }


def raw(value: dict[str, Any]) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def parse(value: dict[str, Any], window: WindowKind = WindowKind.TODAY) -> Any:
    return parse_promotion_account_response(
        raw(value),
        observed_at=OBSERVED_AT,
        expected_business_date=BUSINESS_DATE,
        window_kind=window,
    )


def hourly_raw(hours: list[object]) -> bytes:
    value = response()
    result = value["result"]
    assert isinstance(result, dict)
    result["hourlyReportList"] = [
        {"hour": hour, "spend": {"value": "not-returned"}} for hour in hours
    ]
    return raw(value)


def adapter_values() -> dict[str, object]:
    return {
        "verified": True,
        "data_source": "NETWORK_RESPONSE",
        "target_page_url": "https://yingxiao.pinduoduo.com/mains/promotionOverview",
        "response_host": "yingxiao.pinduoduo.com",
        "response_path": "/mms-gateway/poseidon/api/report/queryHourlyRangeReport",
        "response_method": "POST",
        "response_http_status": 200,
        "response_content_type": "application/json",
        "business_success_path": "success",
        "business_success_value": True,
        "identity_source": "IDENTITY_RESPONSE",
        "identity_response_host": "yingxiao.pinduoduo.com",
        "identity_response_path": "/mms-gateway/venus/api/user/info",
        "identity_response_method": "POST",
        "identity_response_http_status": 200,
        "identity_business_success_path": "success",
        "identity_business_success_value": True,
        "identity_platform_store_id_path": "result.mallId",
        "identity_verification_reference": "account-identity-evidence",
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
        "request_end_day_hour_semantics": "INCLUSIVE_HOURLY_ROW_INDEX_WITH_DOM_CUTOFF",
        "dom_today_spend_selector": "[data-testid='today-spend']",
        "dom_yesterday_spend_selector": "[data-testid='yesterday-spend']",
        "dom_report_date_explanation_selector": ("div[class*='ReportDateExplain_content__']"),
        "dom_spend_unit": "CNY",
    }


def test_parses_strict_account_payload_with_all_twelve_metrics() -> None:
    parsed = parse(response())

    assert isinstance(parsed, PromotionAccountMetricPayload)
    assert parsed.entity_granularity == "ACCOUNT"
    assert parsed.business_date == BUSINESS_DATE
    assert parsed.observed_at == OBSERVED_AT
    assert parsed.source_updated_at == datetime(2026, 9, 8, 8, 50, 0, 123000, tzinfo=UTC)
    assert parsed.source_updated_at_missing_reason is None
    assert parsed.metrics.model_dump() == {
        "spend_cents": 1234,
        "order_spend_cents": 5670,
        "order_spend_roi": "2.5",
        "order_spend_net_roi": "2",
        "net_order_count": 3,
        "order_count": 4,
        "gmv_cents": 10_000,
        "net_gmv_cents": 9001,
        "impression_count": 500,
        "click_count": 25,
        "settlement_roi": "1.23",
        "settlement_order_count": 2,
        "missing_reasons": {},
    }


def test_hourly_evidence_requires_complete_inclusive_zero_based_range() -> None:
    parsed = parse_promotion_account_hourly_evidence(
        hourly_raw(list(range(12))),
        expected_end_day_hour=11,
    )

    assert parsed.row_count == 12
    assert parsed.first_hour == 0
    assert parsed.last_hour == 11


@pytest.mark.parametrize(
    "hours",
    [
        list(range(11)),
        [*range(11), 10],
        [*range(11), 12],
        [False, *range(1, 12)],
    ],
)
def test_hourly_evidence_rejects_missing_duplicate_or_invalid_hours(
    hours: list[object],
) -> None:
    with pytest.raises(CollectionRejected, match="HOURLY_REPORT"):
        parse_promotion_account_hourly_evidence(
            hourly_raw(hours),
            expected_end_day_hour=11,
        )


def test_yesterday_retains_explicit_null_source_update_reason() -> None:
    parsed = parse(
        response(outer_update=None, secondary_update=None),
        window=WindowKind.YESTERDAY,
    )

    assert parsed.source_updated_at is None
    assert parsed.source_updated_at_missing_reason == "SOURCE_VALUE_NULL"


@pytest.mark.parametrize(
    ("window", "primary", "secondary", "error_code"),
    [
        (WindowKind.TODAY, None, SOURCE_UPDATED_MS, "TODAY_SOURCE_UPDATED_AT_NULL"),
        (WindowKind.TODAY, SOURCE_UPDATED_MS, None, "TODAY_SOURCE_UPDATED_AT_NULL"),
        (
            WindowKind.YESTERDAY,
            SOURCE_UPDATED_MS,
            None,
            "YESTERDAY_SOURCE_UPDATED_AT_NOT_ALL_NULL",
        ),
        (
            WindowKind.YESTERDAY,
            None,
            SOURCE_UPDATED_MS,
            "YESTERDAY_SOURCE_UPDATED_AT_NOT_ALL_NULL",
        ),
        (
            WindowKind.TODAY,
            SOURCE_UPDATED_MS,
            SOURCE_UPDATED_MS + 1,
            "SOURCE_UPDATED_AT_MISMATCH",
        ),
        (
            WindowKind.TODAY,
            SOURCE_UPDATED_MS // 1000,
            SOURCE_UPDATED_MS // 1000,
            "REPORTLASTUPDATETIME_NOT_UNIX_MILLISECONDS",
        ),
    ],
)
def test_source_update_pair_is_conservative(
    window: WindowKind,
    primary: object,
    secondary: object,
    error_code: str,
) -> None:
    with pytest.raises(CollectionRejected) as caught:
        parse(
            response(
                outer_update=primary,
                secondary_update=secondary,
            ),
            window=window,
        )
    assert caught.value.error_code == error_code


@pytest.mark.parametrize("location", ["primary", "secondary"])
def test_missing_source_update_key_is_not_treated_as_nullable(location: str) -> None:
    value = response(outer_update=None, secondary_update=None)
    result = value["result"]
    if location == "primary":
        del result["reportLastUpdateTime"]
    elif location == "secondary":
        del result["lastUpdateTime"]
    with pytest.raises(CollectionRejected) as caught:
        parse(value, window=WindowKind.YESTERDAY)
    assert caught.value.error_code == "SOURCE_UPDATED_AT_FIELD_MISSING"


@pytest.mark.parametrize("count", [0, 2])
def test_requires_exactly_one_daily_row(count: int) -> None:
    value = response()
    value["result"]["dailyReportList"] = value["result"]["dailyReportList"] * count
    with pytest.raises(CollectionRejected) as caught:
        parse(value)
    assert caught.value.error_code == "DAILY_REPORT_LIST_NOT_SINGLE_DAY"


@pytest.mark.parametrize(
    ("observed_date", "error_code"),
    [
        ("2026-09-07 00:00:00", "ACCOUNT_BUSINESS_DATE_MISMATCH"),
        ("2026-09-08", "ACCOUNT_BUSINESS_DATE_INVALID"),
        ("2026-09-08 01:00:00", "ACCOUNT_BUSINESS_DATE_INVALID"),
    ],
)
def test_daily_business_date_must_be_canonical_and_match_the_request(
    observed_date: str, error_code: str
) -> None:
    with pytest.raises(CollectionRejected) as caught:
        parse(response(business_date=observed_date))
    assert caught.value.status == "TIME_SCOPE_UNVERIFIED"
    assert caught.value.error_code == error_code


@pytest.mark.parametrize(
    ("field", "daily_value", "summary_value"),
    [
        ("click", True, 1),
        ("orderNum", 1, 1.0),
        ("spend", wrapped("YUAN", "1.00"), wrapped("YUAN", "1")),
    ],
)
def test_daily_and_summary_comparison_is_recursive_and_type_exact(
    field: str, daily_value: object, summary_value: object
) -> None:
    daily = metrics()
    summary = copy.deepcopy(daily)
    daily[field] = daily_value
    summary[field] = summary_value
    with pytest.raises(CollectionRejected) as caught:
        parse(response(daily_metrics=daily, summary_metrics=summary))
    assert caught.value.status == "DATA_MISMATCH"
    assert caught.value.error_code == f"DAILY_SUM_REPORT_MISMATCH:{field}"


def test_matching_missing_and_null_metrics_remain_null_with_exact_reasons() -> None:
    changed = metrics()
    changed.pop("spend")
    changed["click"] = None

    parsed = parse(response(daily_metrics=changed))

    assert parsed.metrics.spend_cents is None
    assert parsed.metrics.click_count is None
    assert parsed.metrics.missing_reasons == {
        "spend_cents": "SOURCE_FIELD_MISSING",
        "click_count": "SOURCE_VALUE_NULL",
    }
    assert parsed.metrics.order_count == 4


def test_matching_wrappers_preserve_missing_and_null_inner_values() -> None:
    changed = metrics()
    changed["spend"] = {"unit": "YUAN", "unitCode": 1}
    changed["orderSpendRoiUnified"] = {
        "unit": "PER_ONE",
        "unitCode": 1,
        "value": None,
    }

    parsed = parse(response(daily_metrics=changed))

    assert parsed.metrics.spend_cents is None
    assert parsed.metrics.order_spend_roi is None
    assert parsed.metrics.missing_reasons == {
        "spend_cents": "SOURCE_FIELD_MISSING",
        "order_spend_roi": "SOURCE_VALUE_NULL",
    }


@pytest.mark.parametrize(
    ("daily_has_field", "daily_value", "summary_has_field", "summary_value"),
    [
        (False, None, True, None),
        (True, None, False, None),
        (False, None, True, wrapped("YUAN", "0")),
        (True, wrapped("YUAN", "0"), False, None),
    ],
)
def test_mixed_missing_null_or_present_states_are_data_mismatch(
    daily_has_field: bool,
    daily_value: object,
    summary_has_field: bool,
    summary_value: object,
) -> None:
    daily = metrics()
    summary = copy.deepcopy(daily)
    daily.pop("spend")
    summary.pop("spend")
    if daily_has_field:
        daily["spend"] = daily_value
    if summary_has_field:
        summary["spend"] = summary_value

    with pytest.raises(CollectionRejected) as caught:
        parse(response(daily_metrics=daily, summary_metrics=summary))
    assert caught.value.status == "DATA_MISMATCH"
    assert caught.value.error_code == "DAILY_SUM_REPORT_MISMATCH:spend"


@pytest.mark.parametrize(
    ("field", "replacement", "error_code"),
    [
        ("spend", wrapped("PER_ONE", "1"), "METRIC_UNIT_MISMATCH:spend"),
        (
            "orderSpendRoiUnified",
            wrapped("YUAN", "1"),
            "METRIC_UNIT_MISMATCH:orderSpendRoiUnified",
        ),
        ("gmv", {"unitCode": 1, "value": "1"}, "METRIC_UNIT_MISMATCH:gmv"),
        ("settlementOrder", True, "INVALID_COUNT:settlementOrder"),
    ],
)
def test_rejects_unverified_metric_units_and_non_exact_counts(
    field: str, replacement: object, error_code: str
) -> None:
    changed = metrics()
    changed[field] = replacement
    with pytest.raises(CollectionRejected) as caught:
        parse(response(daily_metrics=changed))
    assert caught.value.status == "UNIT_UNVERIFIED"
    assert caught.value.error_code == error_code


def test_account_contract_forbids_open_fields_and_enforces_missing_reason_pair() -> None:
    parsed = parse(response())
    values = parsed.model_dump(mode="python")
    values["unexpected_metric_bag"] = {}
    with pytest.raises(ValidationError):
        PromotionAccountMetricPayload.model_validate(values, strict=True)

    values.pop("unexpected_metric_bag")
    values["source_updated_at"] = None
    with pytest.raises(ValidationError):
        PromotionAccountMetricPayload.model_validate(values, strict=True)


def test_account_schema_documents_normalized_business_date_and_is_closed() -> None:
    schema = PromotionAccountMetricPayload.model_json_schema()
    assert schema["additionalProperties"] is False
    business_date = schema["properties"]["business_date"]
    assert business_date["format"] == "date"
    assert "YYYY-MM-DD 00:00:00" in business_date["description"]


def test_verified_account_adapter_locks_evidence_contract_and_dom_selectors() -> None:
    configured = PromotionAccountAdapterSettings.model_validate(adapter_values(), strict=True)
    assert configured.supported_windows == ["TODAY", "YESTERDAY"]
    assert configured.parser_version == "d4-account-v3"
    assert configured.request_end_day_hour_semantics == "INCLUSIVE_HOURLY_ROW_INDEX_WITH_DOM_CUTOFF"

    for field, changed in (
        ("summary_path", "result.otherSummary"),
        ("request_end_day_hour_field", "fixedHour"),
        ("request_date_format", ""),
        ("response_date_format", ""),
        ("secondary_source_updated_at_path", "result.otherLastUpdateTime"),
        ("dom_report_date_explanation_selector", ""),
        ("dom_report_date_explanation_selector", "div.report-date-explanation"),
        ("parser_version", "d4-account-v1"),
        ("supported_windows", ["TODAY", "TODAY"]),
    ):
        values = adapter_values()
        values[field] = changed
        with pytest.raises(ValidationError):
            PromotionAccountAdapterSettings.model_validate(values, strict=True)

    values = adapter_values()
    values["dom_yesterday_spend_selector"] = values["dom_today_spend_selector"]
    with pytest.raises(ValidationError):
        PromotionAccountAdapterSettings.model_validate(values, strict=True)
