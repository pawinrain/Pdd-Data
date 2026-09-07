from __future__ import annotations

import argparse
import asyncio
import json
import re
from contextlib import suppress
from urllib.parse import urlsplit

from playwright.async_api import Response, async_playwright

from pdd_data_mcp.browser.promotion import sanitized_json_shape

_SAFE_DISCRIMINATOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,80}$")


async def probe(target_url: str, paths: set[str], observe_seconds: float) -> None:
    manager = await async_playwright().start()
    browser = await manager.chromium.connect_over_cdp("http://127.0.0.1:9222")
    results: dict[str, list[dict[str, object]]] = {}
    tasks: set[asyncio.Task[None]] = set()

    async def record(response: Response) -> None:
        parsed = urlsplit(response.url)
        content_type = response.headers.get("content-type", "").partition(";")[0].casefold()
        if (
            parsed.hostname != "mms.pinduoduo.com"
            or parsed.path not in paths
            or response.request.method not in {"GET", "POST"}
            or response.status != 200
            or content_type != "application/json"
        ):
            return
        try:
            body = await response.body()
            payload = json.loads(body)
        except Exception as exc:  # pragma: no cover - diagnostic boundary
            results.setdefault(parsed.path, []).append({"error_type": type(exc).__name__})
            return
        request_payload: object | None = None
        with suppress(Exception):  # pragma: no cover - diagnostic boundary
            request_payload = response.request.post_data_json
        item = {
            "method": response.request.method,
            "request_discriminator": {
                key: value
                for key in ("type", "operation", "method")
                if isinstance(request_payload, dict)
                and isinstance(value := request_payload.get(key), str)
                and _SAFE_DISCRIMINATOR.fullmatch(value)
            },
            "request_shape": sanitized_json_shape(request_payload, max_depth=8, max_entries=200),
            "response_shape": sanitized_json_shape(payload, max_depth=10, max_entries=600),
        }
        bucket = results.setdefault(parsed.path, [])
        if item not in bucket:
            bucket.append(item)

    def schedule(response: Response) -> None:
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
        print(
            json.dumps(
                {
                    "target_path": urlsplit(target_url).path,
                    "probed_paths": sorted(paths),
                    "results": results,
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
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--observe-seconds", type=float, default=8.0)
    args = parser.parse_args()
    asyncio.run(probe(args.target_url, set(args.paths), args.observe_seconds))


if __name__ == "__main__":
    main()
