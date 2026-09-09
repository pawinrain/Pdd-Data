from __future__ import annotations

import hashlib
import http.client
import re
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal
from urllib.parse import urlsplit

from pdd_data_mcp.errors import CollectionRejected

PDD_DYNAMIC_FONT_HOST = "pfile.pddpic.com"
PDD_DYNAMIC_FONT_PATH_PREFIX = "/webspider-sdk-api/"
MAX_DYNAMIC_FONT_BYTES = 131_072

_FONT_PATH_PATTERN = re.compile(r"^/webspider-sdk-api/[0-9a-f]{32}-[0-9a-f]{32}\.ttf$")
_FONT_CONTENT_TYPE = "application/x-font-ttf"
_COUNT_PATTERN = re.compile(r"^(?:0|[1-9][0-9]*)$")
_DECIMAL_PATTERN = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?$")


@dataclass(frozen=True)
class _AuditedDigitFontProfile:
    version: str
    codepoint_to_digit: Mapping[int, str]


def _profile(version: str, values: Mapping[int, str]) -> _AuditedDigitFontProfile:
    if len(values) != 10 or set(values.values()) != set("0123456789"):
        raise AssertionError("audited font profile must contain a digit bijection")
    return _AuditedDigitFontProfile(version, MappingProxyType(dict(values)))


_AUDITED_PROFILES: Mapping[str, _AuditedDigitFontProfile] = MappingProxyType(
    {
        "a7aac68a42956807c2f5e4c52e29694515b9441a118adc3523d62ef46bb8d448": _profile(
            "pdd-digit-font-a7aac68a-v1",
            {
                0xEC2B: "0",
                0xE809: "1",
                0xE591: "2",
                0xE8A6: "3",
                0xEE90: "4",
                0xEC80: "5",
                0xE97E: "6",
                0xEEF8: "7",
                0xEE12: "8",
                0xE633: "9",
            },
        ),
        "3861d3322b1267200735d9f5cb48bc6d833a75ee3eb468b54f89abe7e0038eb7": _profile(
            "pdd-digit-font-3861d332-v1",
            {
                0xE6EB: "0",
                0xE378: "1",
                0xE551: "2",
                0xE3C1: "3",
                0xE6EA: "4",
                0xEBF4: "5",
                0xE9E5: "6",
                0xE6B6: "7",
                0xEF35: "8",
                0xEFBA: "9",
            },
        ),
    }
)


def _reject(code: str) -> CollectionRejected:
    return CollectionRejected("UNIT_UNVERIFIED", code)


def validate_dynamic_font_url(url: str) -> str:
    """Return the safe path from a font URL discovered on the current page."""

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise _reject("DYNAMIC_FONT_URL_INVALID") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != PDD_DYNAMIC_FONT_HOST
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or _FONT_PATH_PATTERN.fullmatch(parsed.path) is None
    ):
        raise _reject("DYNAMIC_FONT_URL_NOT_ALLOWED")
    return parsed.path


@dataclass(frozen=True)
class BoundDynamicDigitFont:
    """One freshly fetched font with a reviewed SHA-bound digit mapping."""

    source_path: str
    sha256: str
    profile_version: str
    codepoint_to_digit: Mapping[int, str]

    def decode(self, value: str, *, kind: Literal["COUNT", "DECIMAL"]) -> str:
        _assert_audited_decoder(self)
        if not isinstance(value, str) or not value or len(value) > 128:
            raise _reject("DYNAMIC_FONT_SOURCE_VALUE_NOT_BOUNDED_STRING")
        output: list[str] = []
        for character in value:
            if character == ".":
                output.append(character)
                continue
            digit = self.codepoint_to_digit.get(ord(character))
            if digit is None:
                raise _reject("DYNAMIC_FONT_CODEPOINT_UNMAPPED")
            output.append(digit)
        decoded = "".join(output)
        pattern = _COUNT_PATTERN if kind == "COUNT" else _DECIMAL_PATTERN
        if pattern.fullmatch(decoded) is None:
            raise _reject(f"DYNAMIC_FONT_DECODED_{kind}_GRAMMAR_INVALID")
        return decoded


def fetch_dynamic_digit_font(
    font_url: str,
    *,
    timeout_seconds: float = 10.0,
    _connection_factory: Callable[..., http.client.HTTPSConnection] = (http.client.HTTPSConnection),
) -> BoundDynamicDigitFont:
    """Freshly download the page's current font and select its reviewed map."""

    source_path = validate_dynamic_font_url(font_url)
    if not 0.1 <= timeout_seconds <= 30.0:
        raise ValueError("font fetch timeout must be between 0.1 and 30 seconds")
    connection = _connection_factory(
        PDD_DYNAMIC_FONT_HOST,
        port=443,
        timeout=timeout_seconds,
        context=ssl.create_default_context(),
    )
    try:
        connection.request(
            "GET",
            source_path,
            headers={
                "Accept": _FONT_CONTENT_TYPE,
                "Accept-Encoding": "identity",
                "Cache-Control": "no-cache",
                "Connection": "close",
                "Pragma": "no-cache",
            },
        )
        response = connection.getresponse()
        body = response.read(MAX_DYNAMIC_FONT_BYTES + 1)
        if response.status != 200:
            raise _reject("DYNAMIC_FONT_HTTP_STATUS_UNVERIFIED")
        if response.getheader("Content-Type", "").strip().casefold() != _FONT_CONTENT_TYPE:
            raise _reject("DYNAMIC_FONT_CONTENT_TYPE_UNVERIFIED")
        encoding = response.getheader("Content-Encoding")
        if encoding is not None and encoding.strip().casefold() not in {"", "identity"}:
            raise _reject("DYNAMIC_FONT_CONTENT_ENCODING_UNVERIFIED")
        length_text = response.getheader("Content-Length")
        if length_text is None:
            raise _reject("DYNAMIC_FONT_CONTENT_LENGTH_MISSING")
        try:
            declared_size = int(length_text)
        except ValueError as exc:
            raise _reject("DYNAMIC_FONT_CONTENT_LENGTH_INVALID") from exc
        if not 1 <= declared_size <= MAX_DYNAMIC_FONT_BYTES:
            raise _reject("DYNAMIC_FONT_CONTENT_LENGTH_UNSAFE")
        if len(body) != declared_size:
            raise _reject("DYNAMIC_FONT_CONTENT_LENGTH_MISMATCH")
        if body[:4] != b"\x00\x01\x00\x00":
            raise _reject("DYNAMIC_FONT_SFNT_SIGNATURE_MISMATCH")
    except (OSError, http.client.HTTPException) as exc:
        raise _reject("DYNAMIC_FONT_FETCH_FAILED") from exc
    finally:
        connection.close()

    digest = hashlib.sha256(body).hexdigest()
    profile = _AUDITED_PROFILES.get(digest)
    if profile is None:
        raise _reject("DYNAMIC_FONT_PROFILE_UNKNOWN")
    return BoundDynamicDigitFont(
        source_path=source_path,
        sha256=digest,
        profile_version=profile.version,
        codepoint_to_digit=profile.codepoint_to_digit,
    )


def _assert_audited_decoder(decoder: BoundDynamicDigitFont) -> None:
    profile = _AUDITED_PROFILES.get(decoder.sha256)
    if (
        profile is None
        or decoder.profile_version != profile.version
        or dict(decoder.codepoint_to_digit) != dict(profile.codepoint_to_digit)
        or _FONT_PATH_PATTERN.fullmatch(decoder.source_path) is None
    ):
        raise _reject("DYNAMIC_FONT_DECODER_NOT_AUDITED")


__all__ = [
    "MAX_DYNAMIC_FONT_BYTES",
    "PDD_DYNAMIC_FONT_HOST",
    "PDD_DYNAMIC_FONT_PATH_PREFIX",
    "BoundDynamicDigitFont",
    "fetch_dynamic_digit_font",
    "validate_dynamic_font_url",
]
