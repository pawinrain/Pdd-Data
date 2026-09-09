from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from conftest import make_repository, write_test_config
from pydantic import ValidationError

import pdd_data_mcp.browser.product_business_parsing as parsing_module
import pdd_data_mcp.contracts as contracts
from pdd_data_mcp.application import PddDataService
from pdd_data_mcp.browser.dynamic_digit_font import BoundDynamicDigitFont
from pdd_data_mcp.browser.product_business_parsing import (
    PRODUCT_BUSINESS_LIST_PATH,
    PRODUCT_BUSINESS_READY_PATH,
    fetch_first_parse_product_business_list_response,
    parse_product_business_list_response,
    parse_product_business_ready_response,
)
from pdd_data_mcp.collectors import SyntheticCollector
from pdd_data_mcp.contracts.models import (
    DatasetType,
    ProductBusinessMetricCandidate,
    ProductBusinessMetricCandidates,
    ProductBusinessYesterdayCandidateEvidence,
    Scope,
    WindowKind,
)
from pdd_data_mcp.errors import CollectionRejected
from pdd_data_mcp.schema_export import SCHEMAS
from pdd_data_mcp.validation import SnapshotValidator

ZONE = ZoneInfo("Asia/Shanghai")
BUSINESS_DATE = date(2026, 9, 7)
OBSERVED_AT = datetime(2026, 9, 8, 9, 30, tzinfo=ZONE)


def evidence(**changes: object) -> ProductBusinessYesterdayCandidateEvidence:
    values: dict[str, object] = {
        "business_date": BUSINESS_DATE,
        # The format is deliberately opaque: only later live evidence may bind it.
        "request_start_date_text": "verified-request-start",
        "request_end_date_text": "verified-request-end",
        "response_stat_date_text": "verified-row-date",
        "window_start": datetime(2026, 9, 7, tzinfo=ZONE),
        "window_end": datetime(2026, 9, 8, tzinfo=ZONE),
        "date_semantics_verified": True,
    }
    values.update(changes)
    return ProductBusinessYesterdayCandidateEvidence.model_validate(values, strict=True)


def request(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "actVs": 0,
        "crawlerInfo": "opaque-crawler-info",
        "endDate": "verified-request-end",
        "pageNum": 1,
        "pageSize": 10,
        "queryType": 0,
        "sortCol": 0,
        "sortType": 0,
        "startDate": "verified-request-start",
    }
    value.update(changes)
    return value


def row(*, goods_id: int = 301, **changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "goodsId": goods_id,
        "goodsName": "not-exported-product-name",
        "goodsStatus": 1,
        "statDate": "verified-row-date",
        "payOrdrUsrCnt": "1",
        "payOrdrCnt": "2",
        "payOrdrGoodsQty": "3件",
        "payOrdrAmt": "4.50元",
        "goodsUv": "5",
        "goodsPv": "6",
    }
    value.update(changes)
    return value


def response(rows: list[dict[str, object]]) -> bytes:
    return json.dumps(
        {
            "success": True,
            "errorCode": 0,
            "errorMsg": "",
            "ignoredUnverifiedTopLevel": "must-not-be-exported",
            "result": {
                "delayData": 0,
                "goodsDetailList": rows,
                "totalNum": len(rows),
                "timestamp": 1_789_000_000_000,
                "ignoredUnverifiedResult": "must-not-be-exported",
            },
        },
        separators=(",", ":"),
    ).encode()


def parse(raw: bytes, **changes: object) -> Any:
    values: dict[str, object] = {
        "request": request(),
        "response_path": PRODUCT_BUSINESS_LIST_PATH,
        "request_method": "POST",
        "evidence": evidence(),
        "observed_at": OBSERVED_AT,
    }
    values.update(changes)
    return parse_product_business_list_response(raw, **values)  # type: ignore[arg-type]


def test_additive_dataset_and_yesterday_contract_do_not_relabel_product_metrics() -> None:
    assert DatasetType.PRODUCT_BUSINESS_METRICS.value == "product_business_metrics"
    assert DatasetType.PRODUCT_METRICS.value == "product_metrics"
    assert DatasetType.PRODUCT_BUSINESS_METRICS is not DatasetType.PRODUCT_METRICS

    with pytest.raises(ValidationError):
        evidence(window_kind=WindowKind.LAST_7_DAYS)
    with pytest.raises(ValidationError):
        evidence(date_semantics_verified=False)
    with pytest.raises(ValidationError, match="one full natural day"):
        evidence(window_end=datetime(2026, 9, 7, 23, 59, 59, tzinfo=ZONE))


def test_candidate_contract_names_cannot_be_mistaken_for_persisted_records() -> None:
    assert contracts.ProductBusinessMetricCandidateRecord.__name__.endswith("CandidateRecord")
    assert contracts.ProductBusinessYesterdayCandidateEvidence.__name__.endswith(
        "CandidateEvidence"
    )
    assert not hasattr(contracts, "ProductBusinessMetricRecord")
    assert not hasattr(contracts, "ProductBusinessYesterdayEvidence")
    assert "product-business-metric-candidate.schema.json" in SCHEMAS
    assert "product-business-metric-record.schema.json" not in SCHEMAS


def test_parses_only_product_daily_rows_and_leaves_all_metric_semantics_unverified() -> None:
    parsed = parse(response([row(), row(goods_id=302)]))

    assert len(parsed.records) == 2
    record = parsed.records[0]
    assert record.entity_granularity == "PRODUCT"
    assert record.result_granularity == "DAILY_AGGREGATE"
    assert record.window_kind is WindowKind.YESTERDAY
    assert record.business_date == BUSINESS_DATE
    assert record.platform_product_id == "301"
    assert record.source_updated_at is None
    assert record.source_classification_verified is False
    assert record.metrics.paying_buyer_count.source_field == "payOrdrUsrCnt"
    assert record.metrics.paying_buyer_count.source_value == "1"
    assert record.metrics.paid_goods_quantity.source_value == "3件"
    assert record.metrics.paid_amount.source_value == "4.50元"
    for candidate in record.metrics:
        metric = candidate[1]
        assert metric.numeric_format_verified is False
        assert metric.unit_semantics_verified is False
    assert parsed.metric_window.kind is WindowKind.YESTERDAY
    assert parsed.metric_window.start == datetime(2026, 9, 7, tzinfo=ZONE)
    assert parsed.metric_window.end == datetime(2026, 9, 8, tzinfo=ZONE)
    assert parsed.metric_window.window_complete is True
    assert parsed.metric_window.source_finalized is None
    assert parsed.total_num_candidate == "2"
    assert parsed.page_num_candidate == "1"
    assert parsed.page_size_candidate == "10"
    assert parsed.pagination_semantics_verified is False
    assert parsed.result_timestamp_candidate == "1789000000000"
    assert parsed.result_timestamp_unit_verified is False
    assert parsed.source_updated_at is None
    assert parsed.identity_verified is False
    serialized = json.dumps([item.model_dump(mode="json") for item in parsed.records])
    assert "not-exported-product-name" not in serialized
    assert "must-not-be-exported" not in serialized


def test_missing_and_null_metric_strings_stay_null_and_never_become_zero() -> None:
    missing = row()
    missing.pop("goodsUv")
    missing["goodsPv"] = None

    metrics = parse(response([missing])).records[0].metrics

    assert metrics.goods_visitor_count.source_value is None
    assert metrics.goods_visitor_count.missing_reason == "SOURCE_FIELD_MISSING"
    assert metrics.goods_page_view_count.source_value is None
    assert metrics.goods_page_view_count.missing_reason == "SOURCE_VALUE_NULL"
    assert metrics.paid_order_count.source_value == "2"
    assert metrics.paid_order_count.missing_reason is None


def _bound_font() -> BoundDynamicDigitFont:
    digits = MappingProxyType(
        {
            0xE6EB: "0",
            0xE378: "1",
            0xE551: "2",
            0xE3C1: "3",
            0xE6EA: "4",
            0xEBF4: "5",
            0xE9E5: "6",
            0xE6B6: "7",
            0xEF35: "8",
            0xEFBA: "9",
        }
    )
    return BoundDynamicDigitFont(
        source_path=(
            "/webspider-sdk-api/"
            "11111111111111111111111111111111-22222222222222222222222222222222.ttf"
        ),
        sha256="3861d3322b1267200735d9f5cb48bc6d833a75ee3eb468b54f89abe7e0038eb7",
        profile_version="pdd-digit-font-3861d332-v1",
        codepoint_to_digit=digits,
    )


def _encoded_row(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "payOrdrUsrCnt": "\ue378\ue551",
        "payOrdrCnt": "\ue3c1\ue6ea",
        "payOrdrGoodsQty": "\uebf4\ue9e5",
        "payOrdrAmt": "\ue6b6\uef35.\uefba\ue6eb",
        "goodsUv": "\ue378\ue6eb\ue6eb",
        "goodsPv": "\ue551\ue6eb\ue6eb",
    }
    values.update(changes)
    return row(**values)


def _parse_with_font(
    raw: bytes,
    *,
    decoder: BoundDynamicDigitFont,
) -> Any:
    return fetch_first_parse_product_business_list_response(
        raw,
        font_url=(
            "https://pfile.pddpic.com/webspider-sdk-api/"
            "11111111111111111111111111111111-22222222222222222222222222222222.ttf"
        ),
        request=request(),
        response_path=PRODUCT_BUSINESS_LIST_PATH,
        request_method="POST",
        evidence=evidence(),
        observed_at=OBSERVED_AT,
        _font_fetcher=lambda _url: decoder,
    )


def test_optional_bound_font_decodes_candidates_without_relabeling_source_or_units() -> None:
    decoder = _bound_font()
    parsed = _parse_with_font(response([_encoded_row()]), decoder=decoder)

    assert parsed.numeric_format_decoded is True
    assert parsed.unit_semantics_verified is False
    assert parsed.font_profile_version == "pdd-digit-font-3861d332-v1"
    assert parsed.decoded_records is not None
    decoded = parsed.decoded_records[0].metrics
    assert decoded.paying_buyer_count == "12"
    assert decoded.paid_order_count == "34"
    assert decoded.paid_goods_quantity == "56"
    assert decoded.paid_amount == "78.90"
    assert decoded.goods_visitor_count == "100"
    assert decoded.goods_page_view_count == "200"
    assert parsed.records[0].metrics.paid_amount.source_value == "\ue6b6\uef35.\uefba\ue6eb"
    assert parsed.records[0].metrics.paid_amount.numeric_format_verified is False
    assert parsed.records[0].metrics.paid_amount.unit_semantics_verified is False


def test_font_decode_rejects_whole_response_on_first_unmapped_pua() -> None:
    decoder = _bound_font()
    with pytest.raises(CollectionRejected) as caught:
        _parse_with_font(
            response([_encoded_row(), _encoded_row(goods_id=302, goodsPv="\ue777")]),
            decoder=decoder,
        )

    assert caught.value.status == "UNIT_UNVERIFIED"
    assert caught.value.error_code == "DYNAMIC_FONT_CODEPOINT_UNMAPPED"


def test_empty_result_does_not_claim_that_any_numeric_value_was_decoded() -> None:
    parsed = _parse_with_font(response([]), decoder=_bound_font())

    assert parsed.decoded_records == []
    assert parsed.numeric_format_decoded is False


def test_current_font_profile_rejects_values_from_the_prior_rotated_font() -> None:
    decoder = _bound_font()
    with pytest.raises(CollectionRejected) as caught:
        _parse_with_font(
            response([_encoded_row(goodsUv="\ue809")]),
            decoder=decoder,
        )

    assert caught.value.error_code == "DYNAMIC_FONT_CODEPOINT_UNMAPPED"


def test_font_fetch_happens_before_response_parse_and_failure_stops_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    real_decode_json = parsing_module.decode_json_object

    def tracked_decode_json(raw: bytes) -> dict[str, Any]:
        events.append("list-parse")
        return real_decode_json(raw)

    monkeypatch.setattr(parsing_module, "decode_json_object", tracked_decode_json)

    def fetched(_url: str) -> BoundDynamicDigitFont:
        events.append("font-fetch")
        return _bound_font()

    fetch_first_parse_product_business_list_response(
        response([_encoded_row()]),
        font_url=(
            "https://pfile.pddpic.com/webspider-sdk-api/"
            "11111111111111111111111111111111-22222222222222222222222222222222.ttf"
        ),
        request=request(),
        response_path=PRODUCT_BUSINESS_LIST_PATH,
        request_method="POST",
        evidence=evidence(),
        observed_at=OBSERVED_AT,
        _font_fetcher=fetched,
    )
    assert events == ["font-fetch", "list-parse"]

    events.clear()

    def failed_fetch(_url: str) -> BoundDynamicDigitFont:
        events.append("font-fetch")
        raise CollectionRejected("UNIT_UNVERIFIED", "DYNAMIC_FONT_FETCH_FAILED")

    with pytest.raises(CollectionRejected, match="DYNAMIC_FONT_FETCH_FAILED"):
        fetch_first_parse_product_business_list_response(
            response([_encoded_row()]),
            font_url=(
                "https://pfile.pddpic.com/webspider-sdk-api/"
                "11111111111111111111111111111111-22222222222222222222222222222222.ttf"
            ),
            request=request(),
            response_path=PRODUCT_BUSINESS_LIST_PATH,
            request_method="POST",
            evidence=evidence(),
            observed_at=OBSERVED_AT,
            _font_fetcher=failed_fetch,
        )
    assert events == ["font-fetch"]


@pytest.mark.parametrize(
    ("changes", "status", "error_code"),
    [
        ({"response_path": "/unverified"}, "ADAPTER_UNVERIFIED", "ENDPOINT_OR_METHOD"),
        ({"request_method": "GET"}, "ADAPTER_UNVERIFIED", "ENDPOINT_OR_METHOD"),
        (
            {"request": request(startDate="different")},
            "TIME_SCOPE_UNVERIFIED",
            "REQUEST_DATE_EVIDENCE_MISMATCH",
        ),
        (
            {"observed_at": datetime(2026, 9, 9, 9, tzinfo=ZONE)},
            "TIME_SCOPE_UNVERIFIED",
            "NOT_EXACT_YESTERDAY",
        ),
    ],
)
def test_endpoint_and_yesterday_evidence_are_hard_gates(
    changes: dict[str, object], status: str, error_code: str
) -> None:
    with pytest.raises(CollectionRejected) as caught:
        parse(response([row()]), **changes)

    assert caught.value.status == status
    assert error_code in caught.value.error_code


def test_row_date_wrong_metric_type_and_duplicate_product_are_rejected() -> None:
    with pytest.raises(CollectionRejected) as caught:
        parse(response([row(statDate="other-date")]))
    assert caught.value.status == "TIME_SCOPE_UNVERIFIED"
    assert "ROW_STAT_DATE_EVIDENCE_MISMATCH" in caught.value.error_code

    with pytest.raises(CollectionRejected) as caught:
        parse(response([row(goodsUv=0)]))
    assert caught.value.status == "UNIT_UNVERIFIED"
    assert caught.value.error_code.endswith(":goodsUv")

    with pytest.raises(CollectionRejected) as caught:
        parse(response([row(), row()]))
    assert caught.value.status == "DATA_MISMATCH"
    assert caught.value.error_code == "DUPLICATE_PRODUCT_BUSINESS_ROW"


def test_source_mapping_and_null_reason_contracts_are_closed() -> None:
    metric = ProductBusinessMetricCandidate(
        source_field="payOrdrUsrCnt",
        source_value="1",
    )
    values = {
        "paying_buyer_count": metric,
        "paid_order_count": metric,
        "paid_goods_quantity": ProductBusinessMetricCandidate(
            source_field="payOrdrGoodsQty", source_value="1"
        ),
        "paid_amount": ProductBusinessMetricCandidate(source_field="payOrdrAmt", source_value="1"),
        "goods_visitor_count": ProductBusinessMetricCandidate(
            source_field="goodsUv", source_value="1"
        ),
        "goods_page_view_count": ProductBusinessMetricCandidate(
            source_field="goodsPv", source_value="1"
        ),
    }
    with pytest.raises(ValidationError, match="source-field mapping mismatch"):
        ProductBusinessMetricCandidates.model_validate(values, strict=True)
    with pytest.raises(ValidationError, match="requires an exact missing reason"):
        ProductBusinessMetricCandidate(source_field="goodsUv")
    with pytest.raises(ValidationError, match="must not have a missing reason"):
        ProductBusinessMetricCandidate(
            source_field="goodsUv",
            source_value="0",
            missing_reason="SOURCE_VALUE_NULL",
        )


def test_ready_date_is_only_an_unverified_candidate_and_never_source_update() -> None:
    raw = json.dumps(
        {"success": True, "errorCode": 0, "errorMsg": "", "result": "opaque-ready-date"}
    ).encode()

    parsed = parse_product_business_ready_response(
        raw,
        request={"crawlerInfo": "opaque"},
        response_path=PRODUCT_BUSINESS_READY_PATH,
        request_method="POST",
    )

    assert parsed.ready_date_candidate == "opaque-ready-date"
    assert parsed.date_semantics_verified is False
    assert parsed.source_updated_at is None


def test_capability_and_collection_remain_unavailable_without_an_adapter(tmp_path: Path) -> None:
    config = write_test_config(tmp_path / "config.toml", test_mode=False)
    real_connection = config.connections[0].model_copy(
        update={
            "real_collection_enabled": True,
            "cdp_endpoint": "http://127.0.0.1:9222",
        }
    )
    real_config = config.model_copy(update={"connections": [real_connection]})
    repository = make_repository(real_config)

    class MustNotRunCollector:
        async def collect(self, **_kwargs: object) -> Any:
            raise AssertionError("unavailable product business dataset must not reach a collector")

    service = PddDataService(
        config=real_config,
        repository=repository,
        synthetic_collector=SyntheticCollector(),
        validator=SnapshotValidator(),
        real_collectors={real_connection.connection_id: MustNotRunCollector()},
    )
    capabilities = service.capabilities()
    detail = capabilities.dataset_details["product_business_metrics"]
    assert capabilities.datasets["product_business_metrics"] == "UNAVAILABLE"
    assert detail.entity_granularity == "PRODUCT"
    assert detail.supported_window_kinds == []
    assert detail.verified is False
    assert detail.current_only is False
    assert detail.limitation is not None
    assert "YESTERDAY" in detail.limitation
    assert "7-day" in detail.limitation

    result = asyncio.run(
        service.collect_snapshot(
            connection_id=real_connection.connection_id,
            dataset_type=DatasetType.PRODUCT_BUSINESS_METRICS,
            scope=Scope(
                kind=WindowKind.YESTERDAY,
                business_date=BUSINESS_DATE,
                object_type="PRODUCT_ALL",
                version="product-business-yesterday-v1",
            ),
            limit=50,
            idempotency_key="must-not-collect-product-business",
        )
    )
    assert result.status == "DATASET_UNVERIFIED"
    assert result.committed is False
    assert result.error_code == "PRODUCT_BUSINESS_COLLECTION_NOT_ADAPTED"
