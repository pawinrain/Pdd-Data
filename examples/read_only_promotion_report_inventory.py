from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlsplit

from playwright.async_api import Response, async_playwright

from pdd_data_mcp.browser.cdp import same_page_url

CDP_ENDPOINT = "http://127.0.0.1:9222"
PROMOTION_URL = "https://yingxiao.pinduoduo.com/goods/promotion/list"
REPORT_URL = "https://yingxiao.pinduoduo.com/goods/report/promotion/overView"
ALLOWED_HOST = "yingxiao.pinduoduo.com"
MAX_ENTRIES = 300


def _path(value: str) -> str | None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or parsed.hostname != ALLOWED_HOST:
        return None
    return re.sub(r"\d{4,}", "{id}", parsed.path)[:512]


async def inspect() -> None:
    manager = await async_playwright().start()
    browser = await manager.chromium.connect_over_cdp(CDP_ENDPOINT)
    metadata: set[tuple[str, str, int, str]] = set()

    def on_response(response: Response) -> None:
        path = _path(response.url)
        content_type = response.headers.get("content-type", "").partition(";")[0].casefold()
        if path and len(metadata) < MAX_ENTRIES:
            metadata.add((path, response.request.method, response.status, content_type))

    try:
        pages = [page for context in browser.contexts for page in context.pages]
        matching = [page for page in pages if same_page_url(page.url, PROMOTION_URL)]
        if len(matching) != 1:
            raise RuntimeError("exactly one promotion page must be open")
        page = matching[0]
        page.on("response", on_response)
        try:
            await page.goto(REPORT_URL, wait_until="domcontentloaded", timeout=20_000)
            await page.wait_for_timeout(5_000)
            body = await page.locator("body").inner_text(timeout=10_000)
            if any(term in body for term in ("请登录", "登录后", "安全验证", "验证码", "风险提示")):
                raise RuntimeError("authentication or risk gate is visible")
            dimension_candidates = await page.evaluate(
                r"""() => Array.from(document.querySelectorAll('body *'))
                    .filter(node => {
                        const text = (node.innerText || '').trim();
                        if (!/推广商品|计划/.test(text) || text.length > 40) return false;
                        return !Array.from(node.children).some(child =>
                            (child.innerText || '').trim() === text);
                    })
                    .slice(0, 50)
                    .map(node => ({
                        text: (node.innerText || '').replace(/\d/g, '#'),
                        tag: node.tagName.toLowerCase(),
                        testid: node.getAttribute('data-testid') || '',
                        role: node.getAttribute('role') || '',
                        aria: node.getAttribute('aria-label') || '',
                        className: String(node.className || '').slice(0, 160)
                    }))"""
            )
            result = {
                "status": "PASS",
                "target_path": urlsplit(page.url).path,
                "visible_semantics": {
                    label: label in body
                    for label in (
                        "计划",
                        "推广商品",
                        "账户",
                        "报表",
                        "分日",
                        "按日",
                        "今日",
                        "昨日",
                        "近7日",
                        "近30日",
                        "近90日",
                    )
                },
                "responses": [
                    {
                        "path": path,
                        "method": method,
                        "status": status,
                        "content_type": content_type,
                    }
                    for path, method, status, content_type in sorted(metadata)
                ],
                "dimension_candidates": dimension_candidates,
                "request_bodies_read": False,
                "response_bodies_read": False,
                "query_values_printed": False,
                "browser_closed": False,
            }
            print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
        finally:
            page.remove_listener("response", on_response)
            if not same_page_url(page.url, PROMOTION_URL):
                await page.goto(PROMOTION_URL, wait_until="domcontentloaded", timeout=20_000)
    finally:
        await manager.stop()


if __name__ == "__main__":
    asyncio.run(inspect())
