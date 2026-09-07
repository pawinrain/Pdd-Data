from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pdd_data_mcp.config import PromotionAdapterSettings
from pdd_data_mcp.errors import CollectionRejected

MISSING_DOM_VALUES = frozenset({"", "--", "-", "—", "暂无数据", "加载中"})
MONEY_PATTERN = re.compile(r"^[+]?(\d+(?:\.\d+)?)([万亿]?)$")


@dataclass(frozen=True)
class ParsedPromotionResponse:
    ad_spend_cents: int
    business_date: date
    platform_store_id: str
    source_updated_at: datetime | None


@dataclass(frozen=True)
class ParsedDomMoney:
    displayed_cents: int
    minimum_cents: int
    maximum_cents: int
    precision: Literal["EXACT", "APPROXIMATE"]

    def matches(self, actual_cents: int) -> bool:
        return self.minimum_cents <= actual_cents <= self.maximum_cents


def decode_json_object(raw: bytes) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant {value}")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            parse_float=Decimal,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise CollectionRejected("PLATFORM_ERROR", "INVALID_JSON_RESPONSE") from exc
    if not isinstance(value, dict):
        raise CollectionRejected("PLATFORM_ERROR", "JSON_RESPONSE_NOT_OBJECT")
    return value


def read_object_path(value: dict[str, Any], path: str) -> Any:
    current: Any = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise CollectionRejected("PLATFORM_ERROR", f"MISSING_FIELD:{path}")
        current = current[part]
    return current


def _parse_cents(value: Any, unit: str) -> int:
    if isinstance(value, bool | float):
        raise CollectionRejected("UNIT_UNVERIFIED", "UNSAFE_MONEY_NUMBER_TYPE")
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise CollectionRejected("UNIT_UNVERIFIED", "INVALID_MONEY_VALUE") from exc
    if not amount.is_finite() or amount < 0:
        raise CollectionRejected("UNIT_UNVERIFIED", "INVALID_MONEY_VALUE")
    cents = amount if unit == "CNY_CENT" else amount * Decimal(100)
    if cents != cents.to_integral_value():
        raise CollectionRejected("UNIT_UNVERIFIED", "MONEY_NOT_INTEGER_CENTS")
    return int(cents)


def _strict_success(actual: Any, expected: bool | int | str) -> bool:
    return type(actual) is type(expected) and actual == expected


def _normalize_store_id(value: Any) -> str:
    if isinstance(value, bool | float) or not isinstance(value, int | str):
        raise CollectionRejected("IDENTITY_UNVERIFIED", "STORE_ID_MISSING_IN_RESPONSE")
    normalized = str(value).strip()
    if not normalized:
        raise CollectionRejected("IDENTITY_UNVERIFIED", "STORE_ID_MISSING_IN_RESPONSE")
    return normalized


def verify_store_identity(observed: Any, expected_store_id: str, expected_sha256: str = "") -> str:
    normalized = _normalize_store_id(observed)
    if expected_store_id:
        if normalized != expected_store_id:
            raise CollectionRejected("IDENTITY_MISMATCH", "STORE_ID_MISMATCH")
        return normalized
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    if not expected_sha256 or digest != expected_sha256:
        raise CollectionRejected("IDENTITY_MISMATCH", "STORE_ID_FINGERPRINT_MISMATCH")
    return f"sha256:{digest}"


def _parse_business_date(value: Any, date_format: str) -> date:
    if not isinstance(value, str):
        raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "BUSINESS_DATE_MISSING")
    try:
        if date_format == "ISO_DATETIME_SECONDS":
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").date()
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "INVALID_BUSINESS_DATE") from exc


def parse_promotion_response(
    raw: bytes,
    adapter: PromotionAdapterSettings,
    *,
    expected_store_id: str,
    expected_business_date: date,
    identity_raw: bytes | None = None,
    expected_store_id_sha256: str = "",
) -> ParsedPromotionResponse:
    if not adapter.verified:
        raise CollectionRejected("ADAPTER_UNVERIFIED", "PROMOTION_ADAPTER_NOT_VERIFIED")
    payload = decode_json_object(raw)
    success = read_object_path(payload, adapter.business_success_path)
    if not _strict_success(success, adapter.business_success_value):
        raise CollectionRejected("PLATFORM_ERROR", "BUSINESS_RESPONSE_NOT_SUCCESS")
    identity_payload = payload
    store_path = adapter.platform_store_id_path
    if adapter.identity_response_path:
        if identity_raw is None:
            raise CollectionRejected("IDENTITY_UNVERIFIED", "IDENTITY_RESPONSE_MISSING")
        identity_payload = decode_json_object(identity_raw)
        identity_success = read_object_path(
            identity_payload, adapter.identity_business_success_path
        )
        if not _strict_success(identity_success, adapter.identity_business_success_value):
            raise CollectionRejected("IDENTITY_UNVERIFIED", "IDENTITY_RESPONSE_NOT_SUCCESS")
        store_path = adapter.identity_platform_store_id_path
    observed_store = verify_store_identity(
        read_object_path(identity_payload, store_path),
        expected_store_id,
        expected_store_id_sha256,
    )
    metric_record = payload
    date_path = adapter.business_date_path
    if adapter.metric_list_path:
        items = read_object_path(payload, adapter.metric_list_path)
        if not isinstance(items, list):
            raise CollectionRejected("PLATFORM_ERROR", "METRIC_LIST_NOT_ARRAY")
        matches: list[tuple[dict[str, Any], date]] = []
        for item in items:
            if not isinstance(item, dict):
                raise CollectionRejected("PLATFORM_ERROR", "METRIC_LIST_ITEM_NOT_OBJECT")
            item_date = _parse_business_date(
                read_object_path(item, adapter.metric_item_business_date_path),
                adapter.metric_item_date_format,
            )
            if item_date == expected_business_date:
                matches.append((item, item_date))
        if len(matches) != 1:
            raise CollectionRejected(
                "TIME_SCOPE_UNVERIFIED",
                "TODAY_METRIC_RECORD_MISSING" if not matches else "TODAY_METRIC_RECORD_NOT_UNIQUE",
            )
        metric_record, business_date = matches[0]
        date_path = adapter.metric_item_business_date_path
    else:
        business_date = _parse_business_date(
            read_object_path(payload, date_path), adapter.metric_item_date_format
        )
    if business_date != expected_business_date:
        raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "BUSINESS_DATE_MISMATCH")
    if adapter.ad_spend_unit_path:
        observed_unit = read_object_path(metric_record, adapter.ad_spend_unit_path)
        if (
            not isinstance(observed_unit, str)
            or observed_unit != adapter.ad_spend_expected_unit_value
        ):
            raise CollectionRejected("UNIT_UNVERIFIED", "MONEY_UNIT_MISMATCH")
    cents = _parse_cents(
        read_object_path(metric_record, adapter.ad_spend_path), adapter.ad_spend_unit
    )
    source_updated_at: datetime | None = None
    if adapter.source_updated_at_path:
        raw_updated = read_object_path(metric_record, adapter.source_updated_at_path)
        if raw_updated is not None:
            if not isinstance(raw_updated, str):
                raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "INVALID_SOURCE_UPDATED_AT")
            try:
                source_updated_at = datetime.fromisoformat(raw_updated.replace("Z", "+00:00"))
            except ValueError as exc:
                raise CollectionRejected(
                    "TIME_SCOPE_UNVERIFIED", "INVALID_SOURCE_UPDATED_AT"
                ) from exc
            if source_updated_at.tzinfo is None or source_updated_at.utcoffset() is None:
                raise CollectionRejected(
                    "TIME_SCOPE_UNVERIFIED", "SOURCE_UPDATED_AT_REQUIRES_TIMEZONE"
                )
            if (
                source_updated_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
                != expected_business_date
            ):
                raise CollectionRejected(
                    "TIME_SCOPE_UNVERIFIED", "SOURCE_UPDATED_AT_OUTSIDE_BUSINESS_DATE"
                )
    return ParsedPromotionResponse(
        ad_spend_cents=cents,
        business_date=business_date,
        platform_store_id=observed_store,
        source_updated_at=source_updated_at,
    )


def parse_dom_money(text: str | None, unit: str) -> ParsedDomMoney:
    if text is None:
        raise CollectionRejected("DATA_MISMATCH", "DOM_VALUE_MISSING")
    normalized = text.strip().replace(",", "").replace(" ", "").replace("¥", "").replace("元", "")
    if normalized in MISSING_DOM_VALUES:
        raise CollectionRejected("DATA_MISMATCH", "DOM_VALUE_MISSING")
    match = MONEY_PATTERN.fullmatch(normalized)
    if match is None:
        raise CollectionRejected("UNIT_UNVERIFIED", "UNRECOGNIZED_DOM_MONEY")
    number_text, suffix = match.groups()
    if unit == "CNY_CENT" and suffix:
        raise CollectionRejected("UNIT_UNVERIFIED", "CENT_DISPLAY_CANNOT_USE_SUFFIX")
    try:
        amount = Decimal(number_text)
    except InvalidOperation as exc:
        raise CollectionRejected("UNIT_UNVERIFIED", "INVALID_DOM_MONEY") from exc
    multiplier = Decimal(1 if unit == "CNY_CENT" else 100)
    if suffix == "万":
        multiplier *= Decimal(10_000)
    elif suffix == "亿":
        multiplier *= Decimal(100_000_000)
    cents = amount * multiplier
    if cents != cents.to_integral_value():
        raise CollectionRejected("UNIT_UNVERIFIED", "DOM_MONEY_NOT_INTEGER_CENTS")
    displayed = int(cents)
    if not suffix:
        return ParsedDomMoney(displayed, displayed, displayed, "EXACT")
    decimals = len(number_text.partition(".")[2])
    quantum = (Decimal(10) ** -decimals) * multiplier
    half_quantum = quantum / Decimal(2)
    minimum = int((cents - half_quantum).to_integral_value(rounding="ROUND_CEILING"))
    maximum = int((cents + half_quantum).to_integral_value(rounding="ROUND_FLOOR"))
    return ParsedDomMoney(displayed, max(0, minimum), maximum, "APPROXIMATE")
