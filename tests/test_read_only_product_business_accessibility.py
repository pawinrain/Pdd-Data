from __future__ import annotations

import ast
import asyncio
import copy
import inspect
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from examples import read_only_product_business_accessibility as accessibility_module
from examples.read_only_product_business_accessibility import (
    AccessibilityRuntime,
    AccessibilityStopped,
    SafetyCounts,
    _error_payload,
    _is_trusted_loopback_endpoint,
    _load_runtime,
    _matches_exact_page,
    _validate_dom_result,
    inspect_browser,
    probe,
)


class FakeLocator:
    def __init__(self, count: int, inner_text: str) -> None:
        self._count = count
        self._inner_text = inner_text
        self.count_calls = 0
        self.inner_text_calls: list[int] = []

    async def count(self) -> int:
        self.count_calls += 1
        return self._count

    async def inner_text(self, *, timeout: int) -> str:
        self.inner_text_calls.append(timeout)
        return self._inner_text


class FakePage:
    def __init__(
        self,
        url: str,
        *,
        identity_text: str = "",
        selector_counts: dict[str, int] | None = None,
        evaluate_result: object | None = None,
        closed: bool = False,
    ) -> None:
        self.url = url
        self.context: FakeContext
        self._identity_text = identity_text
        self._selector_counts = selector_counts or {}
        self._evaluate_result = evaluate_result
        self._closed = closed
        self.locator_calls: list[str] = []
        self.locators: list[FakeLocator] = []
        self.evaluate_calls: list[tuple[str, object]] = []

    def is_closed(self) -> bool:
        return self._closed

    def locator(self, selector: str) -> FakeLocator:
        self.locator_calls.append(selector)
        locator = FakeLocator(
            self._selector_counts.get(selector, 0),
            self._identity_text if selector == "body" else "",
        )
        self.locators.append(locator)
        return locator

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


def _empty_categories() -> dict[str, int]:
    return {category: 0 for category in accessibility_module.NUMERIC_GRAMMAR_CATEGORIES}


def _empty_common_counts() -> dict[str, int]:
    return {key: 0 for key in accessibility_module.COMMON_CONTAINER_COUNT_KEYS}


def _valid_raw_result() -> dict[str, object]:
    categories = {
        source: _empty_categories() for source in accessibility_module.ACCESSIBILITY_SOURCES
    }
    categories["aria_label"]["integer"] = 2
    categories["aria_label"]["other"] = 1
    categories["aria_valuetext"]["decimal"] = 1
    categories["input_value"]["empty"] = 1
    common = {metric: _empty_common_counts() for metric in accessibility_module.METRIC_LABELS}
    common["paid_amount"] = {
        "label_element_count": 2,
        "private_use_container_count": 2,
        "aria_label_numeric_container_count": 1,
        "aria_valuetext_numeric_container_count": 1,
        "input_value_numeric_container_count": 0,
        "any_standardized_numeric_container_count": 2,
    }
    return {
        "scanned_element_count": 20,
        "visible_element_count": 12,
        "element_scan_truncated": False,
        "scanned_text_node_count": 18,
        "text_node_scan_truncated": False,
        "candidate_scan_truncated": False,
        "private_use_element_count": 2,
        "private_use_text_node_count": 3,
        "standardized_accessibility_attr_present_counts": {
            "aria_label": 3,
            "aria_valuetext": 1,
            "input_value": 1,
            "any": 4,
        },
        "numeric_grammar_category_counts": categories,
        "metric_label_common_container_counts": common,
    }


def _runtime(*, expected_store_id: str = "12345678") -> AccessibilityRuntime:
    return AccessibilityRuntime(
        cdp_endpoint="http://127.0.0.1:9222",
        connect_timeout_ms=1_234,
        collection_timeout_ms=5_000,
        expected_store_id=expected_store_id,
        expected_store_id_sha256="",
    )


def _browser_with_required_pages(
    target: FakePage,
    *,
    identity_text: str = "店铺 ID 12345678",
    separate_identity_context: bool = False,
) -> tuple[FakeBrowser, FakePage, list[FakePage]]:
    identity = FakePage(
        accessibility_module.IDENTITY_PAGE_URL,
        identity_text=identity_text,
    )
    required = [FakePage(url) for url in accessibility_module.REQUIRED_SAME_CONTEXT_PAGES]
    target_context = FakeContext(
        [target, *required] if separate_identity_context else [target, identity, *required]
    )
    contexts = [target_context]
    if separate_identity_context:
        contexts.append(FakeContext([identity]))
    return FakeBrowser(contexts), identity, required


def test_validate_dom_result_returns_counts_only_and_canonical_key_order() -> None:
    result = _validate_dom_result(_valid_raw_result())

    assert result["private_use_counts"] == {"element_count": 2, "text_node_count": 3}
    assert result["standardized_accessibility_attr_present_counts"] == {
        "any": 4,
        "aria_label": 3,
        "aria_valuetext": 1,
        "input_value": 1,
    }
    categories = cast(dict[str, dict[str, int]], result["numeric_grammar_category_counts"])
    assert categories["aria_label"]["integer"] == 2
    assert categories["aria_label"]["other"] == 1
    assert set(categories) == accessibility_module.ACCESSIBILITY_SOURCES
    assert set(result) == {
        "scan_counts",
        "private_use_counts",
        "standardized_accessibility_attr_present_counts",
        "numeric_grammar_category_counts",
        "metric_label_common_container_counts",
    }


def _invalid_mutations() -> list[dict[str, object]]:
    mutations: list[dict[str, object]] = []

    extra_key = copy.deepcopy(_valid_raw_result())
    extra_key["raw_text"] = "must never be accepted"
    mutations.append(extra_key)

    boolean_count = copy.deepcopy(_valid_raw_result())
    boolean_count["private_use_element_count"] = True
    mutations.append(boolean_count)

    too_many_visible = copy.deepcopy(_valid_raw_result())
    too_many_visible["visible_element_count"] = 21
    mutations.append(too_many_visible)

    too_many_private_nodes = copy.deepcopy(_valid_raw_result())
    too_many_private_nodes["private_use_text_node_count"] = 19
    mutations.append(too_many_private_nodes)

    invalid_truncation = copy.deepcopy(_valid_raw_result())
    invalid_truncation["candidate_scan_truncated"] = 0
    mutations.append(invalid_truncation)

    category_sum = copy.deepcopy(_valid_raw_result())
    cast(dict[str, dict[str, int]], category_sum["numeric_grammar_category_counts"])["aria_label"][
        "integer"
    ] = 1
    mutations.append(category_sum)

    unknown_category = copy.deepcopy(_valid_raw_result())
    cast(dict[str, dict[str, int]], unknown_category["numeric_grammar_category_counts"])[
        "aria_label"
    ]["raw"] = 0
    mutations.append(unknown_category)

    impossible_any_attr = copy.deepcopy(_valid_raw_result())
    cast(
        dict[str, int],
        impossible_any_attr["standardized_accessibility_attr_present_counts"],
    )["any"] = 2
    mutations.append(impossible_any_attr)

    more_common_than_labels = copy.deepcopy(_valid_raw_result())
    cast(
        dict[str, dict[str, int]],
        more_common_than_labels["metric_label_common_container_counts"],
    )["paid_amount"]["private_use_container_count"] = 3
    mutations.append(more_common_than_labels)

    accessible_without_private = copy.deepcopy(_valid_raw_result())
    cast(
        dict[str, dict[str, int]],
        accessible_without_private["metric_label_common_container_counts"],
    )["paid_amount"]["private_use_container_count"] = 0
    mutations.append(accessible_without_private)

    any_below_source = copy.deepcopy(_valid_raw_result())
    cast(
        dict[str, dict[str, int]],
        any_below_source["metric_label_common_container_counts"],
    )["paid_amount"]["any_standardized_numeric_container_count"] = 0
    mutations.append(any_below_source)
    return mutations


@pytest.mark.parametrize("invalid", _invalid_mutations())
def test_validate_dom_result_fails_closed_on_invalid_or_extra_data(
    invalid: dict[str, object],
) -> None:
    with pytest.raises(AccessibilityStopped, match="ACCESSIBILITY_RESULT_INVALID") as raised:
        _validate_dom_result(invalid)

    assert raised.value.code == "ACCESSIBILITY_RESULT_INVALID"


def test_inspect_browser_returns_no_values_identity_or_page_metadata() -> None:
    target = FakePage(
        f"{accessibility_module.TARGET_PAGE_URL}?token-leak=987654321#fragment-leak",
        evaluate_result=_valid_raw_result(),
    )
    browser, identity, required = _browser_with_required_pages(
        target,
        identity_text="当前店铺 12345678 私密店铺名",
    )
    counts = SafetyCounts()

    result = asyncio.run(inspect_browser(cast(Any, browser), _runtime(), counts))

    assert result["status"] == "PASS"
    assert result["identity_verified"] is True
    assert result["page_gate_counts"] == {
        "context_count": 1,
        "open_page_count": 5,
        "target_page_count": 1,
        "identity_page_count": 1,
        "required_same_context_page_count": 3,
    }
    assert result["private_use_counts"] == {"element_count": 2, "text_node_count": 3}
    serialized = json.dumps(result, ensure_ascii=False)
    for secret in (
        "987654321",
        "token-leak",
        "fragment-leak",
        "12345678",
        "私密店铺名",
        "https://",
        "支付金额",
    ):
        assert secret not in serialized
    assert len(target.evaluate_calls) == 1
    script, argument = target.evaluate_calls[0]
    assert script == accessibility_module._ACCESSIBILITY_PROBE_SCRIPT
    assert argument == {
        "max_elements": accessibility_module.MAX_ELEMENT_SCAN,
        "max_text_nodes": accessibility_module.MAX_TEXT_NODE_SCAN,
        "max_candidates": accessibility_module.MAX_CANDIDATE_ELEMENTS,
        "max_ancestor_depth": accessibility_module.MAX_COMMON_ANCESTOR_DEPTH,
        "metric_labels": accessibility_module.METRIC_LABELS,
    }
    assert identity.locator_calls.count("body") == 1
    assert "html" not in identity.locator_calls
    assert all(not page.evaluate_calls for page in [identity, *required])
    safety = cast(dict[str, int], result["safety_counts"])
    assert safety["identity_dom_reads"] == 1
    assert safety["accessibility_dom_reads"] == 1
    assert safety["safety_selector_checks"] > 0
    for key, value in safety.items():
        if key not in {"identity_dom_reads", "accessibility_dom_reads", "safety_selector_checks"}:
            assert value == 0


@pytest.mark.parametrize(
    "selector,error_code",
    [
        (accessibility_module._LOGIN_SELECTORS[0], "LOGIN_REQUIRED"),
        (accessibility_module._CAPTCHA_SELECTORS[0], "CAPTCHA_PRESENT"),
        (accessibility_module._RISK_SELECTORS[0], "RISK_CONTROL_PRESENT"),
    ],
)
def test_login_captcha_and_risk_gates_stop_before_identity_or_dom_scan(
    selector: str, error_code: str
) -> None:
    target = FakePage(
        accessibility_module.TARGET_PAGE_URL,
        selector_counts={selector: 1},
        evaluate_result=_valid_raw_result(),
    )
    browser, identity, _ = _browser_with_required_pages(target)
    counts = SafetyCounts()

    with pytest.raises(AccessibilityStopped, match=error_code) as raised:
        asyncio.run(inspect_browser(cast(Any, browser), _runtime(), counts))

    assert raised.value.code == error_code
    assert target.evaluate_calls == []
    assert identity.locator_calls == []
    assert counts.identity_dom_reads == 0
    assert counts.accessibility_dom_reads == 0


@pytest.mark.parametrize(
    "browser,error_code",
    [
        (FakeBrowser([FakeContext([])]), "TARGET_PAGE_NOT_FOUND"),
        (
            FakeBrowser(
                [
                    FakeContext(
                        [
                            FakePage(accessibility_module.TARGET_PAGE_URL),
                            FakePage(accessibility_module.TARGET_PAGE_URL),
                        ]
                    )
                ]
            ),
            "TARGET_PAGE_AMBIGUOUS",
        ),
        (
            FakeBrowser([FakeContext([FakePage(accessibility_module.TARGET_PAGE_URL)])]),
            "IDENTITY_PAGE_NOT_FOUND",
        ),
    ],
)
def test_target_and_identity_page_cardinality_gates(browser: FakeBrowser, error_code: str) -> None:
    with pytest.raises(AccessibilityStopped, match=error_code) as raised:
        asyncio.run(inspect_browser(cast(Any, browser), _runtime(), SafetyCounts()))

    assert raised.value.code == error_code


def test_identity_must_share_context_with_target() -> None:
    target = FakePage(
        accessibility_module.TARGET_PAGE_URL,
        evaluate_result=_valid_raw_result(),
    )
    browser, _, _ = _browser_with_required_pages(target, separate_identity_context=True)

    with pytest.raises(AccessibilityStopped, match="IDENTITY_PAGE_CONTEXT_MISMATCH") as raised:
        asyncio.run(inspect_browser(cast(Any, browser), _runtime(), SafetyCounts()))

    assert raised.value.code == "IDENTITY_PAGE_CONTEXT_MISMATCH"
    assert target.evaluate_calls == []


def test_all_required_pages_must_be_unique_in_target_context() -> None:
    target = FakePage(
        accessibility_module.TARGET_PAGE_URL,
        evaluate_result=_valid_raw_result(),
    )
    identity = FakePage(
        accessibility_module.IDENTITY_PAGE_URL,
        identity_text="12345678",
    )
    required_url = accessibility_module.REQUIRED_SAME_CONTEXT_PAGES[0]
    duplicated = [FakePage(required_url), FakePage(required_url)]
    others = [FakePage(url) for url in accessibility_module.REQUIRED_SAME_CONTEXT_PAGES[1:]]
    browser = FakeBrowser([FakeContext([target, identity, *duplicated, *others])])

    with pytest.raises(AccessibilityStopped, match="REQUIRED_CONTEXT_PAGE_AMBIGUOUS") as raised:
        asyncio.run(inspect_browser(cast(Any, browser), _runtime(), SafetyCounts()))

    assert raised.value.code == "REQUIRED_CONTEXT_PAGE_AMBIGUOUS"
    assert target.evaluate_calls == []


@pytest.mark.parametrize(
    "identity_text,error_code",
    [
        ("没有数字身份", "IDENTITY_UNVERIFIED"),
        ("当前店铺 87654321", "IDENTITY_MISMATCH"),
    ],
)
def test_identity_gate_fails_closed_without_exact_match(
    identity_text: str, error_code: str
) -> None:
    target = FakePage(
        accessibility_module.TARGET_PAGE_URL,
        evaluate_result=_valid_raw_result(),
    )
    browser, _, _ = _browser_with_required_pages(target, identity_text=identity_text)

    with pytest.raises(AccessibilityStopped, match=error_code) as raised:
        asyncio.run(inspect_browser(cast(Any, browser), _runtime(), SafetyCounts()))

    assert raised.value.code == error_code
    assert target.evaluate_calls == []


def test_identity_gate_supports_configured_hash_without_outputting_candidate() -> None:
    import hashlib

    identity = "12345678"
    target = FakePage(
        accessibility_module.TARGET_PAGE_URL,
        evaluate_result=_valid_raw_result(),
    )
    browser, _, _ = _browser_with_required_pages(target, identity_text=identity)
    runtime = AccessibilityRuntime(
        cdp_endpoint="http://127.0.0.1:9222",
        connect_timeout_ms=1_234,
        collection_timeout_ms=5_000,
        expected_store_id="",
        expected_store_id_sha256=hashlib.sha256(identity.encode()).hexdigest(),
    )

    result = asyncio.run(inspect_browser(cast(Any, browser), runtime, SafetyCounts()))

    assert result["identity_verified"] is True
    assert identity not in json.dumps(result)


@pytest.mark.parametrize(
    "endpoint,expected",
    [
        ("http://127.0.0.1:9222", True),
        ("http://localhost:9222", True),
        ("ws://[::1]:9222/devtools/browser/session", True),
        ("https://127.0.0.1:9222", False),
        ("http://0.0.0.0:9222", False),
        ("http://127.0.0.1", False),
        ("http://user@127.0.0.1:9222", False),
        ("http://127.0.0.1:9222?token=secret", False),
        ("http://127.0.0.1:9222#secret", False),
    ],
)
def test_only_trusted_loopback_cdp_endpoints_are_accepted(endpoint: str, expected: bool) -> None:
    assert _is_trusted_loopback_endpoint(endpoint) is expected


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://mms.pinduoduo.com/sycm/goods_effect", True),
        ("https://mms.pinduoduo.com/sycm/goods_effect?secret=value", True),
        ("https://mms.pinduoduo.com:443/sycm/goods_effect#private", True),
        ("http://mms.pinduoduo.com/sycm/goods_effect", False),
        ("https://mms.pinduoduo.com.evil.test/sycm/goods_effect", False),
        ("https://user@mms.pinduoduo.com/sycm/goods_effect", False),
        ("https://mms.pinduoduo.com/sycm/goods_effect/other", False),
    ],
)
def test_exact_page_match_does_not_confuse_host_or_path(url: str, expected: bool) -> None:
    assert _matches_exact_page(url, accessibility_module.TARGET_PAGE_URL) is expected


def test_runtime_loader_enforces_real_mode_identity_and_fixed_identity_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = SimpleNamespace(identity_page_url=accessibility_module.IDENTITY_PAGE_URL)
    connection = SimpleNamespace(
        real_collection_enabled=True,
        cdp_endpoint="http://127.0.0.1:9222",
        expected_platform_store_id="12345678",
        expected_platform_store_id_sha256="",
        product_catalog_adapter=adapter,
    )
    config = SimpleNamespace(
        service=SimpleNamespace(test_mode=False),
        collection=SimpleNamespace(
            connect_timeout_ms=1_234,
            collection_timeout_ms=5_678,
        ),
        connection=lambda connection_id: connection
        if connection_id == "conn_current"
        else (_ for _ in ()).throw(KeyError(connection_id)),
    )
    monkeypatch.setattr(accessibility_module, "load_config", lambda path: config)

    runtime = _load_runtime(Path("unused.toml"), "conn_current")

    assert runtime == AccessibilityRuntime(
        cdp_endpoint="http://127.0.0.1:9222",
        connect_timeout_ms=1_234,
        collection_timeout_ms=5_678,
        expected_store_id="12345678",
        expected_store_id_sha256="",
    )


@pytest.mark.parametrize(
    "mutator,error_code",
    [
        (
            lambda config, connection: setattr(config.service, "test_mode", True),
            "REAL_MODE_REQUIRED",
        ),
        (
            lambda config, connection: setattr(connection, "real_collection_enabled", False),
            "REAL_COLLECTION_NOT_ENABLED",
        ),
        (
            lambda config, connection: setattr(connection, "cdp_endpoint", None),
            "CDP_ENDPOINT_NOT_CONFIGURED",
        ),
        (
            lambda config, connection: setattr(connection, "cdp_endpoint", "http://10.0.0.1:9222"),
            "CDP_ENDPOINT_NOT_TRUSTED",
        ),
        (
            lambda config, connection: setattr(connection, "expected_platform_store_id", ""),
            "EXPECTED_IDENTITY_NOT_CONFIGURED",
        ),
        (
            lambda config, connection: setattr(
                connection.product_catalog_adapter,
                "identity_page_url",
                "https://mms.pinduoduo.com/home",
            ),
            "IDENTITY_PAGE_CONFIG_MISMATCH",
        ),
    ],
)
def test_runtime_loader_fails_closed_on_unsafe_configuration(
    monkeypatch: pytest.MonkeyPatch,
    mutator: Any,
    error_code: str,
) -> None:
    adapter = SimpleNamespace(identity_page_url=accessibility_module.IDENTITY_PAGE_URL)
    connection = SimpleNamespace(
        real_collection_enabled=True,
        cdp_endpoint="http://127.0.0.1:9222",
        expected_platform_store_id="12345678",
        expected_platform_store_id_sha256="",
        product_catalog_adapter=adapter,
    )
    config = SimpleNamespace(
        service=SimpleNamespace(test_mode=False),
        collection=SimpleNamespace(
            connect_timeout_ms=1_234,
            collection_timeout_ms=5_678,
        ),
        connection=lambda connection_id: connection,
    )
    mutator(config, connection)
    monkeypatch.setattr(accessibility_module, "load_config", lambda path: config)

    with pytest.raises(AccessibilityStopped, match=error_code) as raised:
        _load_runtime(Path("unused.toml"), "conn_current")

    assert raised.value.code == error_code


def test_probe_connects_only_to_configured_loopback_and_stops_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = FakePage(
        accessibility_module.TARGET_PAGE_URL,
        evaluate_result=_valid_raw_result(),
    )
    browser, _, _ = _browser_with_required_pages(target)
    manager = FakeManager(browser)
    monkeypatch.setattr(
        accessibility_module,
        "async_playwright",
        lambda: FakePlaywrightStarter(manager),
    )

    result = asyncio.run(probe(_runtime()))

    assert result["status"] == "PASS"
    assert manager.chromium.calls == [
        {
            "endpoint": "http://127.0.0.1:9222",
            "timeout": 1_234,
            "is_local": True,
            "no_defaults": True,
        }
    ]
    assert manager.stop_calls == 1


def test_probe_failure_preserves_executed_safety_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = FakePage(
        accessibility_module.TARGET_PAGE_URL,
        evaluate_result=_valid_raw_result(),
    )
    browser, _, _ = _browser_with_required_pages(
        target,
        identity_text="当前店铺 87654321",
    )
    manager = FakeManager(browser)
    monkeypatch.setattr(
        accessibility_module,
        "async_playwright",
        lambda: FakePlaywrightStarter(manager),
    )

    with pytest.raises(AccessibilityStopped, match="IDENTITY_MISMATCH") as raised:
        asyncio.run(probe(_runtime()))

    assert raised.value.counts is not None
    assert raised.value.counts.identity_dom_reads == 1
    assert raised.value.counts.safety_selector_checks > 0
    assert raised.value.counts.accessibility_dom_reads == 0
    payload = _error_payload(raised.value.code, raised.value.counts)
    safety = cast(dict[str, int], payload["safety_counts"])
    assert safety["identity_dom_reads"] == 1
    assert safety["safety_selector_checks"] > 0
    assert safety["accessibility_dom_reads"] == 0


def test_error_payload_has_fixed_codes_counts_and_no_dynamic_detail() -> None:
    payload = _error_payload("ACCESSIBILITY_PROBE_FAILED")

    assert payload["status"] == "STOPPED"
    assert payload["error_code"] == "ACCESSIBILITY_PROBE_FAILED"
    assert payload["private_use_counts"] is None
    assert set(cast(dict[str, int], payload["safety_counts"]).values()) == {0}
    assert set(payload) == {
        "status",
        "error_code",
        "private_use_counts",
        "standardized_accessibility_attr_present_counts",
        "numeric_grammar_category_counts",
        "metric_label_common_container_counts",
        "safety_counts",
    }


def test_source_contains_no_browser_mutation_response_or_forbidden_dom_channels() -> None:
    source = inspect.getsource(accessibility_module)
    assert 'locator("html").text_content' not in source
    tree = ast.parse(source)
    forbidden_method_calls = {
        "add_script_tag",
        "check",
        "click",
        "close",
        "dblclick",
        "dispatch_event",
        "drag_to",
        "fill",
        "focus",
        "goto",
        "hover",
        "on",
        "press",
        "reload",
        "request",
        "route",
        "scroll_into_view_if_needed",
        "select_option",
        "set_checked",
        "set_content",
        "tap",
        "type",
        "uncheck",
    }
    called_attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert called_attributes.isdisjoint(forbidden_method_calls)

    script = accessibility_module._ACCESSIBILITY_PROBE_SCRIPT
    script_lower = script.casefold()
    for forbidden in (
        "document.fonts",
        "font-face",
        "fontfamily",
        "canvas",
        "getimagedata",
        "todataurl",
        "ocr",
        "data-",
        "innerhtml",
        "outerhtml",
        "document.scripts",
        "localstorage",
        "sessionstorage",
        "xmlhttprequest",
        "fetch(",
        ".click(",
        ".scroll",
        "classname",
        ".title",
    ):
        assert forbidden not in script_lower
    attribute_reads = set(re.findall(r"(?:get|has)Attribute\('([^']+)'\)", script))
    assert attribute_reads == {"aria-label", "aria-valuetext"}
    assert "HTMLInputElement" in script
    assert ".value" in script
    assert "TEXT_NODE" in script
    assert "hasPrivateUse" in script


def test_cli_requires_explicit_read_only_confirmation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "read_only_product_business_accessibility.py",
            "--config",
            "unused.toml",
            "--connection-id",
            "conn_current",
        ],
    )

    exit_code = accessibility_module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["status"] == "STOPPED"
    assert payload["error_code"] == "READ_ONLY_CONFIRMATION_REQUIRED"
    assert set(payload["safety_counts"].values()) == {0}
