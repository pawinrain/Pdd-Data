from __future__ import annotations

import asyncio
import json
from collections import Counter
from urllib.parse import urlsplit

from playwright.async_api import async_playwright


async def main() -> None:
    manager = await async_playwright().start()
    browser = await manager.chromium.connect_over_cdp("http://127.0.0.1:9222")
    try:
        rows: list[dict[str, object]] = []
        for context_index, context in enumerate(browser.contexts):
            for page_index, page in enumerate(context.pages):
                parsed = urlsplit(page.url)
                if parsed.hostname not in {"mms.pinduoduo.com", "yingxiao.pinduoduo.com"}:
                    continue
                rows.append(
                    {
                        "context": context_index,
                        "page": page_index,
                        "host": parsed.hostname,
                        "path": parsed.path.rstrip("/") or "/",
                        "title": (await page.title())[:80],
                    }
                )
        counts = Counter((str(row["host"]), str(row["path"])) for row in rows)
        print(
            json.dumps(
                {
                    "context_count": len(browser.contexts),
                    "matching_tab_count": len(rows),
                    "duplicate_targets": [
                        {"host": host, "path": path, "count": count}
                        for (host, path), count in sorted(counts.items())
                        if count > 1
                    ],
                    "tabs": rows,
                },
                ensure_ascii=True,
                separators=(",", ":"),
            )
        )
    finally:
        await browser.close()
        await manager.stop()


if __name__ == "__main__":
    asyncio.run(main())
