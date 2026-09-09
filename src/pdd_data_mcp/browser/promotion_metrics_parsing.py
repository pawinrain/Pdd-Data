from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pdd_data_mcp.browser.parsing import decode_json_object, verify_store_identity
from pdd_data_mcp.contracts.models import (
    MissingReason,
    PromotedProductEffectMetrics,
    PromotedProductMetricRecord,
    PromotionConfigurationRecord,
)
from pdd_data_mcp.errors import CollectionRejected

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
_REPORT_FIELDS = {**_MONEY_FIELDS, **_RATIO_FIELDS, **_COUNT_FIELDS}
_MISSING = object()


@dataclass(frozen=True)
class ParsedPromotedProductResponse:
    """Strictly parsed promoted-product rows and their current configuration.

    The response has no verified total field, so ``total_observed`` deliberately
    remains ``None``. ``campaign_id`` on each row is only a foreign-key
    association and does not make the row campaign-granular.
    """

    metric_records: list[PromotedProductMetricRecord]
    configuration_records: list[PromotionConfigurationRecord]
    summary_metrics: PromotedProductEffectMetrics
    source_updated_at: datetime
    observed_platform_store_id: str | None
    total_observed: None = None


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool | float) or not isinstance(value, int | str | Decimal):
        raise CollectionRejected("UNIT_UNVERIFIED", f"INVALID_NUMBER_TYPE:{field}")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise CollectionRejected("UNIT_UNVERIFIED", f"INVALID_NUMBER:{field}") from exc
    if not parsed.is_finite() or parsed < 0:
        raise CollectionRejected("UNIT_UNVERIFIED", f"INVALID_NUMBER:{field}")
    return parsed


def _yuan_to_cents(value: Any, field: str) -> int:
    cents = _decimal(value, field) * Decimal(100)
    if cents != cents.to_integral_value():
        raise CollectionRejected("UNIT_UNVERIFIED", f"MONEY_NOT_INTEGER_CENTS:{field}")
    return int(cents)


def _per_one_to_string(value: Any, field: str) -> str:
    parsed = _decimal(value, field)
    result = format(parsed, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return result or "0"


def _missing_reason(value: object) -> MissingReason | None:
    if value is _MISSING:
        return "SOURCE_FIELD_MISSING"
    if value is None:
        return "SOURCE_VALUE_NULL"
    return None


def _wrapped_metric(
    parent: dict[str, Any],
    source_field: str,
    *,
    unit: Literal["YUAN", "PER_ONE"],
) -> tuple[int | str | None, MissingReason | None]:
    raw = parent.get(source_field, _MISSING)
    missing = _missing_reason(raw)
    if missing is not None:
        return None, missing
    if not isinstance(raw, dict):
        raise CollectionRejected("PLATFORM_ERROR", f"METRIC_NOT_OBJECT:{source_field}")
    value = raw.get("value", _MISSING)
    missing = _missing_reason(value)
    if missing is not None:
        return None, missing
    if raw.get("unit", _MISSING) != unit or type(raw.get("unitCode", _MISSING)) is not int:
        raise CollectionRejected("UNIT_UNVERIFIED", f"METRIC_UNIT_MISMATCH:{source_field}")
    if raw["unitCode"] != 1:
        raise CollectionRejected("UNIT_UNVERIFIED", f"METRIC_UNIT_CODE_MISMATCH:{source_field}")
    if unit == "YUAN":
        return _yuan_to_cents(value, source_field), None
    return _per_one_to_string(value, source_field), None


def _count_metric(
    parent: dict[str, Any], source_field: str
) -> tuple[int | None, MissingReason | None]:
    raw = parent.get(source_field, _MISSING)
    missing = _missing_reason(raw)
    if missing is not None:
        return None, missing
    if type(raw) is not int or raw < 0:
        raise CollectionRejected("UNIT_UNVERIFIED", f"INVALID_COUNT:{source_field}")
    return raw, None


def _parse_effect_metrics(value: object, *, source_name: str) -> PromotedProductEffectMetrics:
    if value is _MISSING:
        parent: dict[str, Any] = {}
        container_reason: MissingReason | None = "SOURCE_FIELD_MISSING"
    elif value is None:
        parent = {}
        container_reason = "SOURCE_VALUE_NULL"
    elif isinstance(value, dict):
        parent = value
        container_reason = None
    else:
        raise CollectionRejected("PLATFORM_ERROR", f"{source_name}_NOT_OBJECT")

    parsed: dict[str, int | str | None] = {}
    reasons: dict[str, MissingReason] = {}
    for source_field, output_field in _MONEY_FIELDS.items():
        if container_reason is None:
            metric, reason = _wrapped_metric(parent, source_field, unit="YUAN")
        else:
            metric, reason = None, container_reason
        parsed[output_field] = metric
        if reason is not None:
            reasons[output_field] = reason
    for source_field, output_field in _RATIO_FIELDS.items():
        if container_reason is None:
            metric, reason = _wrapped_metric(parent, source_field, unit="PER_ONE")
        else:
            metric, reason = None, container_reason
        parsed[output_field] = metric
        if reason is not None:
            reasons[output_field] = reason
    for source_field, output_field in _COUNT_FIELDS.items():
        if container_reason is None:
            metric, reason = _count_metric(parent, source_field)
        else:
            metric, reason = None, container_reason
        parsed[output_field] = metric
        if reason is not None:
            reasons[output_field] = reason
    return PromotedProductEffectMetrics.model_validate(
        {**parsed, "missing_reasons": reasons}, strict=True
    )


def _positive_platform_id(value: object, field: str) -> str:
    if type(value) is not int or value <= 0:
        raise CollectionRejected("PLATFORM_ERROR", f"INVALID_ID:{field}")
    return str(value)


def _report_timestamp(value: object) -> datetime:
    if type(value) is not int or len(str(value)) != 13:
        raise CollectionRejected(
            "TIME_SCOPE_UNVERIFIED", "REPORT_LAST_UPDATE_TIME_NOT_UNIX_MILLISECONDS"
        )
    seconds, milliseconds = divmod(value, 1000)
    try:
        parsed = datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=milliseconds * 1000)
    except (OverflowError, OSError, ValueError) as exc:
        raise CollectionRejected(
            "TIME_SCOPE_UNVERIFIED", "REPORT_LAST_UPDATE_TIME_OUT_OF_RANGE"
        ) from exc
    if not 2020 <= parsed.year <= 2100:
        raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "REPORT_LAST_UPDATE_TIME_OUT_OF_RANGE")
    return parsed


def _configuration_value(
    row: dict[str, Any], source_field: str, *, unit: Literal["YUAN", "PER_ONE"]
) -> tuple[int | str | None, MissingReason | None]:
    return _wrapped_metric(row, source_field, unit=unit)


def _status_value(row: dict[str, Any]) -> tuple[int | None, MissingReason | None]:
    raw = row.get("adStatus", _MISSING)
    missing = _missing_reason(raw)
    if missing is not None:
        return None, missing
    if type(raw) is not int or raw < 0:
        raise CollectionRejected("PLATFORM_ERROR", "INVALID_AD_STATUS")
    return raw, None


def _parse_configuration(
    row: dict[str, Any],
    *,
    ad_id: str,
    campaign_id: str,
    platform_product_id: str,
    observed_at: datetime,
) -> PromotionConfigurationRecord:
    values: dict[str, int | str | None] = {}
    reasons: dict[str, MissingReason] = {}
    mappings = (
        ("max_cost_cents", "maxCost", "YUAN"),
        ("target_roi", "targetRoi", "PER_ONE"),
        ("agent_bid", "agentBid", "PER_ONE"),
    )
    for output_field, source_field, unit in mappings:
        parsed, reason = _configuration_value(
            row,
            source_field,
            unit=unit,  # type: ignore[arg-type]
        )
        values[output_field] = parsed
        if reason is not None:
            reasons[output_field] = reason
    status, reason = _status_value(row)
    values["ad_status"] = status
    if reason is not None:
        reasons["ad_status"] = reason
    return PromotionConfigurationRecord.model_validate(
        {
            "ad_id": ad_id,
            "campaign_id": campaign_id,
            "platform_product_id": platform_product_id,
            **values,
            "configuration_observed_at": observed_at,
            "missing_reasons": reasons,
        },
        strict=True,
    )


def parse_promoted_product_response(
    raw: bytes,
    *,
    observed_at: datetime,
    expected_store_id: str,
    expected_store_id_sha256: str = "",
) -> ParsedPromotedProductResponse:
    """Parse the verified goods-promotion v3 list response without inferring totals.

    This parser emits promoted-product records only. Although every row has a
    ``planId``, it never aggregates or relabels an ``adInfo`` row as a campaign.
    """

    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "OBSERVED_AT_REQUIRES_TIMEZONE")
    payload = decode_json_object(raw)
    if payload.get("success", _MISSING) is not True:
        raise CollectionRejected("PLATFORM_ERROR", "BUSINESS_RESPONSE_NOT_SUCCESS")
    result = payload.get("result", _MISSING)
    if not isinstance(result, dict):
        raise CollectionRejected("PLATFORM_ERROR", "PROMOTION_RESULT_NOT_OBJECT")
    rows = result.get("adInfos", _MISSING)
    if not isinstance(rows, list):
        raise CollectionRejected("PLATFORM_ERROR", "AD_INFOS_NOT_ARRAY")
    source_updated_at = _report_timestamp(result.get("reportLastUpdateTime", _MISSING))
    summary = _parse_effect_metrics(
        result.get("sumReportInfo", _MISSING), source_name="SUM_REPORT_INFO"
    )

    metric_records: list[PromotedProductMetricRecord] = []
    configuration_records: list[PromotionConfigurationRecord] = []
    seen_links: set[tuple[str, str, str]] = set()
    seen_ad_ids: set[str] = set()
    observed_platform_store_id: str | None = None
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise CollectionRejected("PLATFORM_ERROR", f"AD_INFO_NOT_OBJECT:{index}")
        ad_id = _positive_platform_id(row.get("adId", _MISSING), "adId")
        campaign_id = _positive_platform_id(row.get("planId", _MISSING), "planId")
        platform_product_id = _positive_platform_id(row.get("goodsId", _MISSING), "goodsId")
        observed_store = verify_store_identity(
            row.get("mallId", _MISSING), expected_store_id, expected_store_id_sha256
        )
        if observed_platform_store_id is None:
            observed_platform_store_id = observed_store
        elif observed_store != observed_platform_store_id:
            raise CollectionRejected("IDENTITY_MISMATCH", "ROW_STORE_IDENTITY_MISMATCH")
        association = (ad_id, campaign_id, platform_product_id)
        if ad_id in seen_ad_ids or association in seen_links:
            raise CollectionRejected("DATA_MISMATCH", "DUPLICATE_PROMOTED_PRODUCT_ROW")
        seen_ad_ids.add(ad_id)
        seen_links.add(association)

        metrics = _parse_effect_metrics(row.get("reportInfo", _MISSING), source_name="REPORT_INFO")
        metric_records.append(
            PromotedProductMetricRecord(
                ad_id=ad_id,
                campaign_id=campaign_id,
                platform_product_id=platform_product_id,
                metrics=metrics,
                observed_at=observed_at,
                source_updated_at=source_updated_at,
            )
        )
        configuration_records.append(
            _parse_configuration(
                row,
                ad_id=ad_id,
                campaign_id=campaign_id,
                platform_product_id=platform_product_id,
                observed_at=observed_at,
            )
        )

    return ParsedPromotedProductResponse(
        metric_records=metric_records,
        configuration_records=configuration_records,
        summary_metrics=summary,
        source_updated_at=source_updated_at,
        observed_platform_store_id=observed_platform_store_id,
    )


__all__ = ["ParsedPromotedProductResponse", "parse_promoted_product_response"]
