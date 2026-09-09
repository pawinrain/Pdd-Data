from __future__ import annotations

import hashlib
from types import MappingProxyType
from typing import Any

import pytest

from pdd_data_mcp.browser import dynamic_digit_font as font_module
from pdd_data_mcp.browser.dynamic_digit_font import (
    MAX_DYNAMIC_FONT_BYTES,
    fetch_dynamic_digit_font,
    validate_dynamic_font_url,
)
from pdd_data_mcp.errors import CollectionRejected

_TEST_MAPPING = {
    0xE101: "0",
    0xE102: "1",
    0xE103: "2",
    0xE104: "3",
    0xE105: "4",
    0xE106: "5",
    0xE107: "6",
    0xE108: "7",
    0xE109: "8",
    0xE10A: "9",
}
_TEST_FONT_URL = (
    "https://pfile.pddpic.com/webspider-sdk-api/"
    "11111111111111111111111111111111-22222222222222222222222222222222.ttf"
)
_TEST_FONT = b"\x00\x01\x00\x00test-font-body"


def _trust_test_font(monkeypatch: pytest.MonkeyPatch, body: bytes = _TEST_FONT) -> None:
    profile = font_module._profile("test-audited-sha-v1", MappingProxyType(_TEST_MAPPING))
    monkeypatch.setattr(
        font_module,
        "_AUDITED_PROFILES",
        MappingProxyType({hashlib.sha256(body).hexdigest(): profile}),
    )


def _fake_connection(
    body: bytes,
    calls: list[tuple[str, str, dict[str, str]]],
    *,
    status: int = 200,
    headers: dict[str, str | None] | None = None,
) -> type[Any]:
    response_headers: dict[str, str | None] = {
        "Content-Type": "application/x-font-ttf",
        "Content-Length": str(len(body)),
        **(headers or {}),
    }

    class Response:
        def __init__(self) -> None:
            self.status = status

        def getheader(self, name: str, default: str | None = None) -> str | None:
            return response_headers.get(name, default)

        def read(self, amount: int) -> bytes:
            assert amount == MAX_DYNAMIC_FONT_BYTES + 1
            return body

    class Connection:
        def __init__(self, host: str, **kwargs: Any) -> None:
            assert host == "pfile.pddpic.com"
            assert kwargs["port"] == 443
            assert kwargs["timeout"] == 3.0

        def request(self, method: str, path: str, *, headers: dict[str, str]) -> None:
            calls.append((method, path, headers))

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            pass

    return Connection


def test_dynamic_url_is_fetched_again_on_every_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trust_test_font(monkeypatch)
    calls: list[tuple[str, str, dict[str, str]]] = []

    bound = fetch_dynamic_digit_font(
        _TEST_FONT_URL,
        timeout_seconds=3.0,
        _connection_factory=_fake_connection(_TEST_FONT, calls),
    )
    fetch_dynamic_digit_font(
        _TEST_FONT_URL,
        timeout_seconds=3.0,
        _connection_factory=_fake_connection(_TEST_FONT, calls),
    )

    assert bound.sha256 == hashlib.sha256(_TEST_FONT).hexdigest()
    assert bound.profile_version == "test-audited-sha-v1"
    assert bound.decode("\ue102\ue101\ue10a", kind="COUNT") == "109"
    assert bound.decode("\ue102\ue103.\ue104\ue105", kind="DECIMAL") == "12.34"
    assert len(calls) == 2
    assert calls[0] == calls[1]
    method, path, headers = calls[0]
    assert method == "GET"
    assert path == validate_dynamic_font_url(_TEST_FONT_URL)
    assert headers["Cache-Control"] == "no-cache"
    assert headers["Accept-Encoding"] == "identity"


@pytest.mark.parametrize(
    "url",
    [
        _TEST_FONT_URL.replace("https://", "http://", 1),
        _TEST_FONT_URL.replace("pfile.pddpic.com", "pfile.pddpic.com.evil.test", 1),
        _TEST_FONT_URL.replace("pfile.pddpic.com", "user@pfile.pddpic.com", 1),
        _TEST_FONT_URL + "?token=secret",
        "https://pfile.pddpic.com/webspider-sdk-api/not-a-font-id.ttf",
    ],
)
def test_font_url_policy_rejects_other_locations(url: str) -> None:
    with pytest.raises(CollectionRejected) as caught:
        validate_dynamic_font_url(url)
    assert caught.value.error_code == "DYNAMIC_FONT_URL_NOT_ALLOWED"


def test_new_unreviewed_font_hash_fails_closed() -> None:
    with pytest.raises(CollectionRejected) as caught:
        fetch_dynamic_digit_font(
            _TEST_FONT_URL,
            timeout_seconds=3.0,
            _connection_factory=_fake_connection(_TEST_FONT, []),
        )
    assert caught.value.error_code == "DYNAMIC_FONT_PROFILE_UNKNOWN"


@pytest.mark.parametrize(
    ("status", "headers", "body", "error_code"),
    [
        (302, {}, _TEST_FONT, "HTTP_STATUS"),
        (200, {"Content-Type": "application/octet-stream"}, _TEST_FONT, "CONTENT_TYPE"),
        (200, {"Content-Encoding": "gzip"}, _TEST_FONT, "CONTENT_ENCODING"),
        (200, {"Content-Length": "1"}, _TEST_FONT, "CONTENT_LENGTH_MISMATCH"),
        (200, {"Content-Length": None}, _TEST_FONT, "CONTENT_LENGTH_MISSING"),
        (200, {}, b"not-a-ttf", "SFNT_SIGNATURE_MISMATCH"),
    ],
)
def test_font_response_contract_fails_closed(
    status: int,
    headers: dict[str, str | None],
    body: bytes,
    error_code: str,
) -> None:
    with pytest.raises(CollectionRejected) as caught:
        fetch_dynamic_digit_font(
            _TEST_FONT_URL,
            timeout_seconds=3.0,
            _connection_factory=_fake_connection(body, [], status=status, headers=headers),
        )
    assert error_code in caught.value.error_code


def test_decode_rejects_unknown_character_and_invalid_grammar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trust_test_font(monkeypatch)
    bound = fetch_dynamic_digit_font(
        _TEST_FONT_URL,
        timeout_seconds=3.0,
        _connection_factory=_fake_connection(_TEST_FONT, []),
    )

    with pytest.raises(CollectionRejected, match="DYNAMIC_FONT_CODEPOINT_UNMAPPED"):
        bound.decode("\ue777", kind="COUNT")
    with pytest.raises(CollectionRejected, match="DYNAMIC_FONT_DECODED_COUNT_GRAMMAR_INVALID"):
        bound.decode("\ue101.\ue102", kind="COUNT")
