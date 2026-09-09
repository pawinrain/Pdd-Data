from __future__ import annotations

import asyncio
import json
import re
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from playwright.async_api import Response, async_playwright

from pdd_data_mcp.browser.cdp import same_page_url

TARGET_URL = "https://mms.pinduoduo.com/home"
RESPONSE_PATH = "/merchant-web-service/leon"
METRICS = {
    "gmv": ("成交金额", "CNY"),
    "order_count": ("成交订单数", "COUNT"),
    "visitor_count": ("商品访客数", "COUNT"),
    "page_view_count": ("商品浏览量", "COUNT"),
    "review_count": ("商品评价数", "COUNT"),
}


def _normalize(value: object, unit: str) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return parsed * (100 if unit == "CNY" else 1)


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


async def main() -> None:
    manager = await async_playwright().start()
    browser = await manager.chromium.connect_over_cdp("http://127.0.0.1:9222")
    observations: list[dict[str, object]] = []
    tasks: set[asyncio.Task[None]] = set()

    async def consume(response: Response) -> None:
        if not _matches(response):
            return
        request_payload = response.request.post_data_json
        if not isinstance(request_payload, dict) or not isinstance(
            request_payload.get("type"), str
        ):
            return
        payload = json.loads(await response.body())
        result = payload.get("result")
        if payload.get("success") is not True or not isinstance(result, dict):
            return
        observations.append(
            {
                "type": request_payload["type"],
                "value": result.get("value"),
                "time": result.get("time"),
            }
        )

    def schedule(response: Response) -> None:
        task = asyncio.create_task(consume(response))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    try:
        pages = [page for context in browser.contexts for page in context.pages]
        page = next(
            (candidate for candidate in pages if same_page_url(candidate.url, TARGET_URL)), None
        )
        if page is None:
            raise RuntimeError("exact home page is not open")
        page.on("response", schedule)
        await page.reload(wait_until="domcontentloaded", timeout=20_000)
        await asyncio.sleep(8)
        if tasks:
            await asyncio.gather(*tasks)
        page.remove_listener("response", schedule)

        dom_values: dict[str, Decimal | None] = {}
        selector_counts: dict[str, int] = {}
        for name, (label, unit) in METRICS.items():
            selector = (
                "xpath=//div[contains(concat(' ',normalize-space(@class),' '),"
                "' manage-data-chart__panel__card ')][.//span[normalize-space(.)="
                f"'{label}']]//span[contains(@class,'content_val')]"
            )
            locator = page.locator(selector)
            selector_counts[name] = await locator.count()
            text = await locator.first.text_content() if selector_counts[name] else None
            dom_values[name] = _normalize(text, unit)

        mappings: dict[str, list[str]] = {}
        value_shapes: dict[str, dict[str, object]] = {}
        dates: set[str] = set()
        for observation in observations:
            raw_time = observation["time"]
            if isinstance(raw_time, int | float) and not isinstance(raw_time, bool):
                seconds = raw_time / 1000 if raw_time > 10_000_000_000 else raw_time
                with suppress(OSError, OverflowError, ValueError):
                    dates.add(datetime.fromtimestamp(seconds, tz=UTC).date().isoformat())
            candidates = [
                name
                for name, (_, unit) in METRICS.items()
                if dom_values[name] is not None
                and _normalize(observation["value"], unit) == dom_values[name]
            ]
            mappings[str(observation["type"])] = candidates
            raw_value = observation["value"]
            text_value = raw_value if isinstance(raw_value, str) else ""
            value_shapes[str(observation["type"])] = {
                "type": type(raw_value).__name__,
                "length": len(text_value),
                "is_plain_decimal": bool(re.fullmatch(r"\d+(?:\.\d+)?", text_value)),
                "is_missing_marker": text_value.strip() in {"", "--"},
            }
        print(
            json.dumps(
                {
                    "status": "PASS",
                    "response_count": len(observations),
                    "unique_type_count": len(mappings),
                    "selector_counts": selector_counts,
                    "response_dates_utc": sorted(dates),
                    "candidate_metric_mapping": mappings,
                    "response_value_shapes": value_shapes,
                    "uniquely_mapped_metrics": sorted(
                        {values[0] for values in mappings.values() if len(values) == 1}
                    ),
                    "business_values_output": False,
                },
                ensure_ascii=True,
                separators=(",", ":"),
            )
        )
    finally:
        await manager.stop()


if __name__ == "__main__":
    asyncio.run(main())
