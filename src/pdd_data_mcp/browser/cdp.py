from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Browser, Error, Page, Playwright, async_playwright

from pdd_data_mcp.config import ConnectionSettings, PromotionAdapterSettings
from pdd_data_mcp.errors import CollectionRejected

LOGGER = logging.getLogger("pdd_data_mcp.browser")

# Navigation timeout for a tab the service opens itself when no target tab exists.
DEFAULT_PAGE_OPEN_TIMEOUT_MS = 15_000


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


def same_page_url(candidate: str, target: str, *, require_clean_url: bool = False) -> bool:
    """Match one configured page while deliberately ignoring query and fragment data.

    ``require_clean_url`` additionally refuses candidates that carry a query string or
    fragment. Collectors that must capture an *unfiltered* page pass it so a stale tab
    left on a filtered/keyword URL is never reused: such a tab would otherwise be matched
    here and then refused by the caller's own URL assertion, which fails every run until
    the tab is closed by hand.
    """
    actual = urlsplit(candidate)
    expected = urlsplit(target)
    if require_clean_url and (actual.query or actual.fragment):
        return False
    return (
        actual.scheme.casefold() == expected.scheme.casefold()
        and actual.hostname == expected.hostname
        and actual.port == expected.port
        and actual.path.rstrip("/") == expected.path.rstrip("/")
    )


async def _selector_present(page: Page, selector: str) -> bool:
    return bool(selector) and await page.locator(selector).count() > 0


async def _assert_page_ready(
    page: Page,
    *,
    login_selector: str,
    captcha_selector: str,
    error_selector: str,
    page_code: str,
) -> None:
    if await _selector_present(page, captcha_selector):
        raise CollectionRejected("AUTH_REQUIRED", "CAPTCHA_PRESENT")
    if await _selector_present(page, login_selector):
        raise CollectionRejected("AUTH_REQUIRED", f"{page_code}_LOGIN_REQUIRED")
    if await _selector_present(page, error_selector):
        raise CollectionRejected("PLATFORM_ERROR", f"{page_code}_ERROR")


async def open_missing_page(
    browser: Browser,
    target_page_url: str,
    *,
    login_selector: str = "",
    captcha_selector: str = "",
    error_selector: str = "",
    page_code: str = "CORE_DATA_PAGE",
    open_timeout_ms: int = DEFAULT_PAGE_OPEN_TIMEOUT_MS,
) -> Page:
    """Open a new tab in the already-connected user Chrome and navigate to a configured URL.

    The target URL always comes from reviewed local configuration (host allowlisted at
    config load time), never from an MCP parameter. This never launches or closes a
    browser, never creates a new browser context (so the existing login session is used),
    and leaves the opened tab in place for later runs.
    """

    contexts = [context for context in browser.contexts if context.pages] or list(browser.contexts)
    if not contexts:
        raise CollectionRejected("CDP_UNAVAILABLE", "NO_BROWSER_CONTEXT")
    context = contexts[0]
    page = await context.new_page()
    try:
        await page.goto(target_page_url, wait_until="domcontentloaded", timeout=open_timeout_ms)
    except Error as exc:
        raise CollectionRejected("PLATFORM_ERROR", f"{page_code}_OPEN_FAILED") from exc
    LOGGER.info("target tab missing; opened new tab page_code=%s", page_code)
    await _assert_page_ready(
        page,
        login_selector=login_selector,
        captcha_selector=captcha_selector,
        error_selector=error_selector,
        page_code=page_code,
    )
    return page


async def select_target_page(
    browser: Browser,
    connection: ConnectionSettings,
    adapter: PromotionAdapterSettings,
    *,
    auto_open: bool = False,
    open_timeout_ms: int = DEFAULT_PAGE_OPEN_TIMEOUT_MS,
) -> Page:
    pages: list[Page] = [
        page for context in browser.contexts for page in context.pages if not page.is_closed()
    ]
    matching = [page for page in pages if same_page_url(page.url, connection.target_page_url)]
    if not matching:
        if not auto_open:
            raise CollectionRejected("TARGET_PAGE_NOT_FOUND", "PROMOTION_OVERVIEW_PAGE_NOT_FOUND")
        return await open_missing_page(
            browser,
            connection.target_page_url,
            login_selector=adapter.login_selector,
            captcha_selector=adapter.captcha_selector,
            error_selector=adapter.error_selector,
            page_code="PROMOTION_OVERVIEW_PAGE",
            open_timeout_ms=open_timeout_ms,
        )
    if len(matching) != 1:
        raise CollectionRejected("TARGET_PAGE_NOT_FOUND", "AMBIGUOUS_PROMOTION_OVERVIEW_PAGE")
    page = matching[0]
    await _assert_page_ready(
        page,
        login_selector=adapter.login_selector,
        captcha_selector=adapter.captcha_selector,
        error_selector=adapter.error_selector,
        page_code="PROMOTION_OVERVIEW_PAGE",
    )
    return page


async def select_page_by_url(
    browser: Browser,
    target_page_url: str,
    *,
    login_selector: str = "",
    captcha_selector: str = "",
    error_selector: str = "",
    page_code: str = "CORE_DATA_PAGE",
    auto_open: bool = False,
    open_timeout_ms: int = DEFAULT_PAGE_OPEN_TIMEOUT_MS,
    require_clean_url: bool = False,
) -> Page:
    pages: list[Page] = [
        page for context in browser.contexts for page in context.pages if not page.is_closed()
    ]
    matching = [
        page
        for page in pages
        if same_page_url(page.url, target_page_url, require_clean_url=require_clean_url)
    ]
    if not matching:
        if not auto_open:
            raise CollectionRejected("TARGET_PAGE_NOT_FOUND", f"{page_code}_NOT_FOUND")
        return await open_missing_page(
            browser,
            target_page_url,
            login_selector=login_selector,
            captcha_selector=captcha_selector,
            error_selector=error_selector,
            page_code=page_code,
            open_timeout_ms=open_timeout_ms,
        )
    if len(matching) != 1:
        raise CollectionRejected("TARGET_PAGE_NOT_FOUND", f"AMBIGUOUS_{page_code}")
    page = matching[0]
    await _assert_page_ready(
        page,
        login_selector=login_selector,
        captcha_selector=captcha_selector,
        error_selector=error_selector,
        page_code=page_code,
    )
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
