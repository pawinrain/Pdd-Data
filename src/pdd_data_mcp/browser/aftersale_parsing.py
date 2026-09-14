"""Parse MMS aftersale queryList responses into AftersaleOrderRecord rows."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pdd_data_mcp.contracts.models import AftersaleOrderField, AftersaleOrderRecord, MissingReason
from pdd_data_mcp.errors import CollectionRejected


def _read_path(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise CollectionRejected("DATA_MISMATCH", "AFTERSALE_PATH_MISSING")
        cur = cur[part]
    return cur


def _as_nonneg_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if value != value or value < 0:
            return None
        return int(round(value))
    try:
        num = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if num != num or num < 0:
        return None
    return int(round(num))


def _as_str(value: Any, *, max_len: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:max_len]


def _created_at(value: Any) -> datetime:
    ts = _as_nonneg_int(value)
    if ts is None:
        raise CollectionRejected("DATA_MISMATCH", "AFTERSALE_CREATED_AT_MISSING")
    if ts > 10_000_000_000:
        ts = ts // 1000
    return datetime.fromtimestamp(ts, tz=UTC)


def _take(
    row: dict[str, Any],
    path: str,
    *,
    kind: str,
    max_len: int = 256,
) -> tuple[Any, MissingReason | None]:
    if not path:
        return None, "SOURCE_FIELD_MISSING"
    try:
        value = _read_path(row, path)
    except CollectionRejected:
        return None, "SOURCE_FIELD_MISSING"
    if value is None:
        return None, "SOURCE_VALUE_NULL"
    if kind == "int":
        parsed = _as_nonneg_int(value)
        return (parsed, None) if parsed is not None else (None, "SOURCE_VALUE_NULL")
    parsed = _as_str(value, max_len=max_len)
    return (parsed, None) if parsed is not None else (None, "SOURCE_VALUE_NULL")


def parse_aftersale_list_response(
    raw: dict[str, Any] | bytes | str,
    *,
    adapter: Any,
    observed_at: datetime,
) -> tuple[list[AftersaleOrderRecord], int | None]:
    payload = json.loads(raw) if isinstance(raw, (bytes, str)) else raw
    if not isinstance(payload, dict):
        raise CollectionRejected("DATA_MISMATCH", "AFTERSALE_PAYLOAD_NOT_OBJECT")

    success_path = adapter.business_success_path or "success"
    success = _read_path(payload, success_path)
    if success is not True and success != adapter.business_success_value:
        raise CollectionRejected("DATA_MISMATCH", "AFTERSALE_BUSINESS_NOT_SUCCESS")

    list_path = adapter.list_path or "result.list"
    rows = _read_path(payload, list_path)
    if not isinstance(rows, list):
        raise CollectionRejected("DATA_MISMATCH", "AFTERSALE_LIST_NOT_ARRAY")

    total = None
    if adapter.total_path:
        try:
            total = _as_nonneg_int(_read_path(payload, adapter.total_path))
        except CollectionRejected:
            total = None

    records: list[AftersaleOrderRecord] = []
    for item in rows:
        if not isinstance(item, dict):
            raise CollectionRejected("DATA_MISMATCH", "AFTERSALE_ROW_NOT_OBJECT")
        aftersale_id = _as_str(_read_path(item, adapter.aftersale_id_path or "id"), max_len=128)
        order_sn = _as_str(_read_path(item, adapter.order_sn_path or "orderSn"), max_len=128)
        if not aftersale_id or not order_sn:
            raise CollectionRejected("DATA_MISMATCH", "AFTERSALE_ID_OR_ORDER_SN_EMPTY")
        created_at = _created_at(_read_path(item, adapter.created_at_path or "createdAt"))

        missing: dict[AftersaleOrderField, MissingReason] = {}
        field_specs: list[tuple[AftersaleOrderField, str, str, int]] = [
            ("refund_amount_cents", adapter.refund_amount_path or "refundAmount", "int", 0),
            ("order_amount_cents", adapter.order_amount_path or "orderAmount", "int", 0),
            ("goods_name", adapter.goods_name_path or "goodsName", "str", 512),
            ("goods_spec", adapter.goods_spec_path or "goodsSpec", "str", 256),
            ("goods_number", adapter.goods_number_path or "goodsNumber", "int", 0),
            ("reason_desc", adapter.reason_desc_path or "afterSalesReasonDesc", "str", 256),
            ("expire_remain_seconds", adapter.expire_remain_path or "expireRemainTime", "int", 0),
            ("aftersale_title", adapter.aftersale_title_path or "afterSalesTitle", "str", 256),
        ]
        values: dict[str, Any] = {}
        for field, path, kind, max_len in field_specs:
            value, reason = _take(item, path, kind=kind, max_len=max_len or 256)
            values[field] = value
            if reason is not None:
                missing[field] = reason

        type_val, _ = _take(item, adapter.aftersale_type_path or "afterSalesType", kind="int")
        type_name, _ = _take(
            item, adapter.aftersale_type_name_path or "afterSalesTypeName", kind="str", max_len=128
        )
        status_val, _ = _take(item, adapter.aftersale_status_path or "afterSalesStatus", kind="int")

        records.append(
            AftersaleOrderRecord(
                aftersale_id=aftersale_id,
                order_sn=order_sn,
                aftersale_type=type_val,
                aftersale_type_name=type_name,
                aftersale_status=status_val,
                aftersale_title=values["aftersale_title"],
                refund_amount_cents=values["refund_amount_cents"],
                order_amount_cents=values["order_amount_cents"],
                goods_name=values["goods_name"],
                goods_spec=values["goods_spec"],
                goods_number=values["goods_number"],
                reason_desc=values["reason_desc"],
                expire_remain_seconds=values["expire_remain_seconds"],
                created_at=created_at,
                observed_at=observed_at,
                missing_reasons=missing,
            )
        )
    return records, total
