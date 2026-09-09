from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pdd_data_mcp.browser.dynamic_digit_font import (
    BoundDynamicDigitFont,
    fetch_dynamic_digit_font,
)
from pdd_data_mcp.browser.parsing import decode_json_object
from pdd_data_mcp.contracts.models import (
    MetricWindow,
    MissingReason,
    ProductBusinessMetricCandidate,
    ProductBusinessMetricCandidateRecord,
    ProductBusinessMetricCandidates,
    ProductBusinessSourceField,
    ProductBusinessYesterdayCandidateEvidence,
    WindowKind,
)
from pdd_data_mcp.errors import CollectionRejected

PRODUCT_BUSINESS_LIST_PATH = "/sydney/api/goodsDataShow/queryGoodsDetailVOListForMMS"
PRODUCT_BUSINESS_READY_PATH = "/sydney/api/goodsDataShow/queryGoodsReadyDate"
PRODUCT_BUSINESS_SCOPE_VERSION = "product-business-yesterday-v1"
MAX_PRODUCT_BUSINESS_RESPONSE_BYTES = 1_048_576

_LIST_REQUEST_FIELDS = frozenset(
    {
        "actVs",
        "crawlerInfo",
        "endDate",
        "pageNum",
        "pageSize",
        "queryType",
        "sortCol",
        "sortType",
        "startDate",
    }
)
_SOURCE_TO_OUTPUT: dict[ProductBusinessSourceField, str] = {
    "payOrdrUsrCnt": "paying_buyer_count",
    "payOrdrCnt": "paid_order_count",
    "payOrdrGoodsQty": "paid_goods_quantity",
    "payOrdrAmt": "paid_amount",
    "goodsUv": "goods_visitor_count",
    "goodsPv": "goods_page_view_count",
}
_MISSING = object()


@dataclass(frozen=True)
class DecodedProductBusinessMetricCandidates:
    """Numeric-format candidates decoded without asserting business units."""

    paying_buyer_count: str | None
    paid_order_count: str | None
    paid_goods_quantity: str | None
    paid_amount: str | None
    goods_visitor_count: str | None
    goods_page_view_count: str | None


@dataclass(frozen=True)
class DecodedProductBusinessMetricCandidateRecord:
    platform_product_id: str
    metrics: DecodedProductBusinessMetricCandidates


@dataclass(frozen=True)
class ParsedProductBusinessResponse:
    records: list[ProductBusinessMetricCandidateRecord]
    metric_window: MetricWindow
    page_num_candidate: str
    page_size_candidate: str
    total_num_candidate: str
    result_timestamp_candidate: str
    pagination_semantics_verified: Literal[False] = False
    result_timestamp_unit_verified: Literal[False] = False
    source_updated_at: None = None
    identity_verified: Literal[False] = False
    source_classification_verified: Literal[False] = False
    decoded_records: list[DecodedProductBusinessMetricCandidateRecord] | None = None
    font_sha256: str | None = None
    font_profile_version: str | None = None
    font_source_path: str | None = None
    numeric_format_decoded: bool = False
    unit_semantics_verified: Literal[False] = False


@dataclass(frozen=True)
class ParsedProductBusinessReadyResponse:
    ready_date_candidate: str
    date_semantics_verified: Literal[False] = False
    source_updated_at: None = None


def _require_endpoint(*, response_path: str, request_method: str, expected_path: str) -> None:
    if response_path != expected_path or request_method != "POST":
        raise CollectionRejected(
            "ADAPTER_UNVERIFIED", "PRODUCT_BUSINESS_ENDPOINT_OR_METHOD_MISMATCH"
        )


def _required_object(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CollectionRejected("PLATFORM_ERROR", f"{field}_NOT_OBJECT")
    return value


def _required_string(value: object, field: str, *, max_length: int = 4096) -> str:
    if not isinstance(value, str) or len(value) > max_length:
        raise CollectionRejected("PLATFORM_ERROR", f"{field}_NOT_BOUNDED_STRING")
    return value


def _number_candidate(value: object, field: str, *, nonnegative: bool = False) -> str:
    if type(value) is int:
        parsed = Decimal(value)
    elif isinstance(value, Decimal):
        parsed = value
    else:
        raise CollectionRejected("PLATFORM_ERROR", f"{field}_NOT_JSON_NUMBER")
    if not parsed.is_finite() or (nonnegative and parsed < 0):
        raise CollectionRejected("PLATFORM_ERROR", f"{field}_INVALID_JSON_NUMBER")
    normalized = format(parsed, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def _positive_integral_id(value: object, field: str) -> str:
    candidate = _number_candidate(value, field, nonnegative=True)
    try:
        parsed = Decimal(candidate)
    except ValueError as exc:  # pragma: no cover - candidate is produced by Decimal above
        raise CollectionRejected("PLATFORM_ERROR", f"{field}_INVALID_ID") from exc
    if parsed <= 0 or parsed != parsed.to_integral_value():
        raise CollectionRejected("PLATFORM_ERROR", f"{field}_INVALID_ID")
    return str(int(parsed))


def _require_success(payload: dict[str, Any]) -> object:
    if payload.get("success", _MISSING) is not True:
        raise CollectionRejected("PLATFORM_ERROR", "PRODUCT_BUSINESS_RESPONSE_NOT_SUCCESS")
    _number_candidate(payload.get("errorCode", _MISSING), "errorCode")
    _required_string(payload.get("errorMsg", _MISSING), "errorMsg")
    if "result" not in payload:
        raise CollectionRejected("PLATFORM_ERROR", "RESULT_MISSING")
    return payload["result"]


def _validate_list_request(
    request: object, evidence: ProductBusinessYesterdayCandidateEvidence
) -> tuple[str, str]:
    value = _required_object(request, "request")
    missing = _LIST_REQUEST_FIELDS - set(value)
    if missing:
        raise CollectionRejected("PLATFORM_ERROR", "PRODUCT_BUSINESS_REQUEST_FIELDS_MISSING")
    _required_string(value["crawlerInfo"], "crawlerInfo", max_length=16_384)
    for field in ("actVs", "queryType", "sortCol", "sortType"):
        _number_candidate(value[field], field)
    page_num = _number_candidate(value["pageNum"], "pageNum", nonnegative=True)
    page_size = _number_candidate(value["pageSize"], "pageSize", nonnegative=True)
    start_date = _required_string(value["startDate"], "startDate", max_length=64)
    end_date = _required_string(value["endDate"], "endDate", max_length=64)
    if start_date != evidence.request_start_date_text or end_date != evidence.request_end_date_text:
        raise CollectionRejected(
            "TIME_SCOPE_UNVERIFIED", "PRODUCT_BUSINESS_REQUEST_DATE_EVIDENCE_MISMATCH"
        )
    return page_num, page_size


def _metric_candidate(
    row: dict[str, Any], source_field: ProductBusinessSourceField
) -> ProductBusinessMetricCandidate:
    raw = row.get(source_field, _MISSING)
    missing_reason: MissingReason | None = None
    source_value: str | None
    if raw is _MISSING:
        source_value = None
        missing_reason = "SOURCE_FIELD_MISSING"
    elif raw is None:
        source_value = None
        missing_reason = "SOURCE_VALUE_NULL"
    elif isinstance(raw, str):
        source_value = raw
    else:
        raise CollectionRejected(
            "UNIT_UNVERIFIED", f"PRODUCT_BUSINESS_SOURCE_VALUE_NOT_STRING:{source_field}"
        )
    try:
        return ProductBusinessMetricCandidate(
            source_field=source_field,
            source_value=source_value,
            missing_reason=missing_reason,
        )
    except ValueError as exc:
        raise CollectionRejected(
            "UNIT_UNVERIFIED", f"PRODUCT_BUSINESS_SOURCE_VALUE_UNSAFE:{source_field}"
        ) from exc


def _parse_row(
    value: object,
    *,
    index: int,
    evidence: ProductBusinessYesterdayCandidateEvidence,
    observed_at: datetime,
) -> ProductBusinessMetricCandidateRecord:
    row = _required_object(value, f"goodsDetailList[{index}]")
    platform_product_id = _positive_integral_id(row.get("goodsId", _MISSING), "goodsId")
    _required_string(row.get("goodsName", _MISSING), "goodsName")
    _number_candidate(row.get("goodsStatus", _MISSING), "goodsStatus")
    stat_date = _required_string(row.get("statDate", _MISSING), "statDate", max_length=64)
    if stat_date != evidence.response_stat_date_text:
        raise CollectionRejected(
            "TIME_SCOPE_UNVERIFIED", "PRODUCT_BUSINESS_ROW_STAT_DATE_EVIDENCE_MISMATCH"
        )
    candidates = {
        output_field: _metric_candidate(row, source_field)
        for source_field, output_field in _SOURCE_TO_OUTPUT.items()
    }
    return ProductBusinessMetricCandidateRecord(
        business_date=evidence.business_date,
        platform_product_id=platform_product_id,
        metrics=ProductBusinessMetricCandidates.model_validate(candidates, strict=True),
        observed_at=observed_at,
    )


def _decode_record(
    record: ProductBusinessMetricCandidateRecord,
    font_decoder: BoundDynamicDigitFont,
) -> DecodedProductBusinessMetricCandidateRecord:
    def decode(field: str, *, kind: Literal["COUNT", "DECIMAL"]) -> str | None:
        candidate = getattr(record.metrics, field)
        source_value = candidate.source_value
        if source_value is None:
            return None
        return font_decoder.decode(source_value, kind=kind)

    return DecodedProductBusinessMetricCandidateRecord(
        platform_product_id=record.platform_product_id,
        metrics=DecodedProductBusinessMetricCandidates(
            paying_buyer_count=decode("paying_buyer_count", kind="COUNT"),
            paid_order_count=decode("paid_order_count", kind="COUNT"),
            paid_goods_quantity=decode("paid_goods_quantity", kind="COUNT"),
            paid_amount=decode("paid_amount", kind="DECIMAL"),
            goods_visitor_count=decode("goods_visitor_count", kind="COUNT"),
            goods_page_view_count=decode("goods_page_view_count", kind="COUNT"),
        ),
    )


def parse_product_business_list_response(
    raw: bytes,
    *,
    request: object,
    response_path: str,
    request_method: str,
    evidence: ProductBusinessYesterdayCandidateEvidence,
    observed_at: datetime,
) -> ParsedProductBusinessResponse:
    """Parse one exact product-business list response without promoting unknown semantics.

    The six observed metric values remain source strings with explicit false
    format/unit flags.  This raw parser never accepts a font decoder.  The
    fetch-first candidate helper is separate.  Pagination scalars and the
    result timestamp are candidates only; this function never calls them
    coverage or ``source_updated_at``.
    """

    _require_endpoint(
        response_path=response_path,
        request_method=request_method,
        expected_path=PRODUCT_BUSINESS_LIST_PATH,
    )
    if len(raw) > MAX_PRODUCT_BUSINESS_RESPONSE_BYTES:
        raise CollectionRejected("PLATFORM_ERROR", "PRODUCT_BUSINESS_RESPONSE_TOO_LARGE")
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "OBSERVED_AT_REQUIRES_TIMEZONE")
    zone = ZoneInfo(evidence.business_timezone)
    if observed_at < evidence.window_end or observed_at.astimezone(
        zone
    ).date() != evidence.business_date + timedelta(days=1):
        raise CollectionRejected(
            "TIME_SCOPE_UNVERIFIED", "PRODUCT_BUSINESS_NOT_EXACT_YESTERDAY_AT_OBSERVATION"
        )
    page_num, page_size = _validate_list_request(request, evidence)
    payload = decode_json_object(raw)
    result = _required_object(_require_success(payload), "result")
    _number_candidate(result.get("delayData", _MISSING), "delayData")
    total_num = _number_candidate(result.get("totalNum", _MISSING), "totalNum", nonnegative=True)
    result_timestamp = _number_candidate(
        result.get("timestamp", _MISSING), "timestamp", nonnegative=True
    )
    rows = result.get("goodsDetailList", _MISSING)
    if not isinstance(rows, list):
        raise CollectionRejected("PLATFORM_ERROR", "GOODS_DETAIL_LIST_NOT_ARRAY")
    records = [
        _parse_row(item, index=index, evidence=evidence, observed_at=observed_at)
        for index, item in enumerate(rows)
    ]
    product_ids = [record.platform_product_id for record in records]
    if len(product_ids) != len(set(product_ids)):
        raise CollectionRejected("DATA_MISMATCH", "DUPLICATE_PRODUCT_BUSINESS_ROW")
    return ParsedProductBusinessResponse(
        records=records,
        metric_window=MetricWindow(
            kind=WindowKind.YESTERDAY,
            timezone=evidence.business_timezone,
            start=evidence.window_start,
            end=evidence.window_end,
            window_complete=True,
            source_finalized=None,
        ),
        page_num_candidate=page_num,
        page_size_candidate=page_size,
        total_num_candidate=total_num,
        result_timestamp_candidate=result_timestamp,
    )


def fetch_first_parse_product_business_list_response(
    raw: bytes,
    *,
    font_url: str,
    request: object,
    response_path: str,
    request_method: str,
    evidence: ProductBusinessYesterdayCandidateEvidence,
    observed_at: datetime,
    _font_fetcher: Callable[[str], BoundDynamicDigitFont] = fetch_dynamic_digit_font,
) -> ParsedProductBusinessResponse:
    """Fetch the page's dynamic font first, then decode a candidate sidecar.

    ``font_url`` is intended for a future internal page collector, not an MCP
    parameter.  The raw source candidates remain unchanged, and this helper
    does not promote numeric decoding into verified business-unit semantics.
    """

    font_decoder = _font_fetcher(font_url)
    parsed = parse_product_business_list_response(
        raw,
        request=request,
        response_path=response_path,
        request_method=request_method,
        evidence=evidence,
        observed_at=observed_at,
    )
    decoded_records = [_decode_record(record, font_decoder) for record in parsed.records]
    decoded_any_value = any(
        value is not None for record in decoded_records for value in vars(record.metrics).values()
    )
    return replace(
        parsed,
        decoded_records=decoded_records,
        font_sha256=font_decoder.sha256,
        font_profile_version=font_decoder.profile_version,
        font_source_path=font_decoder.source_path,
        numeric_format_decoded=decoded_any_value,
    )


def parse_product_business_ready_response(
    raw: bytes,
    *,
    request: object,
    response_path: str,
    request_method: str,
) -> ParsedProductBusinessReadyResponse:
    """Parse only the verified ready-response shape, not its date semantics."""

    _require_endpoint(
        response_path=response_path,
        request_method=request_method,
        expected_path=PRODUCT_BUSINESS_READY_PATH,
    )
    if len(raw) > MAX_PRODUCT_BUSINESS_RESPONSE_BYTES:
        raise CollectionRejected("PLATFORM_ERROR", "PRODUCT_BUSINESS_RESPONSE_TOO_LARGE")
    request_object = _required_object(request, "request")
    _required_string(request_object.get("crawlerInfo", _MISSING), "crawlerInfo", max_length=16_384)
    payload = decode_json_object(raw)
    result = _require_success(payload)
    ready_date = _required_string(result, "result", max_length=64)
    return ParsedProductBusinessReadyResponse(ready_date_candidate=ready_date)


__all__ = [
    "MAX_PRODUCT_BUSINESS_RESPONSE_BYTES",
    "PRODUCT_BUSINESS_LIST_PATH",
    "PRODUCT_BUSINESS_READY_PATH",
    "PRODUCT_BUSINESS_SCOPE_VERSION",
    "DecodedProductBusinessMetricCandidateRecord",
    "DecodedProductBusinessMetricCandidates",
    "ParsedProductBusinessReadyResponse",
    "ParsedProductBusinessResponse",
    "fetch_first_parse_product_business_list_response",
    "parse_product_business_list_response",
    "parse_product_business_ready_response",
]
