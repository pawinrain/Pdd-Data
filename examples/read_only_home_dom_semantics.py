from __future__ import annotations

import asyncio
import json
import re

from playwright.async_api import async_playwright

from pdd_data_mcp.browser.cdp import same_page_url

TARGET_URL = "https://mms.pinduoduo.com/home"
LABELS = ["成交金额", "成交订单数", "商品访客数", "商品浏览量", "商品评价数"]


def _classify(text: str, label: str) -> str:
    compact = text.strip().replace(",", "")
    if compact == label:
        return "LABEL"
    if compact == "--":
        return "MISSING_MARKER"
    if re.fullmatch(r"\d+", compact):
        return "INTEGER"
    if re.fullmatch(r"\d+\.\d+", compact):
        return "DECIMAL"
    if compact.startswith("昨日"):
        return "YESTERDAY_COMPARISON"
    if compact == "趋势":
        return "TREND_CONTROL"
    return "OTHER"


async def main() -> None:
    manager = await async_playwright().start()
    browser = await manager.chromium.connect_over_cdp("http://127.0.0.1:9222")
    try:
        pages = [page for context in browser.contexts for page in context.pages]
        page = next(
            (candidate for candidate in pages if same_page_url(candidate.url, TARGET_URL)), None
        )
        if page is None:
            raise RuntimeError("exact home page is not open")
        results: list[dict[str, object]] = []
        for label in LABELS:
            label_locator = page.get_by_text(label, exact=True)
            label_count = await label_locator.count()
            children: list[dict[str, str]] = []
            if label_count == 1:
                card = label_locator.locator(
                    "xpath=ancestor::div[contains(@class,'manage-data-chart__panel__card')][1]"
                )
                leaves = card.locator("xpath=.//*[not(*) and normalize-space(.)!='']")
                for index in range(min(await leaves.count(), 20)):
                    leaf = leaves.nth(index)
                    text = (await leaf.text_content() or "").strip()
                    children.append(
                        {
                            "tag": await leaf.evaluate("element => element.tagName.toLowerCase()"),
                            "class_name": (await leaf.get_attribute("class") or "")[:200],
                            "text_kind": _classify(text, label),
                        }
                    )
            value_selector = (
                "xpath=//div[contains(concat(' ',normalize-space(@class),' '),"
                "' manage-data-chart__panel__card ')][.//span[normalize-space(.)="
                f"'{label}']]//span[contains(@class,'content_val')]"
            )
            value_locator = page.locator(value_selector)
            value_count = await value_locator.count()
            value_text = await value_locator.first.text_content() if value_count else ""
            results.append(
                {
                    "label": label,
                    "label_count": label_count,
                    "card_leaf_nodes": children,
                    "value_selector_count": value_count,
                    "value_kind": _classify(value_text or "", label),
                }
            )
        updated = page.locator("xpath=//span[contains(normalize-space(.),'实时数据更新时间:')]")
        updated_count = await updated.count()
        updated_text = await updated.first.text_content() if updated_count else ""
        match = re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", updated_text or "")
        realtime = page.get_by_text("实时", exact=True)
        print(
            json.dumps(
                {
                    "metrics": results,
                    "source_update_selector_count": updated_count,
                    "source_update_timestamp_present": match is not None,
                    "source_update_date": match.group(0)[:10] if match else None,
                    "realtime_label_count": await realtime.count(),
                },
                ensure_ascii=True,
                separators=(",", ":"),
            )
        )
    finally:
        await manager.stop()


if __name__ == "__main__":
    asyncio.run(main())
