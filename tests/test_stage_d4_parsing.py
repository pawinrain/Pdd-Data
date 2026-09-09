from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from pdd_data_mcp.browser.promotion_metrics_parsing import parse_promoted_product_response
from pdd_data_mcp.contracts.models import (
    DatasetType,
    PromotedProductEffectMetrics,
    PromotionConfigurationRecord,
)
from pdd_data_mcp.errors import CollectionRejected


def wrapped(unit: str, value: object) -> dict[str, object]:
    return {"unit": unit, "unitCode": 1, "value": value}


def report() -> dict[str, object]:
    return {
        "spend": wrapped("YUAN", "12.34"),
        "orderSpend": wrapped("YUAN", "56.70"),
        "orderSpendRoiUnified": wrapped("PER_ONE", "2.5000"),
        "orderSpendNetRoi": wrapped("PER_ONE", 2),
        "netOrderNum": 3,
        "orderNum": 4,
        "gmv": wrapped("YUAN", 100),
        "netGmv": wrapped("YUAN", "90.01"),
        "impression": 500,
        "click": 25,
        "settlementRoi": wrapped("PER_ONE", "1.2300"),
        "settlementOrder": 2,
    }


def row(
    *,
    ad_id: int,
    plan_id: int,
    goods_id: int,
    mall_id: int = 9001,
    report_info: object | None = None,
) -> dict[str, object]:
    return {
        "adId": ad_id,
        "planId": plan_id,
        "goodsId": goods_id,
        "mallId": mall_id,
        "reportInfo": report() if report_info is None else report_info,
        "maxCost": wrapped("YUAN", "20.00"),
        "targetRoi": wrapped("PER_ONE", "3.200"),
        "agentBid": None,
        "adStatus": 1,
    }


def response(rows: list[dict[str, object]], *, summary: object | None = None) -> bytes:
    timestamp = int(datetime(2026, 9, 7, 3, 4, 5, 678000, tzinfo=UTC).timestamp() * 1000)
    value = {
        "success": True,
        "result": {
            "adInfos": rows,
            "sumReportInfo": report() if summary is None else summary,
            "reportLastUpdateTime": timestamp,
        },
    }
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def parse(raw: bytes) -> Any:
    return parse_promoted_product_response(
        raw,
        observed_at=datetime(2026, 9, 7, 3, 5, tzinfo=UTC),
        expected_store_id="9001",
    )


def test_parses_verified_promoted_product_metrics_configuration_and_summary() -> None:
    parsed = parse(
        response(
            [
                row(ad_id=101, plan_id=201, goods_id=301),
                # Repeated plans are allowed: planId is an association, not row granularity.
                row(ad_id=102, plan_id=201, goods_id=302),
            ]
        )
    )

    assert parsed.total_observed is None
    assert parsed.observed_platform_store_id == "9001"
    assert parsed.source_updated_at == datetime(2026, 9, 7, 3, 4, 5, 678000, tzinfo=UTC)
    assert len(parsed.metric_records) == len(parsed.configuration_records) == 2
    metric = parsed.metric_records[0]
    assert metric.entity_granularity == "PROMOTED_PRODUCT"
    assert (metric.ad_id, metric.campaign_id, metric.platform_product_id) == (
        "101",
        "201",
        "301",
    )
    assert metric.metrics.spend_cents == 1234
    assert metric.metrics.order_spend_cents == 5670
    assert metric.metrics.order_spend_roi == "2.5"
    assert metric.metrics.order_spend_net_roi == "2"
    assert metric.metrics.gmv_cents == 10_000
    assert metric.metrics.net_gmv_cents == 9001
    assert metric.metrics.impression_count == 500
    assert metric.metrics.missing_reasons == {}
    assert parsed.summary_metrics.settlement_roi == "1.23"

    configuration = parsed.configuration_records[0]
    assert configuration.entity_granularity == "PROMOTED_PRODUCT"
    assert configuration.max_cost_cents == 2000
    assert configuration.target_roi == "3.2"
    assert configuration.agent_bid is None
    assert configuration.ad_status == 1
    assert configuration.missing_reasons == {"agent_bid": "SOURCE_VALUE_NULL"}


def test_missing_and_null_report_values_remain_null_with_exact_reasons() -> None:
    partial = report()
    partial.pop("click")
    partial["settlementOrder"] = None
    parsed = parse(response([row(ad_id=101, plan_id=201, goods_id=301, report_info=partial)]))
    metrics = parsed.metric_records[0].metrics
    assert metrics.click_count is None
    assert metrics.settlement_order_count is None
    assert metrics.missing_reasons == {
        "click_count": "SOURCE_FIELD_MISSING",
        "settlement_order_count": "SOURCE_VALUE_NULL",
    }


def test_missing_report_container_marks_every_metric_without_inventing_zero() -> None:
    item = row(ad_id=101, plan_id=201, goods_id=301)
    item.pop("reportInfo")
    parsed = parse(response([item]))
    metrics = parsed.metric_records[0].metrics
    assert metrics.spend_cents is None
    assert metrics.order_count is None
    assert len(metrics.missing_reasons) == 12
    assert set(metrics.missing_reasons.values()) == {"SOURCE_FIELD_MISSING"}


@pytest.mark.parametrize(
    ("field", "replacement", "error_code"),
    [
        ("spend", wrapped("PER_ONE", "1.00"), "METRIC_UNIT_MISMATCH:spend"),
        ("click", wrapped("COUNT", 1), "INVALID_COUNT:click"),
    ],
)
def test_rejects_unverified_units_or_changed_count_shape(
    field: str, replacement: object, error_code: str
) -> None:
    changed = report()
    changed[field] = replacement
    with pytest.raises(CollectionRejected) as caught:
        parse(response([row(ad_id=101, plan_id=201, goods_id=301, report_info=changed)]))
    assert caught.value.status == "UNIT_UNVERIFIED"
    assert caught.value.error_code == error_code


def test_agent_bid_is_per_one_not_money() -> None:
    item = row(ad_id=101, plan_id=201, goods_id=301)
    item["agentBid"] = wrapped("PER_ONE", "0.0750")
    parsed = parse(response([item]))
    assert parsed.configuration_records[0].agent_bid == "0.075"

    item["agentBid"] = wrapped("YUAN", "0.0750")
    with pytest.raises(CollectionRejected) as caught:
        parse(response([item]))
    assert caught.value.error_code == "METRIC_UNIT_MISMATCH:agentBid"


def test_requires_millisecond_report_timestamp_in_verified_range() -> None:
    value = json.loads(response([row(ad_id=101, plan_id=201, goods_id=301)]))
    value["result"]["reportLastUpdateTime"] //= 1000
    with pytest.raises(CollectionRejected) as caught:
        parse(json.dumps(value).encode())
    assert caught.value.status == "TIME_SCOPE_UNVERIFIED"
    assert caught.value.error_code == "REPORT_LAST_UPDATE_TIME_NOT_UNIX_MILLISECONDS"

    value["result"]["reportLastUpdateTime"] = 4_102_444_800_000  # 2100-01-01 UTC is valid.
    assert parse(json.dumps(value).encode()).source_updated_at.year == 2100
    value["result"]["reportLastUpdateTime"] = 4_133_980_800_000  # 2101-01-01 UTC.
    with pytest.raises(CollectionRejected) as caught:
        parse(json.dumps(value).encode())
    assert caught.value.error_code == "REPORT_LAST_UPDATE_TIME_OUT_OF_RANGE"


def test_verifies_every_row_store_and_accepts_fingerprint_binding() -> None:
    store_hash = hashlib.sha256(b"9001").hexdigest()
    parsed = parse_promoted_product_response(
        response([row(ad_id=101, plan_id=201, goods_id=301)]),
        observed_at=datetime(2026, 9, 7, tzinfo=UTC),
        expected_store_id="",
        expected_store_id_sha256=store_hash,
    )
    assert parsed.observed_platform_store_id == f"sha256:{store_hash}"

    with pytest.raises(CollectionRejected) as caught:
        parse(
            response(
                [
                    row(ad_id=101, plan_id=201, goods_id=301),
                    row(ad_id=102, plan_id=202, goods_id=302, mall_id=9002),
                ]
            )
        )
    assert caught.value.status == "IDENTITY_MISMATCH"


def test_rejects_duplicate_ad_row_but_never_emits_campaign_records() -> None:
    with pytest.raises(CollectionRejected) as caught:
        parse(
            response(
                [
                    row(ad_id=101, plan_id=201, goods_id=301),
                    row(ad_id=101, plan_id=202, goods_id=302),
                ]
            )
        )
    assert caught.value.error_code == "DUPLICATE_PROMOTED_PRODUCT_ROW"
    assert DatasetType.CAMPAIGN_METRICS.value == "campaign_metrics"
    assert DatasetType.PROMOTION_CONFIGURATION.value == "promotion_configuration"


def test_contracts_require_reason_for_every_null_and_no_reason_for_value() -> None:
    with pytest.raises(ValidationError):
        PromotedProductEffectMetrics()
    with pytest.raises(ValidationError):
        PromotionConfigurationRecord(
            ad_id="101",
            campaign_id="201",
            platform_product_id="301",
            max_cost_cents=100,
            target_roi="2",
            agent_bid="1",
            ad_status=1,
            configuration_observed_at=datetime(2026, 9, 7, tzinfo=UTC),
            missing_reasons={"max_cost_cents": "SOURCE_VALUE_NULL"},
        )
