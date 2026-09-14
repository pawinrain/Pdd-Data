from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pdd_data_mcp.browser.parsing import (
    decode_json_object,
    read_object_path,
    verify_store_identity,
)
from pdd_data_mcp.config import (
    CoreAdapterBaseSettings,
    InventoryAdapterSettings,
    ProductCatalogAdapterSettings,
    StoreMetricSettings,
    StoreOverviewAdapterSettings,
)
from pdd_data_mcp.contracts.models import InventoryRecord, MetricValue, ProductCatalogRecord
from pdd_data_mcp.errors import CollectionRejected


@dataclass(frozen=True)
class ParsedStoreOverview:
    metrics: dict[str, MetricValue | None]
    business_date: date


@dataclass(frozen=True)
class ParsedProductPage:
    records: list[ProductCatalogRecord]
    total_observed: int


@dataclass(frozen=True)
class ParsedInventoryPage:
    records: list[InventoryRecord]
    total_observed: int


def _strict_success(actual: Any, expected: bool | int | str) -> bool:
    return type(actual) is type(expected) and actual == expected


def _parse_non_negative_int(value: Any, code: str) -> int:
    if isinstance(value, bool | float):
        raise CollectionRejected("DATA_MISMATCH", code)
    if isinstance(value, Decimal):
        if value != value.to_integral_value():
            raise CollectionRejected("DATA_MISMATCH", code)
        parsed = int(value)
    elif isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
    else:
        raise CollectionRejected("DATA_MISMATCH", code)
    if parsed < 0:
        raise CollectionRejected("DATA_MISMATCH", code)
    return parsed


def _parse_decimal(value: Any, code: str) -> Decimal:
    if isinstance(value, bool | float) or value is None:
        raise CollectionRejected("UNIT_UNVERIFIED", code)
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise CollectionRejected("UNIT_UNVERIFIED", code) from exc
    if not parsed.is_finite() or parsed < 0:
        raise CollectionRejected("UNIT_UNVERIFIED", code)
    return parsed


def _canonical_decimal(value: Decimal) -> str:
    normalized = format(value.normalize(), "f")
    return "0" if normalized in {"-0", ""} else normalized


def parse_metric_value(value: Any, mapping: StoreMetricSettings) -> int | str:
    amount = _parse_decimal(value, "INVALID_STORE_METRIC_VALUE")
    if mapping.source_unit in {"CNY", "CNY_CENT"}:
        cents = amount * (100 if mapping.source_unit == "CNY" else 1)
        if cents != cents.to_integral_value():
            raise CollectionRejected("UNIT_UNVERIFIED", "MONEY_NOT_INTEGER_CENTS")
        return int(cents)
    if mapping.source_unit == "COUNT":
        if amount != amount.to_integral_value():
            raise CollectionRejected("UNIT_UNVERIFIED", "COUNT_NOT_INTEGER")
        return int(amount)
    if mapping.source_unit == "PERCENT":
        amount /= Decimal(100)
    return _canonical_decimal(amount)


def _parse_business_date(value: Any, date_format: str) -> date:
    if not isinstance(value, str):
        raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "BUSINESS_DATE_MISSING")
    try:
        if date_format == "ISO_DATETIME_SECONDS":
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").date()
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "INVALID_BUSINESS_DATE") from exc


def _validate_business_success(payload: dict[str, Any], adapter: CoreAdapterBaseSettings) -> None:
    actual = read_object_path(payload, adapter.business_success_path)
    if not _strict_success(actual, adapter.business_success_value):
        raise CollectionRejected("PLATFORM_ERROR", "BUSINESS_RESPONSE_NOT_SUCCESS")


def parse_response_identity(
    raw: bytes,
    adapter: CoreAdapterBaseSettings,
    *,
    expected_store_id: str,
    expected_store_id_sha256: str,
) -> str:
    payload = decode_json_object(raw)
    success_path = (
        adapter.identity_business_success_path
        if adapter.identity_source == "IDENTITY_RESPONSE"
        else adapter.business_success_path
    )
    success_value = (
        adapter.identity_business_success_value
        if adapter.identity_source == "IDENTITY_RESPONSE"
        else adapter.business_success_value
    )
    if not _strict_success(read_object_path(payload, success_path), success_value):
        raise CollectionRejected("IDENTITY_UNVERIFIED", "IDENTITY_RESPONSE_NOT_SUCCESS")
    store_path = (
        adapter.identity_platform_store_id_path
        if adapter.identity_source == "IDENTITY_RESPONSE"
        else adapter.platform_store_id_path
    )
    return verify_store_identity(
        read_object_path(payload, store_path), expected_store_id, expected_store_id_sha256
    )


def parse_store_overview_response(
    raw: bytes,
    adapter: StoreOverviewAdapterSettings,
    *,
    expected_business_date: date,
    observed_at: datetime,
) -> ParsedStoreOverview:
    if not adapter.verified:
        raise CollectionRejected("ADAPTER_UNVERIFIED", "STORE_OVERVIEW_ADAPTER_NOT_VERIFIED")
    payload = decode_json_object(raw)
    _validate_business_success(payload, adapter)
    business_date = _parse_business_date(
        read_object_path(payload, adapter.business_date_path), adapter.business_date_format
    )
    if business_date != expected_business_date:
        raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "BUSINESS_DATE_MISMATCH")
    metrics: dict[str, MetricValue | None] = {}
    for name, mapping in adapter.metrics.items():
        try:
            raw_value = read_object_path(payload, mapping.response_path)
        except CollectionRejected as exc:
            if exc.error_code.startswith("MISSING_FIELD:"):
                metrics[name] = None
                continue
            raise
        if raw_value is None:
            metrics[name] = None
            continue
        if mapping.response_unit_path:
            actual_unit = read_object_path(payload, mapping.response_unit_path)
            if actual_unit != mapping.expected_response_unit:
                raise CollectionRejected("UNIT_UNVERIFIED", "STORE_METRIC_UNIT_MISMATCH")
        metrics[name] = MetricValue(
            value=parse_metric_value(raw_value, mapping),
            unit=mapping.output_unit,
            observed_at=observed_at,
            capture_method="NETWORK_RESPONSE",
            precision=mapping.precision,
        )
    return ParsedStoreOverview(metrics=metrics, business_date=business_date)


def _normalize_id(value: Any, code: str) -> str:
    if isinstance(value, bool | float) or not isinstance(value, int | str):
        raise CollectionRejected("DATA_MISMATCH", code)
    normalized = str(value).strip()
    if not normalized:
        raise CollectionRejected("DATA_MISMATCH", code)
    return normalized


def _optional_path(item: dict[str, Any], path: str) -> Any | None:
    if not path:
        return None
    try:
        return read_object_path(item, path)
    except CollectionRejected as exc:
        if exc.error_code.startswith("MISSING_FIELD:"):
            return None
        raise


def _parse_optional_time(value: Any, code: str) -> datetime | None:
    from datetime import timezone
    if value is None:
        return None
    if isinstance(value, int | float):
        # Unix timestamp
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (ValueError, OSError) as exc:
            raise CollectionRejected("TIME_SCOPE_UNVERIFIED", code) from exc
    if not isinstance(value, str):
        raise CollectionRejected("TIME_SCOPE_UNVERIFIED", code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CollectionRejected("TIME_SCOPE_UNVERIFIED", code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CollectionRejected("TIME_SCOPE_UNVERIFIED", code)
    return parsed


def parse_product_page_response(
    raw: bytes,
    adapter: ProductCatalogAdapterSettings,
    *,
    observed_at: datetime,
    expected_store_id: str = "",
    expected_store_id_sha256: str = "",
) -> ParsedProductPage:
    if not adapter.verified:
        raise CollectionRejected("ADAPTER_UNVERIFIED", "PRODUCT_ADAPTER_NOT_VERIFIED")
    payload = decode_json_object(raw)
    _validate_business_success(payload, adapter)
    items = read_object_path(payload, adapter.list_path)
    if not isinstance(items, list):
        raise CollectionRejected("PLATFORM_ERROR", "PRODUCT_LIST_NOT_ARRAY")
    total = _parse_non_negative_int(
        read_object_path(payload, adapter.total_path), "INVALID_PRODUCT_TOTAL"
    )
    records: list[ProductCatalogRecord] = []
    for item in items:
        if not isinstance(item, dict):
            raise CollectionRejected("PLATFORM_ERROR", "PRODUCT_LIST_ITEM_NOT_OBJECT")
        if adapter.item_platform_store_id_path:
            verify_store_identity(
                read_object_path(item, adapter.item_platform_store_id_path),
                expected_store_id,
                expected_store_id_sha256,
            )
        product_id = _normalize_id(
            read_object_path(item, adapter.product_id_path), "PRODUCT_ID_MISSING"
        )
        raw_name = _optional_path(item, adapter.product_name_path)
        name = None if raw_name is None else str(raw_name)
        raw_status = _optional_path(item, adapter.status_path)
        status: Literal["ON_SALE", "OFF_SALE", "UNKNOWN"] | None = None
        if adapter.status_path:
            status = adapter.status_map.get(str(raw_status), "UNKNOWN")
        def _extract_price(raw: Any) -> Any:
            if isinstance(raw, list) and len(raw) > 0:
                return raw[0]
            return raw

        raw_price = _optional_path(item, adapter.price_path)
        price_cents: int | None = None
        if raw_price is not None:
            price_cents = int(
                parse_metric_value(
                    _extract_price(raw_price),
                    StoreMetricSettings(
                        response_path="value",
                        source_unit=adapter.price_unit,
                        output_unit="CNY_CENT",
                    ),
                )
            )
        raw_market_price = _optional_path(item, adapter.market_price_path)
        market_price_cents: int | None = None
        if raw_market_price is not None:
            market_price_cents = int(
                parse_metric_value(
                    _extract_price(raw_market_price),
                    StoreMetricSettings(
                        response_path="value",
                        source_unit=adapter.price_unit,
                        output_unit="CNY_CENT",
                    ),
                )
            )
        image_url = None
        if adapter.image_url_path:
            raw_image = _optional_path(item, adapter.image_url_path)
            image_url = None if raw_image is None else str(raw_image)
        thumb_url = None
        if adapter.thumb_url_path:
            raw_thumb = _optional_path(item, adapter.thumb_url_path)
            thumb_url = None if raw_thumb is None else str(raw_thumb)
        sold_quantity = None
        if adapter.sold_quantity_path:
            raw_sold = _optional_path(item, adapter.sold_quantity_path)
            sold_quantity = None if raw_sold is None else _parse_non_negative_int(raw_sold, "INVALID_SOLD_QUANTITY")
        sold_quantity_30d = None
        if adapter.sold_quantity_30d_path:
            raw_sold_30d = _optional_path(item, adapter.sold_quantity_30d_path)
            sold_quantity_30d = None if raw_sold_30d is None else _parse_non_negative_int(raw_sold_30d, "INVALID_SOLD_QUANTITY_30D")
        fav_cnt = None
        if adapter.fav_cnt_path:
            raw_fav = _optional_path(item, adapter.fav_cnt_path)
            fav_cnt = None if raw_fav is None else _parse_non_negative_int(raw_fav, "INVALID_FAV_CNT")
        cat_name = None
        if adapter.cat_name_path:
            raw_cat = _optional_path(item, adapter.cat_name_path)
            cat_name = None if raw_cat is None else str(raw_cat)
        brand_name = None
        if adapter.brand_name_path:
            raw_brand = _optional_path(item, adapter.brand_name_path)
            brand_name = None if raw_brand is None else str(raw_brand)
        raw_sku_count = _optional_path(item, adapter.sku_count_path)
        sku_count = (
            None
            if raw_sku_count is None
            else _parse_non_negative_int(raw_sku_count, "INVALID_SKU_COUNT")
        )
        records.append(
            ProductCatalogRecord(
                product_id=product_id,
                platform_product_id=product_id,
                name=name,
                price_cents=price_cents,
                market_price_cents=market_price_cents,
                image_url=image_url,
                thumb_url=thumb_url,
                sold_quantity=sold_quantity,
                sold_quantity_30d=sold_quantity_30d,
                fav_cnt=fav_cnt,
                cat_name=cat_name,
                brand_name=brand_name,
                status=status,
                sku_count=sku_count,
                created_at=_parse_optional_time(
                    _optional_path(item, adapter.created_at_path), "INVALID_PRODUCT_CREATED_AT"
                ),
                published_at=_parse_optional_time(
                    _optional_path(item, adapter.published_at_path),
                    "INVALID_PRODUCT_PUBLISHED_AT",
                ),
                observed_at=observed_at,
            )
        )
    ids = [record.product_id for record in records]
    if len(ids) != len(set(ids)):
        raise CollectionRejected("DATA_MISMATCH", "DUPLICATE_PRODUCT_ID")
    if len(records) > total:
        raise CollectionRejected("DATA_MISMATCH", "PRODUCT_COUNT_EXCEEDS_TOTAL")
    return ParsedProductPage(records=records, total_observed=total)


def parse_inventory_page_response(
    raw: bytes,
    adapter: InventoryAdapterSettings,
    *,
    observed_at: datetime,
    expected_store_id: str = "",
    expected_store_id_sha256: str = "",
) -> ParsedInventoryPage:
    if not adapter.verified:
        raise CollectionRejected("ADAPTER_UNVERIFIED", "INVENTORY_ADAPTER_NOT_VERIFIED")
    payload = decode_json_object(raw)
    _validate_business_success(payload, adapter)
    items = read_object_path(payload, adapter.list_path)
    if not isinstance(items, list):
        raise CollectionRejected("PLATFORM_ERROR", "INVENTORY_LIST_NOT_ARRAY")
    total = _parse_non_negative_int(
        read_object_path(payload, adapter.total_path), "INVALID_INVENTORY_TOTAL"
    )
    records: list[InventoryRecord] = []
    for item in items:
        if not isinstance(item, dict):
            raise CollectionRejected("PLATFORM_ERROR", "INVENTORY_LIST_ITEM_NOT_OBJECT")
        if adapter.item_platform_store_id_path:
            verify_store_identity(
                read_object_path(item, adapter.item_platform_store_id_path),
                expected_store_id,
                expected_store_id_sha256,
            )
        product_id = _normalize_id(
            read_object_path(item, adapter.product_id_path), "INVENTORY_PRODUCT_ID_MISSING"
        )
        rows = [item]
        if adapter.granularity == "SKU":
            nested = read_object_path(item, adapter.sku_list_path)
            if not isinstance(nested, list):
                raise CollectionRejected("PLATFORM_ERROR", "SKU_LIST_NOT_ARRAY")
            rows = nested
        for row in rows:
            if not isinstance(row, dict):
                raise CollectionRejected("PLATFORM_ERROR", "INVENTORY_ROW_NOT_OBJECT")
            raw_inventory = _optional_path(row, adapter.inventory_path)
            inventory = (
                None
                if raw_inventory is None
                else _parse_non_negative_int(raw_inventory, "INVALID_INVENTORY_VALUE")
            )
            sku_id = None
            if adapter.granularity == "SKU":
                sku_id = _normalize_id(read_object_path(row, adapter.sku_id_path), "SKU_ID_MISSING")
            records.append(
                InventoryRecord(
                    product_id=product_id,
                    platform_product_id=product_id,
                    sku_id=sku_id,
                    inventory=inventory,
                    granularity=adapter.granularity,
                    observed_at=observed_at,
                )
            )
    keys = [(record.product_id, record.sku_id, record.granularity) for record in records]
    if len(keys) != len(set(keys)):
        raise CollectionRejected("DATA_MISMATCH", "DUPLICATE_INVENTORY_RECORD")
    if adapter.granularity == "PRODUCT" and len(records) > total:
        raise CollectionRejected("DATA_MISMATCH", "INVENTORY_COUNT_EXCEEDS_TOTAL")
    return ParsedInventoryPage(records=records, total_observed=total)
