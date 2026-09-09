from __future__ import annotations

import ast
import asyncio
import inspect
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest

from examples import read_only_product_business_dom_labels as probe_module
from examples.read_only_product_business_dom_labels import ProbeStopped, inspect_browser, probe


class FakeLocator:
    def __init__(self, count: int) -> None:
        self._count = count

    async def count(self) -> int:
        return self._count


class FakePage:
    def __init__(
        self,
        url: str,
        *,
        selector_counts: dict[str, int] | None = None,
        evaluate_result: object | None = None,
        closed: bool = False,
    ) -> None:
        self.url = url
        self.context: FakeContext
        self._selector_counts = selector_counts or {}
        self._evaluate_result = evaluate_result
        self._closed = closed
        self.evaluate_calls: list[tuple[str, object]] = []

    def is_closed(self) -> bool:
        return self._closed

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self._selector_counts.get(selector, 0))

    async def evaluate(self, script: str, argument: object) -> object:
        self.evaluate_calls.append((script, argument))
        return self._evaluate_result


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
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def connect_over_cdp(self, endpoint: str, **kwargs: object) -> FakeBrowser:
        self.calls.append((endpoint, kwargs))
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


def _counts(
    *, exact: int = 0, normalized: int = 0, normalized_contains: int = 0
) -> dict[str, object]:
    return {
        "business": {
            code: {
                "exact_count": exact,
                "normalized_count": normalized,
                "normalized_contains_count": normalized_contains,
            }
            for code in probe_module._BUSINESS_LABELS
        },
        "time": {
            code: {
                "exact_count": exact,
                "normalized_count": normalized,
                "normalized_contains_count": normalized_contains,
            }
            for code in probe_module._TIME_LABELS
        },
    }


def _browser_with_required_pages(target: FakePage) -> FakeBrowser:
    required = [FakePage(url) for url in probe_module._REQUIRED_CONTEXT_URLS.values()]
    return FakeBrowser([FakeContext([target, *required])])


def _clear_selectors() -> dict[str, tuple[str, ...]]:
    return {
        "login": ("login-marker",),
        "captcha": ("captcha-marker",),
        "risk": ("risk-marker",),
    }


def test_inspect_browser_returns_only_presence_and_counts() -> None:
    raw_counts = _counts()
    raw_counts["business"]["product_id"] = {  # type: ignore[index]
        "exact_count": 2,
        "normalized_count": 3,
        "normalized_contains_count": 5,
    }
    raw_counts["time"]["yesterday"] = {  # type: ignore[index]
        "exact_count": 1,
        "normalized_count": 1,
        "normalized_contains_count": 4,
    }
    target = FakePage(
        f"{probe_module._TARGET_URL}?sensitive=query#private",
        evaluate_result=raw_counts,
    )

    result = asyncio.run(
        inspect_browser(
            cast(Any, _browser_with_required_pages(target)),
            safety_selectors=_clear_selectors(),
        )
    )

    product_id = cast(dict[str, object], result["business_labels"])["product_id"]
    yesterday = cast(dict[str, object], result["time_labels"])["yesterday"]
    assert product_id == {
        "exact_present": True,
        "exact_count": 2,
        "normalized_present": True,
        "normalized_count": 3,
        "normalized_contains_present": True,
        "normalized_contains_count": 5,
    }
    assert yesterday == {
        "exact_present": True,
        "exact_count": 1,
        "normalized_present": True,
        "normalized_count": 1,
        "normalized_contains_present": True,
        "normalized_contains_count": 4,
    }
    assert all(cast(dict[str, bool], result["same_context_requirements"]).values())
    assert result["browser_navigation_or_click"] is False
    assert result["page_original_text_output"] is False
    serialized = json.dumps(result, ensure_ascii=False)
    assert "?sensitive=query" not in serialized
    assert "#private" not in serialized
    assert "https://" not in serialized
    assert len(target.evaluate_calls) == 1
    assert target.evaluate_calls[0][1] == {
        "business": probe_module._BUSINESS_LABELS,
        "time": probe_module._TIME_LABELS,
    }


@pytest.mark.parametrize(
    "group,selector,error_code",
    [
        ("login", "login-marker", "LOGIN_SELECTOR_PRESENT"),
        ("captcha", "captcha-marker", "CAPTCHA_SELECTOR_PRESENT"),
        ("risk", "risk-marker", "RISK_SELECTOR_PRESENT"),
    ],
)
def test_safety_selector_stops_before_dom_read(group: str, selector: str, error_code: str) -> None:
    target = FakePage(
        probe_module._TARGET_URL,
        selector_counts={selector: 1},
        evaluate_result=_counts(),
    )
    selectors = _clear_selectors()
    assert selector in selectors[group]

    with pytest.raises(ProbeStopped, match=error_code) as raised:
        asyncio.run(
            inspect_browser(
                cast(Any, _browser_with_required_pages(target)),
                safety_selectors=selectors,
            )
        )

    assert raised.value.code == error_code
    assert target.evaluate_calls == []


@pytest.mark.parametrize(
    "browser,error_code",
    [
        (FakeBrowser([FakeContext([])]), "TARGET_PAGE_NOT_FOUND"),
        (
            FakeBrowser(
                [
                    FakeContext([FakePage(probe_module._TARGET_URL)]),
                    FakeContext([FakePage(probe_module._TARGET_URL)]),
                ]
            ),
            "AMBIGUOUS_TARGET_PAGE",
        ),
        (
            FakeBrowser([FakeContext([FakePage(probe_module._TARGET_URL)])]),
            "REQUIRED_SAME_CONTEXT_PAGE_MISSING",
        ),
    ],
)
def test_target_and_same_context_gate(browser: FakeBrowser, error_code: str) -> None:
    with pytest.raises(ProbeStopped, match=error_code) as raised:
        asyncio.run(inspect_browser(cast(Any, browser), safety_selectors=_clear_selectors()))

    assert raised.value.code == error_code


@pytest.mark.parametrize(
    "invalid_counts",
    [
        {"business": {}, "time": {}},
        {
            "business": {
                code: {
                    "exact_count": -1,
                    "normalized_count": 0,
                    "normalized_contains_count": 0,
                }
                for code in probe_module._BUSINESS_LABELS
            },
            "time": {
                code: {
                    "exact_count": 0,
                    "normalized_count": 0,
                    "normalized_contains_count": 0,
                }
                for code in probe_module._TIME_LABELS
            },
        },
        {
            "business": {
                code: {
                    "exact_count": False,
                    "normalized_count": 0,
                    "normalized_contains_count": 0,
                }
                for code in probe_module._BUSINESS_LABELS
            },
            "time": {
                code: {
                    "exact_count": 0,
                    "normalized_count": 0,
                    "normalized_contains_count": 0,
                }
                for code in probe_module._TIME_LABELS
            },
        },
    ],
)
def test_invalid_dom_count_result_is_rejected(invalid_counts: object) -> None:
    target = FakePage(probe_module._TARGET_URL, evaluate_result=invalid_counts)

    with pytest.raises(ProbeStopped, match="DOM_LABEL_COUNT_RESULT_INVALID"):
        asyncio.run(
            inspect_browser(
                cast(Any, _browser_with_required_pages(target)),
                safety_selectors=_clear_selectors(),
            )
        )


def test_probe_uses_trusted_connection_and_always_stops_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = FakePage(
        probe_module._TARGET_URL,
        evaluate_result=_counts(exact=1, normalized=1, normalized_contains=1),
    )
    manager = FakeManager(_browser_with_required_pages(target))
    monkeypatch.setattr(
        probe_module,
        "async_playwright",
        lambda: FakePlaywrightStarter(manager),
    )
    adapter = SimpleNamespace(login_selector="", captcha_selector="", error_selector="")
    connection = SimpleNamespace(
        real_collection_enabled=True,
        cdp_endpoint="http://127.0.0.1:9222",
        product_catalog_adapter=adapter,
        inventory_adapter=adapter,
        store_overview_adapter=adapter,
    )
    config = SimpleNamespace(
        collection=SimpleNamespace(connect_timeout_ms=4321),
        connection=lambda connection_id: connection
        if connection_id == "trusted_connection"
        else (_ for _ in ()).throw(KeyError(connection_id)),
    )

    result = asyncio.run(probe(cast(Any, config), "trusted_connection"))

    assert result["status"] == "PASS"
    assert manager.chromium.calls == [
        (
            "http://127.0.0.1:9222",
            {"timeout": 4321, "is_local": True, "no_defaults": True},
        )
    ]
    assert manager.stop_calls == 1


def test_probe_source_contains_no_browser_navigation_or_close_calls() -> None:
    tree = ast.parse(inspect.getsource(probe_module))
    called_attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert called_attributes.isdisjoint({"reload", "goto", "click", "close"})
    assert "stop" in called_attributes
