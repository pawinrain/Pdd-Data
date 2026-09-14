from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from playwright.async_api import Browser, Page
from playwright.async_api import Error as PlaywrightError

from pdd_data_mcp.browser.aftersale import AftersaleCdpCollector
from pdd_data_mcp.browser.cdp import (
    DEFAULT_PAGE_OPEN_TIMEOUT_MS,
    open_missing_page,
    select_page_by_url,
    select_target_page,
)
from pdd_data_mcp.config import (
    AftersaleAdapterSettings,
    CollectionSettings,
    ConnectionSettings,
    PromotionAdapterSettings,
)
from pdd_data_mcp.errors import CollectionRejected

_TARGET = "https://yingxiao.pinduoduo.com/mains/promotionOverview"
_MMS_HOME = "https://mms.pinduoduo.com/home"
_LIST_URL = "https://mms.pinduoduo.com/aftersales/aftersale_list?searchType=7"


class _Navigation:
    async def __aenter__(self) -> _Navigation:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


class FakeLocator:
    def __init__(
        self,
        page: FakePage,
        *,
        count: int = 0,
        click_url: str = "",
        fail_click: bool = False,
    ) -> None:
        self._page = page
        self._count = count
        self._click_url = click_url
        self._fail_click = fail_click

    async def count(self) -> int:
        return self._count

    async def click(self, force: bool = False, timeout: int | None = None) -> None:
        if self._fail_click:
            raise PlaywrightError("element not found")
        if self._click_url:
            self._page.url = self._click_url

    @property
    def first(self) -> FakeLocator:
        return self


class FakePage:
    def __init__(
        self,
        url: str = "",
        *,
        closed: bool = False,
        selector_counts: dict[str, int] | None = None,
        goto_fails: bool = False,
        sidebar: bool = False,
    ) -> None:
        self.url = url
        self._closed = closed
        self._selector_counts = selector_counts or {}
        self._goto_fails = goto_fails
        self._sidebar = sidebar
        self.goto_calls: list[tuple[str, str | None, int | None]] = []

    def is_closed(self) -> bool:
        return self._closed

    def locator(self, selector: str) -> FakeLocator:
        if self._sidebar and "aftersale_list" in selector:
            return FakeLocator(self, count=1, click_url=_LIST_URL)
        return FakeLocator(self, count=self._selector_counts.get(selector, 0))

    def get_by_text(self, label: str, exact: bool = False) -> FakeLocator:
        return FakeLocator(self, count=0)

    async def goto(
        self, url: str, wait_until: str | None = None, timeout: int | None = None
    ) -> None:
        self.goto_calls.append((url, wait_until, timeout))
        if self._goto_fails:
            raise PlaywrightError("navigation failed")
        self.url = url

    async def bring_to_front(self) -> None:
        return None

    def expect_navigation(self, timeout: int | None = None) -> _Navigation:
        return _Navigation()

    async def wait_for_timeout(self, milliseconds: int) -> None:
        return None


class FakeContext:
    def __init__(
        self,
        pages: list[FakePage] | None = None,
        *,
        new_page_sidebar: bool = False,
        new_page_fn: Callable[[], FakePage] | None = None,
    ) -> None:
        self.pages: list[FakePage] = pages or []
        self.new_page_calls = 0
        self._new_page_sidebar = new_page_sidebar
        self._new_page_fn = new_page_fn

    async def new_page(self) -> FakePage:
        self.new_page_calls += 1
        if self._new_page_fn is not None:
            page = self._new_page_fn()
        else:
            page = FakePage("about:blank", sidebar=self._new_page_sidebar)
        self.pages.append(page)
        return page


class FakeBrowser:
    def __init__(self, contexts: list[FakeContext]) -> None:
        self.contexts = contexts


def _browser(contexts: list[FakeContext]) -> Browser:
    return cast(Browser, FakeBrowser(contexts))


def _fake(page: Page) -> FakePage:
    return cast(FakePage, page)


def _connection(**overrides: Any) -> ConnectionSettings:
    return ConnectionSettings(connection_id="conn_1", store_id="st_1", **overrides)


def test_existing_unique_tab_is_used_without_opening() -> None:
    existing = FakePage(_TARGET)
    context = FakeContext([existing])

    page = asyncio.run(
        select_page_by_url(
            _browser([context]), _TARGET, page_code="PROMOTION_ACCOUNT_PAGE", auto_open=True
        )
    )

    assert _fake(page) is existing
    assert context.new_page_calls == 0


def test_dirty_tab_is_skipped_when_clean_url_required() -> None:
    # 默认行为（向后兼容）：忽略 query，带参数的标签页照旧被复用。
    legacy = FakeContext([FakePage(f"{_TARGET}?keyword=hidden")])
    reused = asyncio.run(
        select_page_by_url(
            _browser([legacy]), _TARGET, page_code="PROMOTION_ACCOUNT_PAGE", auto_open=True
        )
    )
    assert _fake(reused).url == f"{_TARGET}?keyword=hidden"
    assert legacy.new_page_calls == 0

    # require_clean_url=True：脏标签页不再被复用，改为打开一个干净页面。
    strict = FakeContext([FakePage(f"{_TARGET}?keyword=hidden")])
    opened = asyncio.run(
        select_page_by_url(
            _browser([strict]),
            _TARGET,
            page_code="PROMOTION_ACCOUNT_PAGE",
            auto_open=True,
            require_clean_url=True,
        )
    )
    assert strict.new_page_calls == 1
    assert _fake(opened).url == _TARGET


def test_missing_tab_still_fails_closed_when_switch_off() -> None:
    context = FakeContext([FakePage("https://yingxiao.pinduoduo.com/other")])

    with pytest.raises(CollectionRejected) as exc:
        asyncio.run(
            select_page_by_url(_browser([context]), _TARGET, page_code="PROMOTION_ACCOUNT_PAGE")
        )
    assert exc.value.status == "TARGET_PAGE_NOT_FOUND"
    assert exc.value.error_code == "PROMOTION_ACCOUNT_PAGE_NOT_FOUND"
    assert context.new_page_calls == 0


def test_missing_tab_is_opened_when_switch_on() -> None:
    context = FakeContext([FakePage("https://mms.pinduoduo.com/home")])

    page = asyncio.run(
        select_page_by_url(
            _browser([context]),
            _TARGET,
            page_code="PROMOTION_ACCOUNT_PAGE",
            auto_open=True,
            open_timeout_ms=12_345,
        )
    )

    assert context.new_page_calls == 1
    assert _fake(page).goto_calls == [(_TARGET, "domcontentloaded", 12_345)]
    assert page.url == _TARGET


def test_open_uses_empty_context_when_no_pages_anywhere() -> None:
    context = FakeContext([])

    page = asyncio.run(open_missing_page(_browser([context]), _TARGET, page_code="X_PAGE"))

    assert context.new_page_calls == 1
    assert _fake(page).goto_calls[0][0] == _TARGET


def test_open_without_any_context_is_rejected() -> None:
    with pytest.raises(CollectionRejected) as exc:
        asyncio.run(open_missing_page(_browser([]), _TARGET, page_code="X_PAGE"))
    assert exc.value.status == "CDP_UNAVAILABLE"
    assert exc.value.error_code == "NO_BROWSER_CONTEXT"


def test_opened_tab_showing_login_is_rejected() -> None:
    context = FakeContext(
        [FakePage("https://mms.pinduoduo.com/home")],
        new_page_fn=lambda: FakePage(_TARGET, selector_counts={"input[type='password']": 1}),
    )

    with pytest.raises(CollectionRejected) as exc:
        asyncio.run(
            select_page_by_url(
                _browser([context]),
                _TARGET,
                login_selector="input[type='password']",
                page_code="PROMOTION_ACCOUNT_PAGE",
                auto_open=True,
            )
        )
    assert exc.value.status == "AUTH_REQUIRED"
    assert exc.value.error_code == "PROMOTION_ACCOUNT_PAGE_LOGIN_REQUIRED"


def test_opened_tab_navigation_failure_is_rejected() -> None:
    context = FakeContext(
        [FakePage("https://mms.pinduoduo.com/home")],
        new_page_fn=lambda: FakePage("about:blank", goto_fails=True),
    )

    with pytest.raises(CollectionRejected) as exc:
        asyncio.run(
            select_page_by_url(
                _browser([context]), _TARGET, page_code="PROMOTION_ACCOUNT_PAGE", auto_open=True
            )
        )
    assert exc.value.status == "PLATFORM_ERROR"
    assert exc.value.error_code == "PROMOTION_ACCOUNT_PAGE_OPEN_FAILED"


def test_ambiguous_tabs_still_fail_closed_even_with_switch_on() -> None:
    context = FakeContext([FakePage(_TARGET), FakePage(_TARGET + "?x=1")])

    with pytest.raises(CollectionRejected) as exc:
        asyncio.run(select_page_by_url(_browser([context]), _TARGET, auto_open=True))
    assert exc.value.status == "TARGET_PAGE_NOT_FOUND"
    assert exc.value.error_code.startswith("AMBIGUOUS_")


def test_select_target_page_opens_configured_connection_url() -> None:
    context = FakeContext([FakePage("https://mms.pinduoduo.com/home")])
    connection = _connection(target_page_url=_TARGET)
    adapter = PromotionAdapterSettings()

    page = asyncio.run(select_target_page(_browser([context]), connection, adapter, auto_open=True))

    assert _fake(page).goto_calls == [(_TARGET, "domcontentloaded", DEFAULT_PAGE_OPEN_TIMEOUT_MS)]


def test_collection_settings_defaults_keep_fail_closed() -> None:
    settings = CollectionSettings()
    assert settings.auto_open_missing_pages is False
    assert settings.page_open_timeout_ms == 15_000


def _aftersale_collector(tmp_path: Path, *, auto_open: bool) -> AftersaleCdpCollector:
    connection = _connection(
        target_page_url=_LIST_URL,
        aftersale_adapter=AftersaleAdapterSettings(),
    )
    collection = CollectionSettings(auto_open_missing_pages=auto_open)
    return AftersaleCdpCollector(
        connection=connection,
        collection=collection,
        runtime_root=tmp_path,
    )


def test_aftersale_without_mms_tab_fails_closed_without_switch(tmp_path: Path) -> None:
    collector = _aftersale_collector(tmp_path, auto_open=False)

    with pytest.raises(CollectionRejected) as exc:
        asyncio.run(collector._open_aftersale_list_page(_browser([FakeContext([])])))
    assert exc.value.error_code == "AFTERSALE_MMS_TAB_MISSING"


def test_aftersale_opens_home_and_enters_via_sidebar(tmp_path: Path) -> None:
    collector = _aftersale_collector(tmp_path, auto_open=True)
    # Empty browser: the service-opened tab gets a working in-app sidebar entry.
    context = FakeContext([], new_page_sidebar=True)

    page = asyncio.run(collector._open_aftersale_list_page(_browser([context])))

    assert context.new_page_calls == 1
    assert _fake(page).goto_calls == [(_MMS_HOME, "domcontentloaded", 15_000)]
    assert "/aftersales/aftersale_list" in page.url
