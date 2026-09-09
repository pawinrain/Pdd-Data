from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Response, async_playwright

from pdd_data_mcp.browser.cdp import same_page_url
from pdd_data_mcp.browser.promotion import sanitized_json_shape

CDP_ENDPOINT = "http://127.0.0.1:9222"
PROMOTION_URL = "https://yingxiao.pinduoduo.com/goods/promotion/list"
REPORT_URL = "https://yingxiao.pinduoduo.com/goods/report/promotion/overView"
HOST = "yingxiao.pinduoduo.com"
PATH = "/mms-gateway/poseidon/api/report/queryEntityReport"
MAX_RESPONSE_BYTES = 1_048_576


def _matches(response: Response) -> bool:
    parsed = urlsplit(response.url)
    return (
        parsed.hostname == HOST
        and parsed.path == PATH
        and response.request.method == "POST"
        and response.status == 200
        and response.headers.get("content-type", "").partition(";")[0].casefold()
        == "application/json"
    )


async def _json_body(response: Response) -> dict[str, Any]:
    raw = await response.body()
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RuntimeError("verified response exceeds one MiB")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError("verified response is not an object")
    return value


async def inspect() -> None:
    manager = await async_playwright().start()
    browser = await manager.chromium.connect_over_cdp(CDP_ENDPOINT)
    try:
        pages = [page for context in browser.contexts for page in context.pages]
        matching = [page for page in pages if same_page_url(page.url, PROMOTION_URL)]
        if len(matching) != 1:
            raise RuntimeError("exactly one promotion page must be open")
        page = matching[0]
        try:
            async with page.expect_response(_matches, timeout=20_000) as info:
                await page.goto(REPORT_URL, wait_until="domcontentloaded", timeout=20_000)
            response = await info.value
            request = response.request.post_data_json
            payload = await _json_body(response)
            if not isinstance(request, dict):
                raise RuntimeError("verified request is not an object")
            body = await page.locator("body").inner_text(timeout=10_000)
            if any(term in body for term in ("请登录", "安全验证", "验证码", "风险提示")):
                raise RuntimeError("authentication or risk gate is visible")
            result = payload.get("result")
            rows = result.get("entityReportList") if isinstance(result, dict) else None
            query_range = request.get("queryRange")
            print(
                json.dumps(
                    {
                        "status": "PASS",
                        "request_shape": sanitized_json_shape(request),
                        "response_shape": sanitized_json_shape(payload),
                        "request_discriminator": {
                            key: request.get(key)
                            for key in (
                                "entityDimensionType",
                                "queryDimensionType",
                                "reportPromotionType",
                                "returnTotalSumReport",
                            )
                            if isinstance(request.get(key), bool | int)
                        },
                        "page": {
                            key: query_range.get(key)
                            for key in ("pageNumber", "pageSize")
                            if isinstance(query_range, dict)
                            and isinstance(query_range.get(key), int)
                        },
                        "entity_rows": len(rows) if isinstance(rows, list) else None,
                        "total": (
                            result.get("total")
                            if isinstance(result, dict) and isinstance(result.get("total"), int)
                            else None
                        ),
                        "raw_values_printed": False,
                        "raw_bodies_persisted": False,
                        "browser_closed": False,
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                )
            )
        finally:
            if not same_page_url(page.url, PROMOTION_URL):
                await page.goto(PROMOTION_URL, wait_until="domcontentloaded", timeout=20_000)
    finally:
        await manager.stop()


if __name__ == "__main__":
    asyncio.run(inspect())
