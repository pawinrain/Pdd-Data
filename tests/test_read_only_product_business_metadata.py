from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest

from examples import read_only_product_business_metadata as metadata_module
from examples.read_only_product_business_metadata import (
    MAX_METADATA_OBSERVATIONS,
    REQUIRED_SAME_CONTEXT_PAGES,
    TARGET_PAGE_URL,
    MetadataRuntime,
    MetadataStopped,
    SafetyCounts,
    _assert_safety_gate,
    _capture_metadata,
    _is_strict_pdd_https_url,
    _matches_exact_page,
    _response_metadata,
    _select_target_page,
    metadata_inventory,
)


class FakeLocator:
    def __init__(self, count: int) -> None:
        self._count = count

    async def count(self) -> int:
        return self._count


class FakeRequest:
    def __init__(self, method: str) -> None:
        self.method = method


class FakeResponse:
    def __init__(
        self,
        url: str,
        *,
        method: str = "POST",
        status: int = 200,
        content_type: str = "application/json; charset=utf-8",
    ) -> None:
        self.url = url
        self.request = FakeRequest(method)
        self.status = status
        self.headers = {
            "content-type": content_type,
            "authorization": "must-not-output",
        }

    async def body(self) -> bytes:
        raise AssertionError("response body must never be read")


class FakePage:
    def __init__(
        self,
        url: str,
        *,
        closed: bool = False,
        selector_counts: dict[str, int] | None = None,
        responses: list[FakeResponse] | None = None,
    ) -> None:
        self.url = url
        self._closed = closed
        self.selector_counts = selector_counts or {}
        self.responses = responses or []
        self.listeners: list[Any] = []
        self.reload_calls = 0
        self.context: FakeContext | None = None

    def is_closed(self) -> bool:
        return self._closed

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self.selector_counts.get(selector, 0))

    def on(self, event: str, callback: Any) -> None:
        assert event == "response"
        self.listeners.append(callback)

    def remove_listener(self, event: str, callback: Any) -> None:
        assert event == "response"
        self.listeners.remove(callback)

    async def reload(self, **kwargs: object) -> None:
        assert kwargs == {"wait_until": "domcontentloaded", "timeout": 9000}
        self.reload_calls += 1
        for response in self.responses:
            for callback in list(self.listeners):
                callback(response)


class FakeContext:
    def __init__(self, pages: list[FakePage]) -> None:
        self.pages = pages
        for page in pages:
            page.context = self


class FakeBrowser:
    def __init__(self, contexts: list[FakeContext]) -> None:
        self.contexts = contexts


class FakeChromium:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        self.calls: list[dict[str, object]] = []

    async def connect_over_cdp(self, endpoint: str, **kwargs: object) -> FakeBrowser:
        self.calls.append({"endpoint": endpoint, **kwargs})
        return self.browser


class FakeManager:
    def __init__(self, browser: FakeBrowser) -> None:
        self.chromium = FakeChromium(browser)
        self.stop_calls = 0

    async def stop(self) -> None:
        self.stop_calls += 1


class FakePlaywrightStarter:
    def __init__(self, manager: FakeManager) -> None:
        self.manager = manager

    async def start(self) -> FakeManager:
        return self.manager


def browser_with_required_pages(target: FakePage) -> FakeBrowser:
    required = [FakePage(url) for url in REQUIRED_SAME_CONTEXT_PAGES]
    return FakeBrowser([FakeContext([target, *required])])


def test_page_matching_is_exact_but_drops_query_and_fragment() -> None:
    assert _matches_exact_page(f"{TARGET_PAGE_URL}?secret=value#fragment", TARGET_PAGE_URL)
    assert not _matches_exact_page("http://mms.pinduoduo.com/sycm/goods_effect", TARGET_PAGE_URL)
    assert not _matches_exact_page(
        "https://mms.pinduoduo.com.evil.test/sycm/goods_effect", TARGET_PAGE_URL
    )
    assert not _matches_exact_page(
        "https://user@mms.pinduoduo.com/sycm/goods_effect", TARGET_PAGE_URL
    )


def test_strict_pdd_boundary_and_response_metadata_are_sanitized() -> None:
    response = FakeResponse(
        "https://api.mms.pinduoduo.com/data/product/123456?token=secret#fragment"
    )

    assert _is_strict_pdd_https_url(response.url) == (
        "api.mms.pinduoduo.com",
        "/data/product/{id}",
    )
    assert _response_metadata(cast(Any, response)) == (
        "api.mms.pinduoduo.com",
        "/data/product/{id}",
        "POST",
        200,
        "application/json",
    )
    assert _is_strict_pdd_https_url("https://notpinduoduo.com/private") is None
    assert _is_strict_pdd_https_url("https://pinduoduo.com.evil.test/private") is None


def test_target_must_be_unique_with_required_pages_in_same_context() -> None:
    target = FakePage(TARGET_PAGE_URL)
    browser = browser_with_required_pages(target)

    selected, context_count, open_count, required_count = _select_target_page(cast(Any, browser))

    assert selected is target
    assert (context_count, open_count, required_count) == (1, 4, 3)


@pytest.mark.parametrize(
    "browser,code",
    [
        (FakeBrowser([FakeContext([])]), "TARGET_PAGE_NOT_FOUND"),
        (
            FakeBrowser(
                [
                    FakeContext([FakePage(TARGET_PAGE_URL)]),
                    FakeContext([FakePage(TARGET_PAGE_URL)]),
                ]
            ),
            "TARGET_PAGE_AMBIGUOUS",
        ),
        (
            FakeBrowser([FakeContext([FakePage(TARGET_PAGE_URL)])]),
            "REQUIRED_CONTEXT_PAGE_MISSING",
        ),
    ],
)
def test_target_context_gate_stops_safely(browser: FakeBrowser, code: str) -> None:
    with pytest.raises(MetadataStopped, match=code):
        _select_target_page(cast(Any, browser))


@pytest.mark.parametrize(
    "selector,code",
    [
        ('input[type="password"]', "LOGIN_REQUIRED"),
        ('iframe[src*="captcha"]', "CAPTCHA_PRESENT"),
        ('[data-testid*="risk"]', "RISK_CONTROL_PRESENT"),
    ],
)
def test_auth_and_risk_selectors_stop_before_reload(selector: str, code: str) -> None:
    page = FakePage(TARGET_PAGE_URL, selector_counts={selector: 1})
    counts = SafetyCounts()

    with pytest.raises(MetadataStopped, match=code):
        asyncio.run(_assert_safety_gate(cast(Any, page), counts))

    assert page.reload_calls == 0


def test_capture_reloads_once_records_at_most_300_and_reads_no_body() -> None:
    accepted = FakeResponse("https://mms.pinduoduo.com/api/product/123456?secret=value")
    ignored = FakeResponse("https://notpinduoduo.com/private", content_type="text/plain")
    target = FakePage(
        f"{TARGET_PAGE_URL}?view=business#section",
        responses=[accepted] * (MAX_METADATA_OBSERVATIONS + 2) + [ignored],
    )
    counts = SafetyCounts()

    result = asyncio.run(
        _capture_metadata(
            cast(Any, target),
            collection_timeout_ms=9000,
            observe_seconds=0,
            counts=counts,
        )
    )

    assert target.reload_calls == 1
    assert target.listeners == []
    assert result["recorded_observation_count"] == MAX_METADATA_OBSERVATIONS
    assert result["capped_response_count"] == 2
    assert result["ignored_response_count"] == 1
    assert result["entries"] == [
        {
            "host": "mms.pinduoduo.com",
            "path": "/api/product/{id}",
            "method": "POST",
            "status": 200,
            "content_type": "application/json",
            "observations": MAX_METADATA_OBSERVATIONS,
        }
    ]
    serialized = json.dumps(result)
    assert "secret" not in serialized
    assert "authorization" not in serialized
    assert counts.reload_actions == 1
    assert counts.response_body_reads == 0


def test_metadata_inventory_uses_manager_stop_and_never_browser_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = FakePage(TARGET_PAGE_URL)
    browser = browser_with_required_pages(target)
    manager = FakeManager(browser)
    monkeypatch.setattr(
        metadata_module,
        "async_playwright",
        lambda: FakePlaywrightStarter(manager),
    )

    result = asyncio.run(
        metadata_inventory(
            MetadataRuntime(
                "http://127.0.0.1:9222",
                connect_timeout_ms=1234,
                collection_timeout_ms=9000,
            ),
            observe_seconds=0,
        )
    )

    assert result["status"] == "PASS"
    assert target.reload_calls == 1
    assert manager.stop_calls == 1
    assert manager.chromium.calls == [
        {
            "endpoint": "http://127.0.0.1:9222",
            "timeout": 1234,
            "is_local": True,
            "no_defaults": True,
        }
    ]
    safety = cast(dict[str, int], result["safety_counts"])
    assert safety["playwright_stop_attempts"] == 1
    assert safety["browser_close_actions"] == 0
    assert safety["page_close_actions"] == 0


def test_main_without_confirmation_stops_before_config_or_browser(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden_load_config(path: object) -> None:
        del path
        raise AssertionError("configuration and browser path must not run without confirmation")

    monkeypatch.setattr(metadata_module, "load_config", forbidden_load_config)
    monkeypatch.setattr(
        "sys.argv",
        [
            "read_only_product_business_metadata.py",
            "--config",
            "unused.toml",
            "--connection-id",
            "conn_current",
        ],
    )

    assert metadata_module.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "STOPPED"
    assert payload["error_code"] == "READ_ONLY_CONFIRMATION_REQUIRED"
    assert payload["safety_counts"]["reload_actions"] == 0
    assert payload["safety_counts"]["playwright_stop_attempts"] == 0
