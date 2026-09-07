from __future__ import annotations

import argparse
import asyncio
import json

from playwright.async_api import async_playwright


async def probe(target_url: str, pattern: str) -> None:
    manager = await async_playwright().start()
    browser = await manager.chromium.connect_over_cdp("http://127.0.0.1:9222")
    try:
        pages = [page for context in browser.contexts for page in context.pages]
        page = next(
            (
                candidate
                for candidate in pages
                if candidate.url.rstrip("/") == target_url.rstrip("/")
            ),
            None,
        )
        if page is None:
            raise RuntimeError("exact target page is not open")
        rows = await page.evaluate(
            """pattern => {
                const matcher = new RegExp(pattern, 'i');
                const rows = [];
                for (const element of document.querySelectorAll('body *')) {
                    const text = (element.innerText || '').trim();
                    if (!text || text.length > 100 || !matcher.test(text)) continue;
                    const childHasSame = Array.from(element.children).some(child =>
                        ((child.innerText || '').trim() === text));
                    if (childHasSame) continue;
                    rows.push({
                        tag: element.tagName.toLowerCase(),
                        id: element.id || '',
                        class_name: String(element.className || '').slice(0, 240),
                        data_testid: element.getAttribute('data-testid') || '',
                        role: element.getAttribute('role') || '',
                        aria_label: (element.getAttribute('aria-label') || '').replace(/\\d/g, '#'),
                        redacted_text: text.replace(/\\d/g, '#'),
                    });
                    if (rows.length >= 80) break;
                }
                return rows;
            }""",
            pattern,
        )
        print(
            json.dumps(
                {"candidate_count": len(rows), "candidates": rows},
                ensure_ascii=True,
                separators=(",", ":"),
            )
        )
    finally:
        await browser.close()
        await manager.stop()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("target_url")
    parser.add_argument("pattern")
    args = parser.parse_args()
    asyncio.run(probe(args.target_url, args.pattern))


if __name__ == "__main__":
    main()
