from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest

from examples import read_only_product_business_shape as shape_module
from examples.read_only_product_business_shape import (
    CANDIDATE_RESPONSE_PATHS,
    MAX_RESPONSE_BYTES,
    REQUIRED_SAME_CONTEXT_PAGES,
    TARGET_PAGE_URL,
    SafetyCounts,
    ShapeRuntime,
    ShapeStopped,
    _aggregate_observations,
    _array_path_lengths,
    _candidate_path,
    _capture_shapes,
    _probe_candidate,
    _strict_json,
    shape_inventory,
)


class FakeLocator:
    def __init__(self, count: int) -> None:
        self._count = count

    async def count(self) -> int:
        return self._count


class FakeRequest:
    def __init__(self, payload: object, *, method: str = "POST") -> None:
        self._payload = payload
        self.method = method
        self.post_data_json_reads = 0

    @property
    def post_data_json(self) -> object:
        self.post_data_json_reads += 1
        return self._payload


class FakeResponse:
    def __init__(
        self,
        path: str,
        *,
        request_payload: object | None = None,
        response_body: bytes = b'{"success":true,"result":{"rows":[]}}',
        host: str = "mms.pinduoduo.com",
        method: str = "POST",
        status: int = 200,
        content_type: str = "application/json; charset=utf-8",
        content_length: str | None = None,
    ) -> None:
        self.url = f"https://{host}{path}?token=must-not-output#fragment"
        self.request = FakeRequest(request_payload or {"page": 1}, method=method)
        self.status = status
        self.headers = {"content-type": content_type, "authorization": "must-not-output"}
        if content_length is not None:
            self.headers["content-length"] = content_length
        self._body = response_body
        self.body_reads = 0

    async def body(self) -> bytes:
        self.body_reads += 1
        return self._body


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
        await asyncio.sleep(0)


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

    async def connect_over_cdp(self, endpoint: str, **kwargs: object) -> FakeBrowser:
        assert endpoint == "http://127.0.0.1:9222"
        assert kwargs == {"timeout": 1234, "is_local": True, "no_defaults": True}
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
    return FakeBrowser(
        [FakeContext([target, *(FakePage(url) for url in REQUIRED_SAME_CONTEXT_PAGES)])]
    )


@pytest.mark.parametrize(
    "raw,code",
    [
        (b'{"value":1,"value":2}', "DUPLICATE_JSON_KEY"),
        (b'{"value":NaN}', "NON_FINITE_JSON_NUMBER"),
        (b"{not-json}", "INVALID_JSON_RESPONSE"),
    ],
)
def test_strict_json_rejects_ambiguous_or_invalid_values(raw: bytes, code: str) -> None:
    with pytest.raises(ShapeStopped, match=code):
        _strict_json(raw)


def test_array_lengths_sanitize_dynamic_keys_and_keep_no_values() -> None:
    value = {"result": {"123456": {"rows": [{"nested": [1, 2]}]}}}

    assert _array_path_lengths(value) == [
        {"path": "result.{key}.rows", "length": 1},
        {"path": "result.{key}.rows[].nested", "length": 2},
    ]


def test_only_exact_candidate_contract_can_be_selected() -> None:
    path = CANDIDATE_RESPONSE_PATHS[0]
    exact = FakeResponse(path)
    assert _candidate_path(cast(Any, exact)) == path

    rejected = [
        FakeResponse("/unknown"),
        FakeResponse(path, host="notpinduoduo.com"),
        FakeResponse(path, method="GET"),
        FakeResponse(path, status=204),
        FakeResponse(path, content_type="text/html"),
    ]
    assert all(_candidate_path(cast(Any, response)) is None for response in rejected)
    assert all(response.body_reads == 0 for response in rejected)
    assert all(response.request.post_data_json_reads == 0 for response in rejected)


def test_candidate_probe_outputs_only_shapes_types_and_array_lengths() -> None:
    path = CANDIDATE_RESPONSE_PATHS[0]
    response = FakeResponse(
        path,
        request_payload={"page": 1, "date": "2026-09-08", "goodsId": "request-secret"},
        response_body=(
            b'{"success":true,"result":{"rows":['
            b'{"goodsId":"response-id","goodsName":"private-name","amount":12.34}'
            b'],"dates":["2026-09-08"]}}'
        ),
    )
    counts = SafetyCounts()

    result = asyncio.run(_probe_candidate(cast(Any, response), path, counts))

    assert result["path"] == path
    assert result["request_type"] == "object"
    assert result["response_type"] == "object"
    assert result["response_array_lengths"] == [
        {"path": "result.dates", "length": 1},
        {"path": "result.rows", "length": 1},
    ]
    serialized = json.dumps(result)
    for secret in ("request-secret", "response-id", "private-name", "2026-09-08", "12.34"):
        assert secret not in serialized
    assert response.request.post_data_json_reads == 1
    assert response.body_reads == 1
    assert counts.scalar_value_outputs == 0


def test_content_length_gate_runs_before_response_body() -> None:
    response = FakeResponse(
        CANDIDATE_RESPONSE_PATHS[0],
        content_length=str(MAX_RESPONSE_BYTES + 1),
    )

    with pytest.raises(ShapeStopped, match="RESPONSE_TOO_LARGE"):
        asyncio.run(
            _probe_candidate(
                cast(Any, response),
                CANDIDATE_RESPONSE_PATHS[0],
                SafetyCounts(),
            )
        )

    assert response.request.post_data_json_reads == 0
    assert response.body_reads == 0


def test_shape_capture_reloads_once_and_never_reads_unknown_response() -> None:
    candidate = FakeResponse(CANDIDATE_RESPONSE_PATHS[0])
    unknown = FakeResponse("/unknown", response_body=b'{"secret":"must-not-read"}')
    target = FakePage(TARGET_PAGE_URL, responses=[unknown, candidate])
    counts = SafetyCounts()

    results = asyncio.run(
        _capture_shapes(
            cast(Any, target),
            collection_timeout_ms=9000,
            observe_seconds=0,
            counts=counts,
        )
    )

    assert target.reload_calls == 1
    assert target.listeners == []
    assert unknown.request.post_data_json_reads == 0
    assert unknown.body_reads == 0
    assert candidate.request.post_data_json_reads == 1
    assert candidate.body_reads == 1
    by_path = {str(item["path"]): item for item in results}
    assert by_path[CANDIDATE_RESPONSE_PATHS[0]]["observation_count"] == 1
    assert all(by_path[path]["observation_count"] == 0 for path in CANDIDATE_RESPONSE_PATHS[1:])
    assert counts.response_body_reads == 1
    assert counts.listener_removals == 1


def test_observation_aggregation_counts_shape_variants_without_scalars() -> None:
    path = CANDIDATE_RESPONSE_PATHS[0]
    observation = {
        "path": path,
        "request_type": "object",
        "request_shape": [{"path": "page", "type": "number"}],
        "request_array_lengths": [],
        "response_type": "object",
        "response_shape": [{"path": "success", "type": "boolean"}],
        "response_array_lengths": [],
    }

    results = _aggregate_observations([observation, observation])

    first = results[0]
    assert first["path"] == path
    assert first["observation_count"] == 2
    assert cast(list[dict[str, object]], first["variants"])[0]["observations"] == 2


def test_shape_inventory_stops_only_playwright_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    target = FakePage(TARGET_PAGE_URL)
    browser = browser_with_required_pages(target)
    manager = FakeManager(browser)
    monkeypatch.setattr(
        shape_module,
        "async_playwright",
        lambda: FakePlaywrightStarter(manager),
    )

    result = asyncio.run(
        shape_inventory(
            ShapeRuntime(
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
    safety = cast(dict[str, int], result["safety_counts"])
    assert safety["playwright_stop_attempts"] == 1
    assert safety["browser_close_actions"] == 0
    assert safety["page_close_actions"] == 0


def test_main_without_confirmation_stops_before_config_or_browser(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden_load_config(path: object) -> None:
        del path
        raise AssertionError("must stop before config and browser")

    monkeypatch.setattr(shape_module, "load_config", forbidden_load_config)
    monkeypatch.setattr(
        "sys.argv",
        [
            "read_only_product_business_shape.py",
            "--config",
            "unused.toml",
            "--connection-id",
            "conn_current",
        ],
    )

    assert shape_module.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_code"] == "READ_ONLY_CONFIRMATION_REQUIRED"
    assert payload["safety_counts"]["response_body_reads"] == 0
