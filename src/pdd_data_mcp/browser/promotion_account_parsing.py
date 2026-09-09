from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pdd_data_mcp.browser.parsing import decode_json_object
from pdd_data_mcp.contracts.models import (
    MissingReason,
    PromotedProductEffectMetrics,
    PromotionAccountMetricPayload,
    WindowKind,
)
from pdd_data_mcp.errors import CollectionRejected

_MISSING = object()
_MONEY_FIELDS = {
    "spend": "spend_cents",
    "orderSpend": "order_spend_cents",
    "gmv": "gmv_cents",
    "netGmv": "net_gmv_cents",
}
_RATIO_FIELDS = {
    "orderSpendRoiUnified": "order_spend_roi",
    "orderSpendNetRoi": "order_spend_net_roi",
    "settlementRoi": "settlement_roi",
}
_COUNT_FIELDS = {
    "netOrderNum": "net_order_count",
    "orderNum": "order_count",
    "impression": "impression_count",
    "click": "click_count",
    "settlementOrder": "settlement_order_count",
}
_SELECTED_FIELDS = tuple((*_MONEY_FIELDS, *_RATIO_FIELDS, *_COUNT_FIELDS))


@dataclass(frozen=True)
class PromotionAccountHourlyEvidence:
    row_count: int
    first_hour: int
    last_hour: int


def _strict_equal(left: object, right: object) -> bool:
    """Compare decoded JSON without Python's bool/int or int/Decimal coercions."""

    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        if not isinstance(right, dict) or left.keys() != right.keys():
            return False
        return all(_strict_equal(value, right[key]) for key, value in left.items())
    if isinstance(left, list):
        if not isinstance(right, list) or len(left) != len(right):
            return False
        return all(_strict_equal(a, b) for a, b in zip(left, right, strict=True))
    return left == right


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool | float) or not isinstance(value, int | str | Decimal):
        raise CollectionRejected("UNIT_UNVERIFIED", f"INVALID_NUMBER_TYPE:{field}")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise CollectionRejected("UNIT_UNVERIFIED", f"INVALID_NUMBER:{field}") from exc
    if not parsed.is_finite() or parsed < 0:
        raise CollectionRejected("UNIT_UNVERIFIED", f"INVALID_NUMBER:{field}")
    return parsed


def _wrapped_value(
    parent: dict[str, Any], source_field: str, *, unit: Literal["YUAN", "PER_ONE"]
) -> tuple[int | str | None, MissingReason | None]:
    raw = parent.get(source_field, _MISSING)
    if not isinstance(raw, dict):
        raise CollectionRejected("UNIT_UNVERIFIED", f"METRIC_NOT_OBJECT:{source_field}")
    if raw.get("unit", _MISSING) != unit or type(raw.get("unitCode", _MISSING)) is not int:
        raise CollectionRejected("UNIT_UNVERIFIED", f"METRIC_UNIT_MISMATCH:{source_field}")
    if raw["unitCode"] != 1:
        raise CollectionRejected("UNIT_UNVERIFIED", f"METRIC_UNIT_CODE_MISMATCH:{source_field}")
    value = raw.get("value", _MISSING)
    if value is _MISSING:
        return None, "SOURCE_FIELD_MISSING"
    if value is None:
        return None, "SOURCE_VALUE_NULL"
    parsed = _decimal(value, source_field)
    if unit == "YUAN":
        cents = parsed * Decimal(100)
        if cents != cents.to_integral_value():
            raise CollectionRejected("UNIT_UNVERIFIED", f"MONEY_NOT_INTEGER_CENTS:{source_field}")
        return int(cents), None
    result = format(parsed, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return result or "0", None


def _count_value(parent: dict[str, Any], source_field: str) -> int:
    raw = parent.get(source_field, _MISSING)
    if type(raw) is not int or raw < 0:
        raise CollectionRejected("UNIT_UNVERIFIED", f"INVALID_COUNT:{source_field}")
    return raw


def _parse_metrics(summary: dict[str, Any]) -> PromotedProductEffectMetrics:
    parsed: dict[str, int | str | None] = {}
    missing_reasons: dict[str, MissingReason] = {}
    for source_field, output_field in _MONEY_FIELDS.items():
        raw = summary.get(source_field, _MISSING)
        if raw is _MISSING or raw is None:
            parsed[output_field] = None
            missing_reasons[output_field] = (
                "SOURCE_FIELD_MISSING" if raw is _MISSING else "SOURCE_VALUE_NULL"
            )
        else:
            value, reason = _wrapped_value(summary, source_field, unit="YUAN")
            parsed[output_field] = value
            if reason is not None:
                missing_reasons[output_field] = reason
    for source_field, output_field in _RATIO_FIELDS.items():
        raw = summary.get(source_field, _MISSING)
        if raw is _MISSING or raw is None:
            parsed[output_field] = None
            missing_reasons[output_field] = (
                "SOURCE_FIELD_MISSING" if raw is _MISSING else "SOURCE_VALUE_NULL"
            )
        else:
            value, reason = _wrapped_value(summary, source_field, unit="PER_ONE")
            parsed[output_field] = value
            if reason is not None:
                missing_reasons[output_field] = reason
    for source_field, output_field in _COUNT_FIELDS.items():
        raw = summary.get(source_field, _MISSING)
        if raw is _MISSING or raw is None:
            parsed[output_field] = None
            missing_reasons[output_field] = (
                "SOURCE_FIELD_MISSING" if raw is _MISSING else "SOURCE_VALUE_NULL"
            )
        else:
            parsed[output_field] = _count_value(summary, source_field)
    return PromotedProductEffectMetrics.model_validate(
        {**parsed, "missing_reasons": missing_reasons}, strict=True
    )


def _parse_business_date(value: object) -> date:
    if not isinstance(value, str):
        raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "ACCOUNT_BUSINESS_DATE_INVALID")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "ACCOUNT_BUSINESS_DATE_INVALID") from exc
    if parsed.strftime("%Y-%m-%d %H:%M:%S") != value or any(
        (parsed.hour, parsed.minute, parsed.second, parsed.microsecond)
    ):
        raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "ACCOUNT_BUSINESS_DATE_INVALID")
    return parsed.date()


def _unix_milliseconds(value: object, field: str) -> datetime:
    if type(value) is not int or len(str(value)) != 13:
        raise CollectionRejected("TIME_SCOPE_UNVERIFIED", f"{field.upper()}_NOT_UNIX_MILLISECONDS")
    seconds, milliseconds = divmod(value, 1000)
    try:
        parsed = datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=milliseconds * 1000)
    except (OverflowError, OSError, ValueError) as exc:
        raise CollectionRejected("TIME_SCOPE_UNVERIFIED", f"{field.upper()}_OUT_OF_RANGE") from exc
    if not 2020 <= parsed.year <= 2100:
        raise CollectionRejected("TIME_SCOPE_UNVERIFIED", f"{field.upper()}_OUT_OF_RANGE")
    return parsed


def _source_updated_at(
    result: dict[str, Any], *, window_kind: WindowKind
) -> tuple[datetime | None, Literal["SOURCE_VALUE_NULL"] | None]:
    primary = result.get("reportLastUpdateTime", _MISSING)
    secondary = result.get("lastUpdateTime", _MISSING)
    if primary is _MISSING or secondary is _MISSING:
        raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "SOURCE_UPDATED_AT_FIELD_MISSING")
    if window_kind is WindowKind.YESTERDAY:
        if primary is None and secondary is None:
            return None, "SOURCE_VALUE_NULL"
        raise CollectionRejected(
            "TIME_SCOPE_UNVERIFIED", "YESTERDAY_SOURCE_UPDATED_AT_NOT_ALL_NULL"
        )
    if primary is None or secondary is None:
        raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "TODAY_SOURCE_UPDATED_AT_NULL")
    parsed_primary = _unix_milliseconds(primary, "reportLastUpdateTime")
    parsed_secondary = _unix_milliseconds(secondary, "lastUpdateTime")
    if parsed_primary != parsed_secondary:
        raise CollectionRejected("DATA_MISMATCH", "SOURCE_UPDATED_AT_MISMATCH")
    return parsed_primary, None


def parse_promotion_account_response(
    raw: bytes,
    *,
    observed_at: datetime,
    expected_business_date: date,
    window_kind: WindowKind,
) -> PromotionAccountMetricPayload:
    """Parse one verified account/day response from the hourly dual-report endpoint."""

    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "OBSERVED_AT_REQUIRES_TIMEZONE")
    if type(expected_business_date) is not date:
        raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "EXPECTED_BUSINESS_DATE_INVALID")
    if not isinstance(window_kind, WindowKind) or window_kind not in {
        WindowKind.TODAY,
        WindowKind.YESTERDAY,
    }:
        raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "ACCOUNT_WINDOW_UNSUPPORTED")

    payload = decode_json_object(raw)
    if payload.get("success", _MISSING) is not True:
        raise CollectionRejected("PLATFORM_ERROR", "BUSINESS_RESPONSE_NOT_SUCCESS")
    result = payload.get("result", _MISSING)
    if not isinstance(result, dict):
        raise CollectionRejected("PLATFORM_ERROR", "ACCOUNT_RESULT_NOT_OBJECT")
    daily_rows = result.get("dailyReportList", _MISSING)
    if not isinstance(daily_rows, list):
        raise CollectionRejected("PLATFORM_ERROR", "DAILY_REPORT_LIST_NOT_ARRAY")
    if len(daily_rows) != 1:
        raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "DAILY_REPORT_LIST_NOT_SINGLE_DAY")
    daily = daily_rows[0]
    if not isinstance(daily, dict):
        raise CollectionRejected("PLATFORM_ERROR", "DAILY_REPORT_NOT_OBJECT")
    business_date = _parse_business_date(daily.get("date", _MISSING))
    if business_date != expected_business_date:
        raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "ACCOUNT_BUSINESS_DATE_MISMATCH")
    summary = result.get("sumReport", _MISSING)
    if not isinstance(summary, dict):
        raise CollectionRejected("PLATFORM_ERROR", "SUM_REPORT_NOT_OBJECT")

    for field in _SELECTED_FIELDS:
        if not _strict_equal(daily.get(field, _MISSING), summary.get(field, _MISSING)):
            raise CollectionRejected("DATA_MISMATCH", f"DAILY_SUM_REPORT_MISMATCH:{field}")
    metrics = _parse_metrics(summary)
    source_updated_at, missing_reason = _source_updated_at(result, window_kind=window_kind)
    return PromotionAccountMetricPayload(
        business_date=business_date,
        metrics=metrics,
        observed_at=observed_at,
        source_updated_at=source_updated_at,
        source_updated_at_missing_reason=missing_reason,
    )


def parse_promotion_account_hourly_evidence(
    raw: bytes,
    *,
    expected_end_day_hour: int,
) -> PromotionAccountHourlyEvidence:
    """Verify that the response contains every inclusive hour requested, exactly once."""

    if type(expected_end_day_hour) is not int or not 0 <= expected_end_day_hour <= 23:
        raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "EXPECTED_END_DAY_HOUR_INVALID")
    payload = decode_json_object(raw)
    if payload.get("success", _MISSING) is not True:
        raise CollectionRejected("PLATFORM_ERROR", "BUSINESS_RESPONSE_NOT_SUCCESS")
    result = payload.get("result", _MISSING)
    if not isinstance(result, dict):
        raise CollectionRejected("PLATFORM_ERROR", "ACCOUNT_RESULT_NOT_OBJECT")
    hourly_rows = result.get("hourlyReportList", _MISSING)
    if not isinstance(hourly_rows, list):
        raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "HOURLY_REPORT_LIST_NOT_ARRAY")
    expected_hours = list(range(expected_end_day_hour + 1))
    observed_hours: list[int] = []
    for index, row in enumerate(hourly_rows):
        if not isinstance(row, dict):
            raise CollectionRejected("TIME_SCOPE_UNVERIFIED", f"HOURLY_REPORT_NOT_OBJECT:{index}")
        hour = row.get("hour", _MISSING)
        if type(hour) is not int or not 0 <= hour <= 23:
            raise CollectionRejected("TIME_SCOPE_UNVERIFIED", f"HOURLY_REPORT_HOUR_INVALID:{index}")
        observed_hours.append(hour)
    if observed_hours != expected_hours:
        raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "HOURLY_REPORT_RANGE_MISMATCH")
    return PromotionAccountHourlyEvidence(
        row_count=len(observed_hours),
        first_hour=observed_hours[0],
        last_hour=observed_hours[-1],
    )


__all__ = [
    "PromotionAccountHourlyEvidence",
    "parse_promotion_account_hourly_evidence",
    "parse_promotion_account_response",
]
