from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from urllib.parse import urlsplit

from playwright.async_api import Response, async_playwright

TARGET_URL = "https://mms.pinduoduo.com/goods/goods_list"
RESPONSE_PATH = "/vodka/v2/mms/query/display/mall/goodsList"
TOTAL_SELECTOR = "li[class*='PGT_totalText_']"


def _matches(response: Response) -> bool:
    parsed = urlsplit(response.url)
    return (
        parsed.hostname == "mms.pinduoduo.com"
        and parsed.path == RESPONSE_PATH
        and response.request.method == "POST"
        and response.status == 200
        and response.headers.get("content-type", "").partition(";")[0].casefold()
        == "application/json"
    )


def _kind(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


async def verify(expected_sha256: str) -> None:
    manager = await async_playwright().start()
    browser = await manager.chromium.connect_over_cdp("http://127.0.0.1:9222")
    try:
        pages = [page for context in browser.contexts for page in context.pages]
        page = next(
            (candidate for candidate in pages if candidate.url.rstrip("/") == TARGET_URL), None
        )
        if page is None:
            raise RuntimeError("exact product page is not open")
        async with page.expect_response(_matches, timeout=20_000) as response_info:
            await page.reload(wait_until="domcontentloaded", timeout=20_000)
        response = await response_info.value
        raw = await response.body()
        if len(raw) > 1_048_576:
            raise RuntimeError("response exceeds the configured one MiB boundary")
        payload = json.loads(raw)
        result = payload.get("result")
        items = result.get("goods_list") if isinstance(result, dict) else None
        if payload.get("success") is not True or not isinstance(items, list):
            raise RuntimeError("candidate response semantics are not verified")
        total = result.get("total")
        ids = [item.get("id") for item in items if isinstance(item, dict)]
        mall_ids = {
            hashlib.sha256(str(item.get("mall_id")).encode("utf-8")).hexdigest()
            for item in items
            if isinstance(item, dict) and isinstance(item.get("mall_id"), str | int)
        }
        quantities = [item.get("quantity") for item in items if isinstance(item, dict)]
        sku_rows = [
            sku
            for item in items
            if isinstance(item, dict) and isinstance(item.get("sku_list"), list)
            for sku in item["sku_list"]
            if isinstance(sku, dict)
        ]
        total_locator = page.locator(TOTAL_SELECTOR)
        dom_count = await total_locator.count()
        dom_text = await total_locator.first.text_content() if dom_count else None
        dom_numbers = re.findall(r"\d[\d,]*", dom_text or "")
        dom_total = int(dom_numbers[0].replace(",", "")) if len(dom_numbers) == 1 else None
        output = {
            "status": "PASS",
            "response_success": True,
            "platform_total": total,
            "response_list_count": len(items),
            "unique_product_count": len(set(map(str, ids))),
            "product_id_types": sorted({_kind(value) for value in ids}),
            "all_rows_have_one_store_identity": len(mall_ids) == 1 and len(items) > 0,
            "row_store_identity_matches_expected": mall_ids == {expected_sha256},
            "identity_value_printed": False,
            "field_types": {
                "is_onsale": sorted(
                    {_kind(item.get("is_onsale")) for item in items if isinstance(item, dict)}
                ),
                "market_price": sorted(
                    {_kind(item.get("market_price")) for item in items if isinstance(item, dict)}
                ),
                "sku_count": sorted(
                    {_kind(item.get("sku_count")) for item in items if isinstance(item, dict)}
                ),
                "quantity": sorted({_kind(value) for value in quantities}),
            },
            "inventory_zero_count": sum(value == 0 for value in quantities),
            "inventory_null_count": sum(value is None for value in quantities),
            "sku_row_count": len(sku_rows),
            "sku_inventory_types": sorted({_kind(sku.get("skuQuantity")) for sku in sku_rows}),
            "dom_total_selector_count": dom_count,
            "dom_total": dom_total,
            "network_dom_total_match": isinstance(total, int) and dom_total == total,
        }
        print(json.dumps(output, ensure_ascii=True, separators=(",", ":")))
    finally:
        await browser.close()
        await manager.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Read-only product response semantics gate.")
    parser.add_argument("--expected-sha256", required=True)
    arguments = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{64}", arguments.expected_sha256):
        parser.error("--expected-sha256 must be a lowercase SHA-256 digest")
    asyncio.run(verify(arguments.expected_sha256))
