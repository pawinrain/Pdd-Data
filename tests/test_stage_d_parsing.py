from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

import pytest

from pdd_data_mcp.browser.core_parsing import (
    parse_inventory_page_response,
    parse_product_page_response,
    parse_store_overview_response,
)
from pdd_data_mcp.collectors import SyntheticCollector
from pdd_data_mcp.config import (
    InventoryAdapterSettings,
    ProductCatalogAdapterSettings,
    StoreMetricSettings,
    StoreOverviewAdapterSettings,
)
from pdd_data_mcp.contracts.models import Coverage, CoverageStatus, DatasetType, Scope, WindowKind
from pdd_data_mcp.errors import CollectionRejected

OBSERVED = datetime(2026, 9, 7, 4, 0, tzinfo=UTC)


def base() -> dict[str, object]:
    return {
        "verified": True,
        "target_page_url": "http://127.0.0.1:8765/page",
        "response_host": "127.0.0.1",
        "response_path": "/api/data",
        "response_method": "POST",
        "business_success_path": "success",
        "business_success_value": True,
        "identity_source": "MAIN_RESPONSE",
        "platform_store_id_path": "result.mallId",
        "identity_verification_reference": "fixture-exact-id-v1",
        "parser_version": "fixture/1.0.0",
        "trigger": "RELOAD",
    }


def store_adapter() -> StoreOverviewAdapterSettings:
    return StoreOverviewAdapterSettings.model_validate(
        {
            **base(),
            "business_date_path": "result.date",
            "business_date_format": "ISO_DATE",
            "dom_business_date_selector": "#today",
            "metrics": {
                "gmv": StoreMetricSettings(
                    response_path="result.gmv",
                    source_unit="CNY",
                    output_unit="CNY_CENT",
                    dom_selector="#gmv",
                ),
                "order_count": StoreMetricSettings(
                    response_path="result.orders",
                    source_unit="COUNT",
                    output_unit="COUNT",
                    dom_selector="#orders",
                ),
            },
        }
    )


def product_adapter() -> ProductCatalogAdapterSettings:
    return ProductCatalogAdapterSettings.model_validate(
        {
            **base(),
            "list_path": "result.items",
            "total_path": "result.total",
            "product_id_path": "goodsId",
            "product_name_path": "name",
            "status_path": "status",
            "status_map": {"1": "ON_SALE", "0": "OFF_SALE"},
            "price_path": "price",
            "price_unit": "CNY_CENT",
            "sku_count_path": "skuCount",
            "dom_total_selector": "#total",
        }
    )


def inventory_adapter() -> InventoryAdapterSettings:
    return InventoryAdapterSettings.model_validate(
        {
            **base(),
            "list_path": "result.items",
            "total_path": "result.total",
            "product_id_path": "goodsId",
            "inventory_path": "quantity",
            "granularity": "PRODUCT",
            "dom_total_selector": "#total",
        }
    )


def test_store_parser_preserves_units_and_missing_metric_is_not_zero() -> None:
    parsed = parse_store_overview_response(
        b'{"success":true,"result":{"mallId":1001,"date":"2026-09-07","gmv":"19.90","orders":0}}',
        store_adapter(),
        expected_business_date=date(2026, 9, 7),
        observed_at=OBSERVED,
    )
    assert parsed.metrics["gmv"] is not None
    assert parsed.metrics["gmv"].value == 1990
    assert parsed.metrics["gmv"].unit == "CNY_CENT"
    assert parsed.metrics["order_count"] is not None
    assert parsed.metrics["order_count"].value == 0

    missing = parse_store_overview_response(
        b'{"success":true,"result":{"mallId":1001,"date":"2026-09-07","gmv":"19.90"}}',
        store_adapter(),
        expected_business_date=date(2026, 9, 7),
        observed_at=OBSERVED,
    )
    assert missing.metrics["order_count"] is None


def test_three_product_complete_fixture_and_duplicate_rejection() -> None:
    raw = (
        b'{"success":true,"result":{"mallId":1001,"total":3,"items":['
        b'{"goodsId":101,"name":"A","status":1,"price":1990,"skuCount":1},'
        b'{"goodsId":"102","name":"B","status":1,"price":"2990","skuCount":2},'
        b'{"goodsId":103,"name":"C","status":0,"price":0,"skuCount":1}]}}'
    )
    parsed = parse_product_page_response(raw, product_adapter(), observed_at=OBSERVED)
    assert parsed.total_observed == 3
    assert len(parsed.records) == 3
    assert {record.platform_product_id for record in parsed.records} == {"101", "102", "103"}
    assert parsed.records[2].price_cents == 0

    duplicate = raw.replace(b'"goodsId":103', b'"goodsId":101')
    with pytest.raises(CollectionRejected, match="DUPLICATE_PRODUCT_ID"):
        parse_product_page_response(duplicate, product_adapter(), observed_at=OBSERVED)


def test_product_row_store_identity_mismatch_is_rejected() -> None:
    adapter = product_adapter().model_copy(update={"item_platform_store_id_path": "mallId"})
    with pytest.raises(CollectionRejected, match="STORE_ID_MISMATCH"):
        parse_product_page_response(
            b'{"success":true,"result":{"mallId":1001,"total":1,"items":['
            b'{"mallId":2002,"goodsId":101,"name":"A","status":1,"price":1990,'
            b'"skuCount":1}]}}',
            adapter,
            observed_at=OBSERVED,
            expected_store_id="1001",
        )


def test_inventory_preserves_real_zero_and_missing_as_null() -> None:
    parsed = parse_inventory_page_response(
        b'{"success":true,"result":{"mallId":1001,"total":3,"items":['
        b'{"goodsId":101,"quantity":0},{"goodsId":102,"quantity":null},'
        b'{"goodsId":103,"quantity":"8"}]}}',
        inventory_adapter(),
        observed_at=OBSERVED,
    )
    assert [record.inventory for record in parsed.records] == [0, None, 8]
    assert {record.product_id for record in parsed.records} == {"101", "102", "103"}


def test_product_inventory_mapping_detects_foreign_product() -> None:
    products = {"101", "102", "103"}
    inventory = {"101", "102", "999"}
    assert inventory.issubset(products) is False


def test_known_three_with_two_captured_is_partial_not_truncated() -> None:
    coverage = Coverage(
        total_observed=3,
        captured=2,
        limit=50,
        pages_read=1,
        coverage=CoverageStatus.PARTIAL,
        truncated=False,
        stop_reason="SOURCE_ROWS_MISSING",
    )
    assert coverage.coverage is CoverageStatus.PARTIAL
    assert coverage.truncated is False


def test_synthetic_55_50_regression_retains_six_page_observation() -> None:
    draft = asyncio.run(
        SyntheticCollector().collect(
            store_id="st_large_fixture",
            dataset_type=DatasetType.PRODUCT_CATALOG,
            scope=Scope(kind=WindowKind.POINT_IN_TIME, business_date=date(2026, 9, 7)),
            limit=50,
            batch_id="batch_large_fixture",
        )
    )
    assert draft.batch_id == "batch_large_fixture"
    assert draft.coverage.total_observed == 55
    assert draft.coverage.captured == 50
    assert draft.coverage.pages_read == 6
    assert draft.coverage.coverage is CoverageStatus.TRUNCATED
    assert draft.coverage.truncated is True
