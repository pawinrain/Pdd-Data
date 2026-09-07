from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from urllib.parse import urlsplit

from playwright.async_api import Response, async_playwright


async def inspect(target_url: str, observe_seconds: float) -> None:
    manager = await async_playwright().start()
    browser = await manager.chromium.connect_over_cdp("http://127.0.0.1:9222")
    entries: list[tuple[str, str, str, int, str]] = []
    tasks: set[asyncio.Task[None]] = set()

    async def record(response: Response) -> None:
        parsed = urlsplit(response.url)
        content_type = response.headers.get("content-type", "").partition(";")[0].casefold()
        if parsed.hostname and parsed.hostname.endswith("pinduoduo.com"):
            entries.append(
                (
                    response.request.method,
                    parsed.hostname,
                    parsed.path,
                    response.status,
                    content_type,
                )
            )

    def schedule(response: Response) -> None:
        if len(entries) + len(tasks) >= 300:
            return
        task = asyncio.create_task(record(response))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

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
        page.on("response", schedule)
        await page.reload(wait_until="domcontentloaded", timeout=20_000)
        await asyncio.sleep(observe_seconds)
        if tasks:
            await asyncio.gather(*tasks)
        page.remove_listener("response", schedule)
        unique = sorted(Counter(entries).items())
        print(
            json.dumps(
                {
                    "target_path": urlsplit(target_url).path,
                    "observed_count": sum(count for _, count in unique),
                    "unique": [
                        {
                            "method": method,
                            "host": host,
                            "path": path,
                            "status": status,
                            "content_type": content_type,
                            "observations": count,
                        }
                        for (method, host, path, status, content_type), count in unique
                    ],
                },
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
    parser.add_argument("--observe-seconds", type=float, default=8.0)
    args = parser.parse_args()
    asyncio.run(inspect(args.target_url, args.observe_seconds))


if __name__ == "__main__":
    main()
