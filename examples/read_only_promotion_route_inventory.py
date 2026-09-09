from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from pdd_data_mcp.browser.cdp import same_page_url

CDP_ENDPOINT = "http://127.0.0.1:9222"
PROMOTION_URL = "https://yingxiao.pinduoduo.com/goods/promotion/list"
MAX_ITEMS = 100
KEYWORDS = ("计划", "单元", "campaign", "plan")


def _safe_path(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.hostname != "yingxiao.pinduoduo.com":
        return None
    return re.sub(r"\d{4,}", "{id}", parsed.path)[:512]


def _safe_label(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = " ".join(value.split())
    return re.sub(r"\d", "#", normalized)[:80]


async def inspect() -> None:
    manager = await async_playwright().start()
    browser = await manager.chromium.connect_over_cdp(CDP_ENDPOINT)
    try:
        pages = [page for context in browser.contexts for page in context.pages]
        matching = [page for page in pages if same_page_url(page.url, PROMOTION_URL)]
        if len(matching) != 1:
            raise RuntimeError("exactly one promotion list page must be open")
        page = matching[0]
        body_text = await page.locator("body").inner_text(timeout=10_000)
        links = await page.locator("a").evaluate_all(
            """nodes => nodes.slice(0, 1000).map(node => ({
                text: node.innerText || '', href: node.href || ''
            }))"""
        )
        matching_links: list[dict[str, str]] = []
        all_navigation_paths: set[str] = set()
        route_labels: list[dict[str, str]] = []
        if isinstance(links, list):
            for item in links:
                if not isinstance(item, dict):
                    continue
                label = _safe_label(item.get("text"))
                path = _safe_path(item.get("href"))
                if path:
                    all_navigation_paths.add(path)
                    if path in {
                        "/goods/promotion/list",
                        "/goods/report/promotion/overView",
                        "/mains/promotionOverview",
                        "/tools",
                    }:
                        route_labels.append({"path": path, "label": label})
                combined = f"{label} {path or ''}".casefold()
                if path and any(keyword.casefold() in combined for keyword in KEYWORDS):
                    matching_links.append({"label": label, "path": path})
        resources = await page.evaluate(
            """() => performance.getEntriesByType('resource')
                .slice(-2000).map(entry => entry.name)"""
        )
        matching_paths = sorted(
            {
                path
                for value in resources
                if isinstance(resources, list)
                for path in [_safe_path(value)]
                if path and any(keyword.casefold() in path.casefold() for keyword in KEYWORDS)
            }
        )
        controls = await page.locator("button,[data-testid]").evaluate_all(
            """nodes => nodes.slice(0, 2000).map(node => ({
                text: node.innerText || '',
                testid: node.getAttribute('data-testid') || '',
                aria: node.getAttribute('aria-label') || '',
                disabled: Boolean(node.disabled) || node.getAttribute('aria-disabled') === 'true'
            }))"""
        )
        pagination_controls: list[dict[str, object]] = []
        if isinstance(controls, list):
            for item in controls:
                if not isinstance(item, dict):
                    continue
                label = _safe_label(item.get("text"))
                testid = _safe_label(item.get("testid"))
                aria = _safe_label(item.get("aria"))
                combined = f"{label} {testid} {aria}".casefold()
                if any(token in combined for token in ("page", "next", "prev", "页")):
                    pagination_controls.append(
                        {
                            "label": label,
                            "testid": testid,
                            "aria": aria,
                            "disabled": item.get("disabled") is True,
                        }
                    )
        date_options = await page.locator('[data-testid^="DateAreaQuickOption_"]').evaluate_all(
            """nodes => nodes.map(node => ({
                text: node.innerText || '',
                testid: node.getAttribute('data-testid') || '',
                ariaSelected: node.getAttribute('aria-selected'),
                className: typeof node.className === 'string' ? node.className : ''
            }))"""
        )
        print(
            json.dumps(
                {
                    "status": "PASS",
                    "body_has_plan_label": "计划" in body_text,
                    "body_has_unit_label": "推广单元" in body_text,
                    "matching_navigation": matching_links[:MAX_ITEMS],
                    "all_navigation_paths": sorted(all_navigation_paths)[:MAX_ITEMS],
                    "route_labels": route_labels[:MAX_ITEMS],
                    "matching_resource_paths": matching_paths[:MAX_ITEMS],
                    "pagination_controls": pagination_controls[:MAX_ITEMS],
                    "date_options": [
                        {
                            "label": _safe_label(item.get("text")),
                            "testid": _safe_label(item.get("testid")),
                            "aria_selected": item.get("ariaSelected"),
                            "class_tokens": sorted(
                                token
                                for token in str(item.get("className", "")).split()
                                if re.fullmatch(r"[A-Za-z_-]{1,40}", token)
                            )[:10],
                        }
                        for item in date_options
                        if isinstance(item, dict)
                    ],
                    "query_values_printed": False,
                    "raw_ids_printed": False,
                    "browser_closed": False,
                },
                ensure_ascii=True,
                separators=(",", ":"),
            )
        )
    finally:
        await manager.stop()


if __name__ == "__main__":
    asyncio.run(inspect())
