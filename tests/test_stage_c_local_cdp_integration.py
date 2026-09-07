from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from mcp import Client, StdioServerParameters
from playwright.async_api import async_playwright


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def business_date() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


class LocalPromotionHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def do_GET(self) -> None:
        path = self.path.partition("?")[0]
        if path == "/mains/promotionOverview":
            body = f"""<!doctype html><html><body>
<div id="store" data-store-id="platform-store-local"></div>
<div id="business-date">{business_date()}</div>
<div id="ad-spend">123.45</div>
<script>fetch('/api/promotion/overview?nonce=' + Date.now());</script>
</body></html>""".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif path == "/home":
            body = b"""<!doctype html><html><body>
<div id="store-date">TODAY</div><div id="gmv">19.90</div><div id="orders">2</div>
<script>fetch('/api/store-overview?nonce=' + Date.now());</script>
</body></html>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif path == "/goods/goods_list":
            body = b"""<!doctype html><html><body>
<div id="product-total">3</div><div id="inventory-total">3</div>
<script>
fetch('/api/products?nonce=' + Date.now());
fetch('/api/inventory?nonce=' + Date.now());
</script>
</body></html>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif path == "/api/promotion/overview":
            body = json.dumps(
                {
                    "result": {
                        "success": True,
                        "store_id": "platform-store-local",
                        "business_date": business_date(),
                        "ad_spend_cents": 12345,
                        "updated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                    }
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
        elif path == "/api/store-overview":
            body = json.dumps(
                {
                    "success": True,
                    "result": {
                        "mallId": "platform-store-local",
                        "date": business_date(),
                        "gmv": "19.90",
                        "orders": 2,
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
        elif path == "/api/products":
            body = json.dumps(
                {
                    "success": True,
                    "result": {
                        "mallId": "platform-store-local",
                        "total": 3,
                        "items": [
                            {"goodsId": "101", "name": "A", "price": 100},
                            {"goodsId": "102", "name": "B", "price": 200},
                            {"goodsId": "103", "name": "C", "price": 300},
                        ],
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
        elif path == "/api/inventory":
            body = json.dumps(
                {
                    "success": True,
                    "result": {
                        "mallId": "platform-store-local",
                        "total": 3,
                        "items": [
                            {"goodsId": "101", "quantity": 0},
                            {"goodsId": "102", "quantity": None},
                            {"goodsId": "103", "quantity": 8},
                        ],
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
        else:
            body = b"not found"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def write_real_test_config(path: Path, *, page_port: int, cdp_port: int) -> None:
    content = f'''[service]
name = "pdd-data-mcp"
transport = "stdio"
mode = "read_only"
test_mode = true

[storage]
backend = "local_files"
data_root = "{(path.parent / "real-data").as_posix()}"
runtime_root = "{(path.parent / "real-runtime").as_posix()}"
partition_timezone = "Asia/Shanghai"
layout_version = "1"
max_bytes = 1073741824
min_free_bytes = 0
retain_committed_snapshots = true
raw_response_persistence = false

[collection]
max_concurrency = 1
max_products = 50
connect_timeout_ms = 5000
collection_timeout_ms = 5000
min_interval_seconds = 0
platform_auto_retries = 0
max_mcp_response_bytes = 262144
max_browser_response_bytes = 1048576
max_inflight_responses = 4

[[connections]]
connection_id = "conn_local_cdp"
store_id = "st_local_real"
cdp_endpoint = "http://127.0.0.1:{cdp_port}"
expected_platform_store_id = "platform-store-local"
expected_platform_store_id_sha256 = ""
target_page_url = "http://127.0.0.1:{page_port}/mains/promotionOverview"
real_collection_enabled = true
synthetic_enabled = false

[connections.discovery]
enabled = false
observe_seconds = 5
max_metadata_entries = 20
candidate_body_probe_enabled = false
candidate_response_host = ""
candidate_response_paths = []
candidate_response_method = "POST"

[connections.promotion_adapter]
verified = true
response_host = "127.0.0.1"
response_path = "/api/promotion/overview"
response_method = "GET"
response_http_status = 200
response_content_type = "application/json"
business_success_path = "result.success"
business_success_value = true
platform_store_id_path = "result.store_id"
identity_response_host = ""
identity_response_path = ""
identity_response_method = "POST"
identity_response_http_status = 200
identity_business_success_path = ""
identity_business_success_value = true
identity_platform_store_id_path = ""
metric_list_path = ""
metric_item_business_date_path = ""
metric_item_date_format = "ISO_DATE"
business_date_path = "result.business_date"
ad_spend_path = "result.ad_spend_cents"
ad_spend_unit = "CNY_CENT"
ad_spend_unit_path = ""
ad_spend_expected_unit_value = ""
source_updated_at_path = "result.updated_at"
parser_version = "local-cdp/1.0.0"
trigger = "RELOAD"
dom_fallback_enabled = false
dom_store_id_selector = "#store"
dom_store_id_attribute = "data-store-id"
dom_business_date_selector = "#business-date"
dom_business_date_attribute = ""
dom_today_label = ""
dom_ad_spend_selector = "#ad-spend"
dom_ad_spend_attribute = ""
dom_ad_spend_unit = "CNY"
login_selector = "#login-required"
captcha_selector = "#captcha"
error_selector = "#page-error"

[connections.store_overview_adapter]
verified = true
target_page_url = "http://127.0.0.1:{page_port}/home"
response_host = "127.0.0.1"
response_path = "/api/store-overview"
response_method = "GET"
business_success_path = "success"
business_success_value = true
identity_source = "MAIN_RESPONSE"
platform_store_id_path = "result.mallId"
identity_verification_reference = "local-main-response-v1"
business_date_path = "result.date"
business_date_format = "ISO_DATE"
dom_business_date_selector = "#store-date"
dom_today_label = "TODAY"
parser_version = "local-store/1.0.0"
trigger = "RELOAD"

[connections.store_overview_adapter.metrics.gmv]
response_path = "result.gmv"
source_unit = "CNY"
output_unit = "CNY_CENT"
dom_selector = "#gmv"

[connections.store_overview_adapter.metrics.order_count]
response_path = "result.orders"
source_unit = "COUNT"
output_unit = "COUNT"
dom_selector = "#orders"

[connections.product_catalog_adapter]
verified = true
target_page_url = "http://127.0.0.1:{page_port}/goods/goods_list"
response_host = "127.0.0.1"
response_path = "/api/products"
response_method = "GET"
business_success_path = "success"
business_success_value = true
identity_source = "MAIN_RESPONSE"
platform_store_id_path = "result.mallId"
identity_verification_reference = "local-main-response-v1"
list_path = "result.items"
total_path = "result.total"
product_id_path = "goodsId"
product_name_path = "name"
price_path = "price"
price_unit = "CNY_CENT"
dom_total_selector = "#product-total"
parser_version = "local-products/1.0.0"
trigger = "RELOAD"

[connections.inventory_adapter]
verified = true
target_page_url = "http://127.0.0.1:{page_port}/goods/goods_list"
response_host = "127.0.0.1"
response_path = "/api/inventory"
response_method = "GET"
business_success_path = "success"
business_success_value = true
identity_source = "MAIN_RESPONSE"
platform_store_id_path = "result.mallId"
identity_verification_reference = "local-main-response-v1"
list_path = "result.items"
total_path = "result.total"
product_id_path = "goodsId"
inventory_path = "quantity"
granularity = "PRODUCT"
dom_total_selector = "#inventory-total"
parser_version = "local-inventory/1.0.0"
trigger = "RELOAD"
'''
    path.write_text(content, encoding="utf-8")


async def chromium_executable() -> str:
    async with async_playwright() as playwright:
        bundled = Path(playwright.chromium.executable_path)
    if bundled.is_file():
        return str(bundled)
    if sys.platform == "win32":
        program_files = os.environ.get("PROGRAMFILES")
        if program_files:
            installed = Path(program_files) / "Google" / "Chrome" / "Application" / "chrome.exe"
            if installed.is_file():
                return str(installed)
    raise RuntimeError("no Chromium executable available for the isolated local-CDP test")


def wait_for_cdp(port: int, process: subprocess.Popen[bytes]) -> None:
    endpoint = f"http://127.0.0.1:{port}/json/version"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("local Chromium exited before CDP became ready")
        try:
            with urllib.request.urlopen(endpoint, timeout=0.5) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("local CDP endpoint did not become ready")


async def open_local_pages(cdp_port: int, page_port: int) -> None:
    manager = await async_playwright().start()
    browser = await manager.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
    context = browser.contexts[0]
    for path in ("/home", "/goods/goods_list"):
        page = await context.new_page()
        await page.goto(f"http://127.0.0.1:{page_port}{path}", wait_until="domcontentloaded")
    await manager.stop()


def mcp_parameters(config: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "pdd_data_mcp.cli", "serve", "--config", str(config)],
        cwd=str(Path(__file__).resolve().parents[1]),
    )


def structured(result: Any) -> dict[str, Any]:
    assert result.is_error is False
    assert isinstance(result.structured_content, dict)
    return result.structured_content


async def exercise_real_local_flow(config: Path) -> list[str]:
    today_scope = {
        "kind": "TODAY",
        "business_date": business_date(),
        "timezone": "Asia/Shanghai",
        "object_type": "ACCOUNT_ALL",
        "filters": {},
        "currency": "CNY",
        "attribution": "PLATFORM_DEFAULT",
        "version": "1",
        "start": None,
        "end": None,
    }
    point_scope = {**today_scope, "kind": "POINT_IN_TIME"}
    calls = {
        "promotion_overview": {"scope": today_scope, "key": "local-cdp-promotion"},
        "store_overview": {"scope": today_scope, "key": "local-cdp-store"},
        "product_catalog": {"scope": point_scope, "key": "local-cdp-products"},
        "inventory": {"scope": point_scope, "key": "local-cdp-inventory"},
    }
    snapshot_ids: list[str] = []
    async with Client(mcp_parameters(config), raise_exceptions=True) as client:
        capabilities = structured(await client.call_tool("pdd_get_capabilities", {}))
        assert capabilities["datasets"]["store_overview"] == "REAL_STORE_TODAY"
        for dataset, call in calls.items():
            collected = structured(
                await client.call_tool(
                    "pdd_collect_snapshot",
                    {
                        "connection_id": "conn_local_cdp",
                        "dataset_type": dataset,
                        "scope": call["scope"],
                        "limit": 50,
                        "idempotency_key": call["key"],
                        "batch_id": "batch_local_core",
                    },
                )
            )
            assert collected["status"] == "SUCCEEDED"
            snapshot_id = str(collected["snapshot_id"])
            snapshot_ids.append(snapshot_id)
            read = structured(
                await client.call_tool("pdd_read_snapshot", {"snapshot_id": snapshot_id})
            )
            assert read["manifest"]["source"] == "PDD_BROWSER_CDP"
            assert read["manifest"]["capture_method"] == "NETWORK_RESPONSE"
            assert read["manifest"]["quality"]["dom_check"] == "MATCHED"
    async with Client(mcp_parameters(config), raise_exceptions=True) as restarted:
        for snapshot_id, (dataset, call) in zip(snapshot_ids, calls.items(), strict=True):
            read = structured(
                await restarted.call_tool("pdd_read_snapshot", {"snapshot_id": snapshot_id})
            )
            replay = structured(
                await restarted.call_tool(
                    "pdd_collect_snapshot",
                    {
                        "connection_id": "conn_local_cdp",
                        "dataset_type": dataset,
                        "scope": call["scope"],
                        "limit": 50,
                        "idempotency_key": call["key"],
                        "batch_id": "batch_local_core",
                    },
                )
            )
            assert read["snapshot_id"] == snapshot_id
            assert replay["snapshot_id"] == snapshot_id
            assert replay["idempotent_replay"] is True
    return snapshot_ids


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Stage C local-CDP acceptance")
@pytest.mark.local_cdp
def test_real_mcp_to_local_cdp_snapshot_restart_and_browser_preserved(tmp_path: Path) -> None:
    page_port = free_port()
    cdp_port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", page_port), LocalPromotionHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    config = tmp_path / "real-test.toml"
    write_real_test_config(config, page_port=page_port, cdp_port=cdp_port)
    executable = asyncio.run(chromium_executable())
    profile = tmp_path / "chromium-profile"
    creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    browser = subprocess.Popen(
        [
            executable,
            "--headless=new",
            f"--remote-debugging-port={cdp_port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            f"http://127.0.0.1:{page_port}/mains/promotionOverview",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
    )
    try:
        wait_for_cdp(cdp_port, browser)
        asyncio.run(open_local_pages(cdp_port, page_port))
        snapshot_ids = asyncio.run(exercise_real_local_flow(config))
        assert browser.poll() is None, "collector disconnected by closing the test browser"
        for snapshot_id in snapshot_ids:
            snapshots = list((tmp_path / "real-data" / "snapshots").rglob(f"*{snapshot_id}"))
            assert len(snapshots) == 1
            assert (snapshots[0] / "COMMIT.json").is_file()
    finally:
        if browser.poll() is None:
            browser.terminate()
            browser.wait(timeout=10)
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
