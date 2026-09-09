from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from examples import read_only_product_business_page_inventory as inventory_module
from examples.read_only_product_business_page_inventory import (
    InventoryRuntime,
    InventoryStopped,
    _error_payload,
    _inventory_open_pages,
    _is_trusted_loopback_endpoint,
    _load_runtime,
    _safe_page_reference,
    inventory,
)


class FakePage:
    def __init__(self, url: str, *, closed: bool = False) -> None:
        self.url = url
        self._closed = closed

    def is_closed(self) -> bool:
        return self._closed


class FakeContext:
    def __init__(self, pages: list[FakePage]) -> None:
        self.pages = pages


class FakeBrowser:
    def __init__(self, contexts: list[FakeContext]) -> None:
        self.contexts = contexts


class FakeChromium:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        self.calls: list[dict[str, object]] = []

    async def connect_over_cdp(self, endpoint: str, **kwargs: object) -> FakeBrowser:
        self.calls.append({"endpoint": endpoint, **kwargs})
        return self.browser


class FakeManager:
    def __init__(self, browser: FakeBrowser) -> None:
        self.chromium = FakeChromium(browser)
        self.stop_calls = 0

    async def stop(self) -> None:
        self.stop_calls += 1


class FakePlaywrightStarter:
    def __init__(self, manager: FakeManager) -> None:
        self.manager = manager

    async def start(self) -> FakeManager:
        return self.manager


def test_safe_page_reference_keeps_only_exact_allowed_host_and_sanitized_path() -> None:
    reference = _safe_page_reference(
        "https://mms.pinduoduo.com/goods/item/123456?token=must-not-output#secret"
    )

    assert reference == {"host": "mms.pinduoduo.com", "path": "/goods/item/{id}"}
    assert _safe_page_reference("https://notpinduoduo.com/goods/item/123456") is None
    assert _safe_page_reference("https://mms.pinduoduo.com.evil.test/goods") is None
    assert _safe_page_reference("http://mms.pinduoduo.com/goods") is None
    assert _safe_page_reference("https://user@mms.pinduoduo.com/goods") is None
    assert _safe_page_reference("https://mms.pinduoduo.com:9443/goods") is None


def test_inventory_lists_only_safe_fields_and_performs_no_page_operations() -> None:
    browser = FakeBrowser(
        [
            FakeContext(
                [
                    FakePage(
                        "https://mms.pinduoduo.com/goods/item/123456"
                        "?store=secret#sensitive-fragment-value"
                    ),
                    FakePage("https://yingxiao.pinduoduo.com/mains/promotionOverview"),
                    FakePage("https://example.com/private/path"),
                    FakePage("https://mms.pinduoduo.com/closed", closed=True),
                ]
            ),
            FakeContext([FakePage("about:blank")]),
        ]
    )

    result = asyncio.run(_inventory_open_pages(cast(Any, browser)))

    assert result["status"] == "PASS"
    assert result["context_count"] == 2
    assert result["page_slot_count"] == 5
    assert result["open_page_count"] == 4
    assert result["closed_page_count"] == 1
    assert result["allowed_pdd_page_count"] == 2
    assert result["excluded_open_page_count"] == 2
    assert result["pages"] == [
        {
            "context_index": 0,
            "page_index": 0,
            "host": "mms.pinduoduo.com",
            "path": "/goods/item/{id}",
        },
        {
            "context_index": 0,
            "page_index": 1,
            "host": "yingxiao.pinduoduo.com",
            "path": "/mains/promotionOverview",
        },
    ]
    assert all(
        set(row) == {"context_index", "page_index", "host", "path"}
        for row in cast(list[dict[str, object]], result["pages"])
    )
    serialized = json.dumps(result, ensure_ascii=False)
    assert "token" not in serialized
    assert "secret" not in serialized
    assert "sensitive-fragment-value" not in serialized
    assert set(cast(dict[str, int], result["safety_counts"]).values()) == {0}


def test_inventory_connects_read_only_and_stops_only_playwright_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = FakeBrowser([FakeContext([FakePage("https://mms.pinduoduo.com/home")])])
    manager = FakeManager(browser)
    monkeypatch.setattr(
        inventory_module,
        "async_playwright",
        lambda: FakePlaywrightStarter(manager),
    )

    result = asyncio.run(
        inventory(InventoryRuntime("http://127.0.0.1:9222", connect_timeout_ms=4321))
    )

    assert result["allowed_pdd_page_count"] == 1
    assert manager.chromium.calls == [
        {
            "endpoint": "http://127.0.0.1:9222",
            "timeout": 4321,
            "is_local": True,
            "no_defaults": True,
        }
    ]
    assert manager.stop_calls == 1


@pytest.mark.parametrize(
    "endpoint,expected",
    [
        ("http://127.0.0.1:9222", True),
        ("ws://[::1]:9222/devtools/browser/value", True),
        ("http://localhost:9222", True),
        ("https://127.0.0.1:9222", False),
        ("http://0.0.0.0:9222", False),
        ("http://127.0.0.1", False),
        ("http://user@127.0.0.1:9222", False),
        ("http://127.0.0.1:9222?token=secret", False),
    ],
)
def test_trusted_loopback_endpoint_gate(endpoint: str, expected: bool) -> None:
    assert _is_trusted_loopback_endpoint(endpoint) is expected


def test_runtime_is_loaded_by_connection_id_without_exposing_config_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = SimpleNamespace(
        real_collection_enabled=True,
        cdp_endpoint="http://127.0.0.1:9222",
    )
    config = SimpleNamespace(
        service=SimpleNamespace(test_mode=False),
        collection=SimpleNamespace(connect_timeout_ms=1234),
        connection=lambda connection_id: connection
        if connection_id == "conn_current"
        else (_ for _ in ()).throw(KeyError(connection_id)),
    )
    monkeypatch.setattr(inventory_module, "load_config", lambda path: config)

    runtime = _load_runtime(Path("unused.toml"), "conn_current")

    assert runtime.cdp_endpoint == "http://127.0.0.1:9222"
    assert runtime.connect_timeout_ms == 1234


def test_runtime_stops_on_unknown_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    config = SimpleNamespace(
        service=SimpleNamespace(test_mode=False),
        collection=SimpleNamespace(connect_timeout_ms=1234),
        connection=lambda connection_id: (_ for _ in ()).throw(KeyError(connection_id)),
    )
    monkeypatch.setattr(inventory_module, "load_config", lambda path: config)

    with pytest.raises(InventoryStopped, match="CONNECTION_ID_NOT_FOUND") as raised:
        _load_runtime(Path("unused.toml"), "conn_missing")

    assert raised.value.code == "CONNECTION_ID_NOT_FOUND"
    assert _error_payload(raised.value.code)["pages"] == []
