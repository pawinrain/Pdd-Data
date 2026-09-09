from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from urllib.parse import urlsplit

from playwright.async_api import Response, async_playwright

from pdd_data_mcp.browser.cdp import same_page_url

CDP_ENDPOINT = "http://127.0.0.1:9222"
IDENTITY_PAGE = "https://mms.pinduoduo.com/mallcenter/info/basic"
REQUIRED_PAGES = {
    "https://mms.pinduoduo.com/home",
    "https://mms.pinduoduo.com/goods/goods_list",
    IDENTITY_PAGE,
}
IDENTITY_PATH = "/earth/api/merchant/queryMerchantInfoByMallId"


def _matches(response: Response) -> bool:
    parsed = urlsplit(response.url)
    return (
        parsed.hostname == "mms.pinduoduo.com"
        and parsed.path == IDENTITY_PATH
        and response.request.method == "POST"
        and response.status == 200
        and response.headers.get("content-type", "").partition(";")[0].casefold()
        == "application/json"
    )


async def verify(expected_sha256: str) -> None:
    manager = await async_playwright().start()
    browser = await manager.chromium.connect_over_cdp(CDP_ENDPOINT)
    try:
        pages = [page for context in browser.contexts for page in context.pages]
        required_present = all(
            any(same_page_url(page.url, required) for page in pages) for required in REQUIRED_PAGES
        )
        identity_page = next(
            (page for page in pages if same_page_url(page.url, IDENTITY_PAGE)), None
        )
        if identity_page is None:
            raise RuntimeError("identity page is not open")
        async with identity_page.expect_response(_matches, timeout=20_000) as response_info:
            await identity_page.reload(wait_until="domcontentloaded", timeout=20_000)
        response = await response_info.value
        payload = json.loads(await response.body())
        raw_id = payload.get("result", {}).get("mallId")
        if payload.get("success") is not True or not isinstance(raw_id, str | int):
            raise RuntimeError("identity response shape is not verified")
        identity = str(raw_id)
        if not identity.isdigit():
            raise RuntimeError("identity value is not a decimal identifier")
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        page_text = await identity_page.locator("html").text_content()
        result = {
            "status": "PASS",
            "required_pages_present": required_present,
            "identity_response_verified": True,
            "page_state_exact_match": isinstance(page_text, str) and identity in page_text,
            "matches_expected_identity": digest == expected_sha256,
            "identity_value_printed": False,
        }
        print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    finally:
        await manager.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Read-only current-store identity gate.")
    parser.add_argument("--expected-sha256", required=True)
    arguments = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{64}", arguments.expected_sha256):
        parser.error("--expected-sha256 must be a lowercase SHA-256 digest")
    asyncio.run(verify(arguments.expected_sha256))
