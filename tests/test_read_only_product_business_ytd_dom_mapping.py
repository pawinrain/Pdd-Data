from __future__ import annotations

import ast
import asyncio
import inspect
import json
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from examples import read_only_product_business_ytd_dom_mapping as mapping_module
from examples.read_only_product_business_semantics import (
    IDENTITY_PAGE_URL,
    REQUIRED_SAME_CONTEXT_PAGES,
    TARGET_PAGE_URL,
    SemanticsRuntime,
    SemanticsStopped,
)
from examples.read_only_product_business_ytd_dom_mapping import (
    BUSINESS_LABEL_ALIASES,
    COUNTEREVIDENCE_PHRASES,
    MappingStopped,
    _validated_result,
    inspect_dom,
)


def container(alias_code: str, *, yesterday: int = 1) -> dict[str, object]:
    return {
        "alias_code": alias_code,
        "found": True,
        "depth": 2,
        "today_exact_count": 1,
        "yesterday_exact_count": yesterday,
        "tag_category": "TD",
        "role_category": "CELL",
        "colspan_category": "MISSING",
        "rowspan_category": "MISSING",
    }


def raw_payload() -> dict[str, object]:
    fields: dict[str, object] = {}
    for field, aliases in BUSINESS_LABEL_ALIASES.items():
        first = next(iter(aliases))
        fields[field] = {
            "alias_visible_exact_counts": {alias: 1 if alias == first else 0 for alias in aliases},
            "visible_exact_element_count": 1,
            "alias_match_status": "UNIQUE",
            "matched_alias_code": first,
            "in_table_count": 1,
            "in_thead_count": 0,
            "in_columnheader_count": 0,
            "container_observations": [container(first)],
            "container_observations_truncated": False,
        }
    return {
        "scanned_element_count": 120,
        "scan_truncated": False,
        "structure_counts": {
            "visible_table_count": 1,
            "visible_grid_count": 0,
            "visible_thead_count": 1,
            "visible_columnheader_count": 8,
        },
        "time_label_counts": {
            "today_visible_exact_count": 6,
            "yesterday_visible_exact_count": 13,
        },
        "fields": fields,
        "counterevidence_phrase_counts": {code: 0 for code in COUNTEREVIDENCE_PHRASES},
    }


class FakeLocator:
    def __init__(self, *, count: int = 0, text: str | None = None) -> None:
        self._count = count
        self._text = text

    async def count(self) -> int:
        return self._count

    async def text_content(self) -> str | None:
        return self._text


class FakePage:
    def __init__(
        self,
        url: str,
        *,
        identity_text: str | None = None,
        evaluate_result: object | None = None,
        selector_counts: dict[str, int] | None = None,
    ) -> None:
        self.url = url
        self.context: FakeContext
        self.identity_text = identity_text
        self.evaluate_result = evaluate_result
        self.selector_counts = selector_counts or {}
        self.evaluate_calls: list[tuple[str, object]] = []

    def is_closed(self) -> bool:
        return False

    def locator(self, selector: str) -> FakeLocator:
        if selector == "html":
            return FakeLocator(count=1, text=self.identity_text)
        return FakeLocator(count=self.selector_counts.get(selector, 0))

    async def evaluate(self, script: str, argument: object) -> object:
        self.evaluate_calls.append((script, argument))
        return self.evaluate_result


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


def fake_browser(
    payload: object,
    *,
    identity_text: str = "店铺ID:123456789",
    target_selectors: dict[str, int] | None = None,
) -> tuple[FakeBrowser, FakePage]:
    target = FakePage(
        TARGET_PAGE_URL,
        evaluate_result=payload,
        selector_counts=target_selectors,
    )
    identity = FakePage(IDENTITY_PAGE_URL, identity_text=identity_text)
    required = [FakePage(url) for url in REQUIRED_SAME_CONTEXT_PAGES]
    return FakeBrowser([FakeContext([target, identity, *required])]), target


def runtime(expected: str = "123456789") -> SemanticsRuntime:
    return SemanticsRuntime(
        cdp_endpoint="http://127.0.0.1:9222",
        connect_timeout_ms=1234,
        collection_timeout_ms=4321,
        expected_store_id=expected,
        expected_store_id_sha256="",
    )


def test_validator_returns_only_fixed_codes_counts_and_categories() -> None:
    result = _validated_result(raw_payload())

    assert result["ytd_anchor_status"] == "STRUCTURAL_CANDIDATE"
    fields = cast(dict[str, dict[str, object]], result["fields"])
    assert fields["paid_amount"]["matched_alias_code"] == "PAID_AMOUNT_YUAN_ASCII"
    assert fields["goods_page_view_count"]["yesterday_container_present"] is True
    assert result["raw_text_output"] is False
    serialized = json.dumps(result, ensure_ascii=False)
    for labels in BUSINESS_LABEL_ALIASES.values():
        for label in labels.values():
            assert label not in serialized
    assert "123456789" not in serialized


def test_counterevidence_or_missing_yesterday_container_keeps_anchor_unverified() -> None:
    counterexample = raw_payload()
    counter_counts = cast(dict[str, int], counterexample["counterevidence_phrase_counts"])
    counter_counts["YESTERDAY_SAME_PERIOD"] = 1
    assert _validated_result(counterexample)["ytd_anchor_status"] == "UNVERIFIED"

    missing = raw_payload()
    fields = cast(dict[str, dict[str, object]], missing["fields"])
    observations = cast(
        list[dict[str, object]],
        fields["goods_visitor_count"]["container_observations"],
    )
    observations[0] = {
        "alias_code": observations[0]["alias_code"],
        "found": False,
        "depth": None,
        "today_exact_count": 0,
        "yesterday_exact_count": 0,
        "tag_category": None,
        "role_category": None,
        "colspan_category": None,
        "rowspan_category": None,
    }
    assert _validated_result(missing)["ytd_anchor_status"] == "UNVERIFIED"


@pytest.mark.parametrize(
    "mutation",
    [
        "scan_truncated",
        "unknown_field",
        "unknown_tag",
        "negative_count",
        "alias_status_lie",
    ],
)
def test_validator_fails_closed_on_untrusted_dom_result(mutation: str) -> None:
    payload = raw_payload()
    fields = cast(dict[str, dict[str, object]], payload["fields"])
    first = fields["paying_buyer_count"]
    observations = cast(list[dict[str, object]], first["container_observations"])
    if mutation == "scan_truncated":
        payload["scan_truncated"] = True
    elif mutation == "unknown_field":
        fields["PRIVATE"] = first
    elif mutation == "unknown_tag":
        observations[0]["tag_category"] = "PRIVATE_TAG"
    elif mutation == "negative_count":
        first["visible_exact_element_count"] = -1
    else:
        first["alias_match_status"] = "NONE"

    with pytest.raises(MappingStopped, match="DOM_MAPPING_RESULT_INVALID"):
        _validated_result(payload)


def test_inspect_dom_uses_identity_and_context_gates_without_browser_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser, target = fake_browser(raw_payload())
    manager = FakeManager(browser)
    monkeypatch.setattr(
        mapping_module,
        "async_playwright",
        lambda: FakePlaywrightStarter(manager),
    )

    result = asyncio.run(inspect_dom(runtime()))

    assert result["status"] == "PASS"
    assert result["identity_verified"] is True
    assert result["required_same_context_page_count"] == 3
    assert manager.chromium.calls == [
        (
            "http://127.0.0.1:9222",
            {"timeout": 1234, "is_local": True, "no_defaults": True},
        )
    ]
    assert manager.stop_calls == 1
    assert len(target.evaluate_calls) == 1
    safety = cast(dict[str, int], result["safety_counts"])
    for key in (
        "reload_actions",
        "navigation_actions",
        "click_actions",
        "page_close_actions",
        "browser_close_actions",
        "response_events_seen",
        "request_json_reads",
        "response_body_reads",
    ):
        assert safety[key] == 0
    assert safety["identity_dom_reads"] == 1
    assert safety["playwright_stop_attempts"] == 1


def test_identity_mismatch_and_safety_marker_stop_before_structure_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser, target = fake_browser(raw_payload(), identity_text="店铺ID:999999999")
    manager = FakeManager(browser)
    monkeypatch.setattr(
        mapping_module,
        "async_playwright",
        lambda: FakePlaywrightStarter(manager),
    )
    with pytest.raises(SemanticsStopped, match="IDENTITY_MISMATCH"):
        asyncio.run(inspect_dom(runtime()))
    assert target.evaluate_calls == []
    assert manager.stop_calls == 1

    browser, target = fake_browser(raw_payload(), target_selectors={'input[type="password"]': 1})
    manager = FakeManager(browser)
    monkeypatch.setattr(
        mapping_module,
        "async_playwright",
        lambda: FakePlaywrightStarter(manager),
    )
    with pytest.raises(SemanticsStopped, match="LOGIN_REQUIRED"):
        asyncio.run(inspect_dom(runtime()))
    assert target.evaluate_calls == []
    assert manager.stop_calls == 1


def test_main_requires_confirmation_before_config_or_browser(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden(*_args: object) -> None:
        raise AssertionError("config must not be loaded")

    monkeypatch.setattr(mapping_module, "_load_runtime", forbidden)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "read_only_product_business_ytd_dom_mapping.py",
            "--config",
            str(Path("unused.toml")),
            "--connection-id",
            "unused",
        ],
    )

    assert mapping_module.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_code"] == "READ_ONLY_CONFIRMATION_REQUIRED"


def test_direct_script_help_runs_in_real_subprocess_without_import_error() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "examples" / "read_only_product_business_ytd_dom_mapping.py"

    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0
    assert "--confirm-read-only" in completed.stdout
    assert "ModuleNotFoundError" not in completed.stderr


def test_source_has_no_page_or_response_mutation_calls() -> None:
    tree = ast.parse(inspect.getsource(mapping_module))
    called_attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert called_attributes.isdisjoint(
        {"reload", "goto", "click", "close", "body", "post_data_json"}
    )
    assert "stop" in called_attributes


def test_error_payload_contains_only_fixed_empty_evidence_and_zero_action_claim() -> None:
    payload = mapping_module._error_payload("IDENTITY_MISMATCH")
    assert payload["fields"] is None
    assert payload["counterevidence_phrase_counts"] is None
    assert set(cast(dict[str, int], payload["safety_claim"]).values()) == {0}
