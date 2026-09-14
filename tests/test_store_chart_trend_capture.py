"""Tests for MMS home realtime trend capture (_capture_store_chart_trend).

Regression: the realtime panel lazy-initializes only when its tab is visible
(visibility/rAF gating). With the home tab in the background, clicking refresh
fired no homePageOverView XHR and chart_trend stayed null. The capture now
focuses the tab first and retries refresh once.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from pdd_data_mcp.browser.core import CoreDataCdpCollector
from pdd_data_mcp.config import CollectionSettings, ConnectionSettings

REFRESH_SELECTOR = '[data-tracking-click-viewid="el_refresh_button"]'
TREND_URL = "https://mms.pinduoduo.com/sydney/api/mallCoreData/homePageOverView"


def _trend_body() -> bytes:
    return json.dumps(
        {
            "success": True,
            "result": {
                "monthTrend": [
                    {
                        "statDate": "2026-09-12",
                        "curPayOrdrAmt": 1000,
                        "curPayOrdrCnt": 1,
                        "guvOned": 2,
                        "gpvOned": 3,
                    },
                    {
                        "statDate": "2026-09-13",
                        "curPayOrdrAmt": 2000,
                        "curPayOrdrCnt": 2,
                        "guvOned": 4,
                        "gpvOned": 5,
                    },
                ]
            },
        }
    ).encode("utf-8")


class FakeRequest:
    method = "POST"


class FakeTrendResponse:
    def __init__(self, body: bytes) -> None:
        self.url = TREND_URL
        self.status = 200
        self.request = FakeRequest()
        self._body = body

    async def body(self) -> bytes:
        return self._body


class FakeRefreshLocator:
    def __init__(self, page: FakeTrendPage) -> None:
        self.page = page
        # The collector always uses `.first`.
        self.first = self

    async def count(self) -> int:
        return 1

    async def click(self, **kwargs: Any) -> None:
        del kwargs
        await self.page.handle_refresh_click()


class FakeTrendPage:
    """Page fake that dispatches homePageOverView only on refresh clicks.

    response_on_click: 1 -> first click responds, 2 -> only the second click.
    """

    def __init__(self, response_on_click: int, *, raise_on_front: bool = False) -> None:
        self.response_on_click = response_on_click
        self.raise_on_front = raise_on_front
        self.listeners: list[Any] = []
        self.clicks = 0
        self.bring_to_front_calls = 0
        self.refresh = FakeRefreshLocator(self)

    def locator(self, selector: str) -> FakeRefreshLocator:
        assert selector == REFRESH_SELECTOR
        return self.refresh

    def on(self, event: str, callback: Any) -> None:
        assert event == "response"
        self.listeners.append(callback)

    def remove_listener(self, event: str, callback: Any) -> None:
        assert event == "response"
        self.listeners.remove(callback)

    async def bring_to_front(self) -> None:
        self.bring_to_front_calls += 1
        if self.raise_on_front:
            raise RuntimeError("focus refused")

    async def wait_for_load_state(self, **kwargs: Any) -> None:
        del kwargs

    async def evaluate(self, script: str) -> None:
        del script

    async def handle_refresh_click(self) -> None:
        self.clicks += 1
        if self.clicks == self.response_on_click:
            response = FakeTrendResponse(_trend_body())
            for callback in list(self.listeners):
                callback(response)
            # Let the listener's body-reading task run.
            await asyncio.sleep(0)


def _collector(tmp_path: Path) -> CoreDataCdpCollector:
    return CoreDataCdpCollector(
        connection=ConnectionSettings(connection_id="conn_1", store_id="st_1"),
        collection=CollectionSettings(collection_timeout_ms=12_000),
        runtime_root=tmp_path,
    )


async def _trigger() -> None:
    # Reload never fires the trend XHR; only the refresh click does.
    return None


async def _run_with_virtual_clock(
    tmp_path: Path, page: FakeTrendPage, monkeypatch: pytest.MonkeyPatch
) -> Any:
    """Run capture with time advanced virtually so retry waits stay fast."""
    loop = asyncio.get_running_loop()
    real_sleep = asyncio.sleep
    clock = {"t": loop.time()}
    monkeypatch.setattr(loop, "time", lambda: clock["t"])

    async def fake_sleep(seconds: float) -> None:
        clock["t"] += seconds
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    collector = _collector(tmp_path)
    return await collector._capture_store_chart_trend(page, _trigger)  # type: ignore[arg-type]


def test_trend_captured_on_first_refresh_click_with_tab_focus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = FakeTrendPage(response_on_click=1)
    trend = asyncio.run(_run_with_virtual_clock(tmp_path, page, monkeypatch))

    assert trend is not None
    assert len(trend.month30) == 2
    assert page.bring_to_front_calls == 1
    assert page.clicks == 1


def test_trend_retries_refresh_click_when_first_fires_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = FakeTrendPage(response_on_click=2)
    trend = asyncio.run(_run_with_virtual_clock(tmp_path, page, monkeypatch))

    assert trend is not None
    assert len(trend.month30) == 2
    assert page.clicks == 2
    assert page.bring_to_front_calls == 1


def test_trend_capture_survives_bring_to_front_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = FakeTrendPage(response_on_click=1, raise_on_front=True)
    trend = asyncio.run(_run_with_virtual_clock(tmp_path, page, monkeypatch))

    assert trend is not None
    assert page.clicks == 1
