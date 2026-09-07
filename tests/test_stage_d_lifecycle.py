from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from pdd_data_mcp.browser.core import CoreDataCdpCollector
from pdd_data_mcp.browser.core_parsing import ParsedStoreOverview
from pdd_data_mcp.config import (
    CollectionSettings,
    ConnectionSettings,
    InventoryAdapterSettings,
    ProductCatalogAdapterSettings,
    StoreMetricSettings,
    StoreOverviewAdapterSettings,
)
from pdd_data_mcp.contracts.models import CoverageStatus, DatasetType, Scope, WindowKind
from pdd_data_mcp.errors import CollectionRejected
from pdd_data_mcp.validation import SnapshotValidator


class FakeRequest:
    method = "POST"


class FakeResponse:
    def __init__(self, body: dict[str, Any], path: str = "/api/data") -> None:
        self.url = f"http://127.0.0.1:8765{path}"
        self.status = 200
        self.headers = {"content-type": "application/json; charset=utf-8"}
        self.request = FakeRequest()
        self._body = json.dumps(body).encode()
        self.body_reads = 0

    async def body(self) -> bytes:
        self.body_reads += 1
        await asyncio.sleep(0)
        return self._body


class FakeLocator:
    def __init__(
        self,
        value: str | None,
        *,
        click: Any | None = None,
        attribute: str | None = None,
    ) -> None:
        self.value = value
        self._click = click
        self.attribute = attribute

    async def count(self) -> int:
        return 1 if self.value is not None or self._click is not None else 0

    async def text_content(self) -> str | None:
        return self.value

    async def get_attribute(self, name: str) -> str | None:
        del name
        return self.attribute

    async def click(self, **kwargs: Any) -> None:
        del kwargs
        if self._click is None:
            raise AssertionError("not clickable")
        await self._click()


class FakePage:
    def __init__(
        self,
        batches: list[list[FakeResponse]],
        locators: dict[str, FakeLocator],
    ) -> None:
        self.url = "http://127.0.0.1:8765/page"
        self.batches = batches
        self.locators = locators
        self.listeners: list[Any] = []
        self.batch_index = 0
        self.locators["#next"] = FakeLocator(None, click=self.dispatch_next)

    def is_closed(self) -> bool:
        return False

    def locator(self, selector: str) -> FakeLocator:
        return self.locators.get(selector, FakeLocator(None))

    def on(self, event: str, callback: Any) -> None:
        assert event == "response"
        self.listeners.append(callback)

    def remove_listener(self, event: str, callback: Any) -> None:
        assert event == "response"
        self.listeners.remove(callback)

    async def _dispatch(self) -> None:
        batch = self.batches[self.batch_index]
        self.batch_index += 1
        for response in batch:
            for callback in list(self.listeners):
                callback(response)
        await asyncio.sleep(0)

    async def reload(self, **kwargs: Any) -> None:
        del kwargs
        await self._dispatch()

    async def dispatch_next(self) -> None:
        await self._dispatch()


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.pages = [page]


class FakeBrowser:
    def __init__(self, page: FakePage) -> None:
        self.contexts = [FakeContext(page)]


class FakeSession:
    def __init__(self, page: FakePage) -> None:
        self.browser = FakeBrowser(page)
        self.disconnected = False

    async def disconnect(self) -> None:
        self.disconnected = True


class FakeConnector:
    def __init__(self, session: FakeSession) -> None:
        self.session = session

    async def connect(self, connection: ConnectionSettings, timeout_ms: int) -> FakeSession:
        del connection, timeout_ms
        return self.session


def today() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def common() -> dict[str, object]:
    return {
        "verified": True,
        "target_page_url": "http://127.0.0.1:8765/page",
        "response_host": "127.0.0.1",
        "response_path": "/api/data",
        "response_method": "POST",
        "business_success_path": "success",
        "identity_source": "MAIN_RESPONSE",
        "platform_store_id_path": "result.mallId",
        "identity_verification_reference": "fixture-main-response-v1",
        "parser_version": "fixture/1.0.0",
        "trigger": "RELOAD",
    }


def store_adapter() -> StoreOverviewAdapterSettings:
    return StoreOverviewAdapterSettings.model_validate(
        {
            **common(),
            "business_date_path": "result.date",
            "dom_business_date_selector": "#today",
            "dom_today_label": "TODAY",
            "metrics": {
                "gmv": StoreMetricSettings(
                    response_path="result.gmv",
                    source_unit="CNY",
                    output_unit="CNY_CENT",
                    dom_selector="#gmv",
                ),
                "orders": StoreMetricSettings(
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
            **common(),
            "list_path": "result.items",
            "total_path": "result.total",
            "product_id_path": "goodsId",
            "product_name_path": "name",
            "price_path": "price",
            "price_unit": "CNY_CENT",
            "dom_total_selector": "#total",
            "next_page_selector": "#next",
        }
    )


def inventory_adapter() -> InventoryAdapterSettings:
    return InventoryAdapterSettings.model_validate(
        {
            **common(),
            "list_path": "result.items",
            "total_path": "result.total",
            "product_id_path": "goodsId",
            "inventory_path": "quantity",
            "dom_total_selector": "#total",
        }
    )


def connection(**updates: object) -> ConnectionSettings:
    values: dict[str, object] = {
        "connection_id": "conn_current",
        "store_id": "st_current_store",
        "cdp_endpoint": "http://127.0.0.1:9222",
        "expected_platform_store_id": "1001",
        "target_page_url": "http://127.0.0.1:8765/promotion",
        "real_collection_enabled": True,
        "store_overview_adapter": store_adapter(),
        "product_catalog_adapter": product_adapter(),
        "inventory_adapter": inventory_adapter(),
    }
    values.update(updates)
    return ConnectionSettings.model_validate(values)


def scope(kind: WindowKind) -> Scope:
    return Scope(kind=kind, business_date=datetime.now(ZoneInfo("Asia/Shanghai")).date())


def collector(page: FakePage, tmp_path: Path) -> tuple[CoreDataCdpCollector, FakeSession]:
    session = FakeSession(page)
    return (
        CoreDataCdpCollector(
            connection=connection(),
            collection=CollectionSettings(min_interval_seconds=0, collection_timeout_ms=1000),
            runtime_root=tmp_path,
            connector=FakeConnector(session),  # type: ignore[arg-type]
        ),
        session,
    )


def test_store_network_and_two_dom_fields_build_valid_today_draft(tmp_path: Path) -> None:
    response = FakeResponse(
        {
            "success": True,
            "result": {"mallId": 1001, "date": today(), "gmv": "19.90", "orders": 0},
        }
    )
    page = FakePage(
        [[FakeResponse({"ignored": True}, "/api/ignored"), response]],
        {
            "#today": FakeLocator("TODAY"),
            "#gmv": FakeLocator("19.90"),
            "#orders": FakeLocator("0"),
        },
    )
    instance, session = collector(page, tmp_path)
    draft = asyncio.run(
        instance.collect(
            store_id="st_current_store",
            dataset_type=DatasetType.STORE_OVERVIEW,
            scope=scope(WindowKind.TODAY),
            limit=50,
            batch_id="batch_current",
        )
    )
    assert draft.batch_id == "batch_current"
    assert draft.metric_window is not None and draft.metric_window.window_complete is False
    assert draft.payload["metrics"]["orders"]["value"] == 0
    assert response.body_reads == 1
    assert page.listeners == [] and session.disconnected is True
    assert SnapshotValidator().validate(draft, synthetic_allowed=False).valid is True


def test_store_dom_source_records_platform_update_time(tmp_path: Path) -> None:
    adapter = StoreOverviewAdapterSettings.model_validate(
        {
            **common(),
            "data_source": "DOM",
            "identity_source": "MERCHANT_PAGE_STATE_SHA256",
            "identity_page_url": "http://127.0.0.1:8765/identity",
            "business_date_path": "",
            "dom_business_date_selector": "#updated",
            "dom_business_date_format": "CONTAINS_ISO_DATETIME_SECONDS",
            "dom_today_label": "实时数据更新时间:",
            "metrics": {
                "gmv": StoreMetricSettings(
                    source_unit="CNY",
                    output_unit="CNY_CENT",
                    dom_selector="#gmv",
                ),
                "orders": StoreMetricSettings(
                    source_unit="COUNT",
                    output_unit="COUNT",
                    dom_selector="#orders",
                ),
            },
        }
    )
    page = FakePage(
        [],
        {
            "#updated": FakeLocator(f"实时数据更新时间: {today()} 02:00:00"),
            "#gmv": FakeLocator("19.90"),
            "#orders": FakeLocator("0"),
        },
    )
    instance, _ = collector(page, tmp_path)
    observed_at = datetime.now(ZoneInfo("UTC"))
    metrics, sources, capture_method, source_updated_at = asyncio.run(
        instance._read_and_crosscheck_store_dom(
            page,
            adapter,
            ParsedStoreOverview(
                metrics={"gmv": None, "orders": None},
                business_date=scope(WindowKind.TODAY).business_date,
            ),
            scope(WindowKind.TODAY),
            observed_at,
        )
    )
    assert source_updated_at is not None and source_updated_at.tzinfo is not None
    assert capture_method == "DOM"
    assert set(sources.values()) == {"DOM"}
    assert all(
        metric is not None and metric.source_updated_at == source_updated_at
        for metric in metrics.values()
    )


def test_product_pagination_reads_three_and_rejects_cross_page_duplicate(tmp_path: Path) -> None:
    first = FakeResponse(
        {
            "success": True,
            "result": {
                "mallId": 1001,
                "total": 3,
                "items": [{"goodsId": 101, "name": "A", "price": 100}],
            },
        }
    )
    second = FakeResponse(
        {
            "success": True,
            "result": {
                "mallId": 1001,
                "total": 3,
                "items": [
                    {"goodsId": 102, "name": "B", "price": 200},
                    {"goodsId": 103, "name": "C", "price": 300},
                ],
            },
        }
    )
    page = FakePage([[first], [second]], {"#total": FakeLocator("共3个商品")})
    instance, _ = collector(page, tmp_path)
    draft = asyncio.run(
        instance.collect(
            store_id="st_current_store",
            dataset_type=DatasetType.PRODUCT_CATALOG,
            scope=scope(WindowKind.POINT_IN_TIME),
            limit=50,
        )
    )
    assert draft.coverage.total_observed == 3
    assert draft.coverage.captured == 3
    assert draft.coverage.pages_read == 2
    assert draft.coverage.coverage is CoverageStatus.COMPLETE
    assert SnapshotValidator().validate(draft, synthetic_allowed=False).valid is True

    duplicate_page = FakePage([[first], [first]], {"#total": FakeLocator("共3个商品")})
    duplicate_collector, _ = collector(duplicate_page, tmp_path / "duplicate")
    with pytest.raises(CollectionRejected, match="DUPLICATE_PRODUCT_ACROSS_PAGES"):
        asyncio.run(
            duplicate_collector.collect(
                store_id="st_current_store",
                dataset_type=DatasetType.PRODUCT_CATALOG,
                scope=scope(WindowKind.POINT_IN_TIME),
                limit=50,
            )
        )


def test_inventory_zero_null_and_mapping_are_preserved(tmp_path: Path) -> None:
    page = FakePage(
        [
            [
                FakeResponse(
                    {
                        "success": True,
                        "result": {
                            "mallId": 1001,
                            "total": 3,
                            "items": [
                                {"goodsId": 101, "quantity": 0},
                                {"goodsId": 102, "quantity": None},
                                {"goodsId": 103, "quantity": 8},
                            ],
                        },
                    }
                )
            ]
        ],
        {"#total": FakeLocator("3")},
    )
    instance, _ = collector(page, tmp_path)
    draft = asyncio.run(
        instance.collect(
            store_id="st_current_store",
            dataset_type=DatasetType.INVENTORY,
            scope=scope(WindowKind.POINT_IN_TIME),
            limit=50,
        )
    )
    assert [item["inventory"] for item in draft.payload] == [0, None, 8]
    assert draft.missing_fields == ["records[1].inventory"]
    assert draft.coverage.coverage is CoverageStatus.COMPLETE
    assert SnapshotValidator().validate(draft, synthetic_allowed=False).valid is True


def test_current_store_identity_uses_merchant_page_state_fingerprint(tmp_path: Path) -> None:
    raw_store_id = "123456789"
    digest = hashlib.sha256(raw_store_id.encode()).hexdigest()
    identity_page = FakePage([], {"html": FakeLocator(f"bootstrap:{raw_store_id}")})
    identity_page.url = "http://127.0.0.1:8765/identity"
    adapter = product_adapter().model_copy(
        update={
            "identity_source": "MERCHANT_PAGE_STATE_SHA256",
            "identity_page_url": identity_page.url,
            "identity_verification_reference": "merchant-bootstrap-fixture-v1",
        }
    )
    current = connection(
        expected_platform_store_id="",
        expected_platform_store_id_sha256=digest,
        product_catalog_adapter=adapter,
    )
    instance = CoreDataCdpCollector(
        connection=current,
        collection=CollectionSettings(min_interval_seconds=0),
        runtime_root=tmp_path,
    )
    observed = asyncio.run(instance._merchant_page_identity(FakeBrowser(identity_page), adapter))
    assert observed == f"sha256:{digest}"
