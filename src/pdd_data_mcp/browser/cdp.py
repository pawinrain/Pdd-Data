from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Browser, Error, Page, Playwright, async_playwright

from pdd_data_mcp.config import ConnectionSettings, PromotionAdapterSettings
from pdd_data_mcp.errors import CollectionRejected


@dataclass
class CdpSession:
    playwright: Playwright
    browser: Browser
    disconnected: bool = False

    async def disconnect(self) -> None:
        """Disconnect Playwright transport without sending Browser.close to user Chrome."""
        if not self.disconnected:
            self.disconnected = True
            await self.playwright.stop()


class CdpConnector:
    async def connect(self, connection: ConnectionSettings, timeout_ms: int) -> CdpSession:
        if connection.cdp_endpoint is None:
            raise CollectionRejected("CDP_UNAVAILABLE", "CDP_ENDPOINT_NOT_CONFIGURED")
        manager = await async_playwright().start()
        try:
            browser = await manager.chromium.connect_over_cdp(
                connection.cdp_endpoint,
                timeout=timeout_ms,
                is_local=True,
                no_defaults=True,
            )
        except Error as exc:
            await manager.stop()
            raise CollectionRejected("CDP_UNAVAILABLE", "CDP_CONNECTION_FAILED") from exc
        return CdpSession(playwright=manager, browser=browser)


def same_page_url(candidate: str, target: str) -> bool:
    """Match one configured page while deliberately ignoring query and fragment data."""
    actual = urlsplit(candidate)
    expected = urlsplit(target)
    return (
        actual.scheme.casefold() == expected.scheme.casefold()
        and actual.hostname == expected.hostname
        and actual.port == expected.port
        and actual.path.rstrip("/") == expected.path.rstrip("/")
    )


async def _selector_present(page: Page, selector: str) -> bool:
    return bool(selector) and await page.locator(selector).count() > 0


async def select_target_page(
    browser: Browser,
    connection: ConnectionSettings,
    adapter: PromotionAdapterSettings,
) -> Page:
    pages: list[Page] = [
        page for context in browser.contexts for page in context.pages if not page.is_closed()
    ]
    matching = [page for page in pages if same_page_url(page.url, connection.target_page_url)]
    if not matching:
        raise CollectionRejected("TARGET_PAGE_NOT_FOUND", "PROMOTION_OVERVIEW_PAGE_NOT_FOUND")
    if len(matching) != 1:
        raise CollectionRejected("TARGET_PAGE_NOT_FOUND", "AMBIGUOUS_PROMOTION_OVERVIEW_PAGE")
    page = matching[0]
    if await _selector_present(page, adapter.captcha_selector):
        raise CollectionRejected("AUTH_REQUIRED", "CAPTCHA_PRESENT")
    if await _selector_present(page, adapter.login_selector):
        raise CollectionRejected("AUTH_REQUIRED", "PROMOTION_LOGIN_REQUIRED")
    if await _selector_present(page, adapter.error_selector):
        raise CollectionRejected("PLATFORM_ERROR", "PROMOTION_PAGE_ERROR")
    return page


async def select_page_by_url(
    browser: Browser,
    target_page_url: str,
    *,
    login_selector: str = "",
    captcha_selector: str = "",
    error_selector: str = "",
    page_code: str = "CORE_DATA_PAGE",
) -> Page:
    pages: list[Page] = [
        page for context in browser.contexts for page in context.pages if not page.is_closed()
    ]
    matching = [page for page in pages if same_page_url(page.url, target_page_url)]
    if not matching:
        raise CollectionRejected("TARGET_PAGE_NOT_FOUND", f"{page_code}_NOT_FOUND")
    if len(matching) != 1:
        raise CollectionRejected("TARGET_PAGE_NOT_FOUND", f"AMBIGUOUS_{page_code}")
    page = matching[0]
    if await _selector_present(page, captcha_selector):
        raise CollectionRejected("AUTH_REQUIRED", "CAPTCHA_PRESENT")
    if await _selector_present(page, login_selector):
        raise CollectionRejected("AUTH_REQUIRED", f"{page_code}_LOGIN_REQUIRED")
    if await _selector_present(page, error_selector):
        raise CollectionRejected("PLATFORM_ERROR", f"{page_code}_ERROR")
    return page


async def read_locator_value(
    page: Page,
    selector: str,
    attribute: str = "",
    *,
    wait_timeout_ms: int = 0,
    failure_status: str = "IDENTITY_UNVERIFIED",
    failure_code: str = "DOM_EVIDENCE_MISSING",
) -> str:
    locator = page.locator(selector)
    wait_for = getattr(locator, "wait_for", None)
    if wait_timeout_ms and wait_for is not None:
        try:
            await wait_for(state="attached", timeout=wait_timeout_ms)
        except Error as exc:
            raise CollectionRejected(failure_status, failure_code) from exc
    count = await locator.count()
    if count != 1:
        raise CollectionRejected(failure_status, f"{failure_code}_NOT_UNIQUE")
    value: Any
    if attribute:
        value = await locator.get_attribute(attribute)
    else:
        value = await locator.text_content()
    if not isinstance(value, str) or not value.strip():
        raise CollectionRejected(failure_status, failure_code)
    return value.strip()
