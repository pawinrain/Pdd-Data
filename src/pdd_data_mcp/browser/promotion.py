from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from datetime import datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from playwright.async_api import Page, Response

from pdd_data_mcp.browser.cdp import CdpConnector, read_locator_value, select_target_page
from pdd_data_mcp.browser.parsing import (
    ParsedDomMoney,
    ParsedPromotionResponse,
    decode_json_object,
    parse_dom_money,
    parse_promotion_response,
    verify_store_identity,
)
from pdd_data_mcp.config import CollectionSettings, ConnectionSettings
from pdd_data_mcp.contracts.models import (
    Coverage,
    CoverageStatus,
    DatasetType,
    IdentityEvidence,
    MetricValue,
    MetricWindow,
    PromotionOverviewPayload,
    Quality,
    Scope,
    SnapshotDraft,
    WindowKind,
)
from pdd_data_mcp.errors import CollectionRejected
from pdd_data_mcp.security import safe_child
from pdd_data_mcp.utils import canonical_json, scope_key, utc_now

_PATH_ID = re.compile(
    r"(?:\d{4,}|[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}|(?=[A-Za-z0-9_-]{16,}$)(?=.*\d)[A-Za-z0-9_-]+)"
)
_SAFE_SHAPE_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


def sanitize_discovery_path(path: str) -> str:
    """Keep route shape while removing likely object/account identifiers."""
    parts = []
    for part in path.split("/"):
        if "%" in part or _PATH_ID.fullmatch(part):
            parts.append("{id}")
        else:
            parts.append(part[:80])
    return "/".join(parts)[:512]


def sanitized_json_shape(
    value: object, *, max_depth: int = 8, max_entries: int = 400
) -> list[dict[str, str]]:
    """Describe JSON structure without retaining scalar values or dynamic object keys."""
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(path: str, kind: str) -> None:
        signature = (path, kind)
        if signature not in seen and len(result) < max_entries:
            seen.add(signature)
            result.append({"path": path or "$", "type": kind})

    def visit(item: object, path: str, depth: int) -> None:
        if len(result) >= max_entries:
            return
        if isinstance(item, dict):
            add(path, "object")
            if depth >= max_depth:
                return
            for key in sorted(item):
                safe_key = key if _SAFE_SHAPE_KEY.fullmatch(key) else "{key}"
                child = f"{path}.{safe_key}" if path else safe_key
                visit(item[key], child, depth + 1)
        elif isinstance(item, list):
            add(path, "array")
            if item and depth < max_depth:
                visit(item[0], f"{path}[]", depth + 1)
        elif item is None:
            add(path, "null")
        elif isinstance(item, bool):
            add(path, "boolean")
        elif isinstance(item, int | float | Decimal):
            add(path, "number")
        elif isinstance(item, str):
            add(path, "string")
        else:
            add(path, "other")

    visit(value, "", 0)
    return result


class PromotionOverviewCdpCollector:
    """Bounded, read-only TODAY ad-spend collector for one configured connection."""

    def __init__(
        self,
        *,
        connection: ConnectionSettings,
        collection: CollectionSettings,
        runtime_root: Path,
        connector: CdpConnector | None = None,
    ) -> None:
        self.connection = connection
        self.collection = collection
        self.runtime_root = runtime_root.resolve(strict=False)
        self.connector = connector or CdpConnector()

    async def collect(
        self,
        *,
        store_id: str,
        dataset_type: DatasetType,
        scope: Scope,
        limit: int,
        batch_id: str | None = None,
    ) -> SnapshotDraft:
        requested_at = utc_now()
        if dataset_type is not DatasetType.PROMOTION_OVERVIEW:
            raise CollectionRejected("DATASET_UNVERIFIED", "REAL_DATASET_NOT_ADAPTED")
        if scope.kind is not WindowKind.TODAY:
            raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "ONLY_TODAY_IS_ADAPTED")
        if scope.timezone != "Asia/Shanghai":
            raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "REAL_TODAY_REQUIRES_ASIA_SHANGHAI")
        if store_id != self.connection.store_id:
            raise CollectionRejected("IDENTITY_MISMATCH", "INTERNAL_STORE_ID_MISMATCH")
        zone = ZoneInfo(scope.timezone)
        if utc_now().astimezone(zone).date() != scope.business_date:
            raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "TODAY_BUSINESS_DATE_IS_NOT_CURRENT")
        self._enforce_min_interval()
        started_at = utc_now()
        session = await self.connector.connect(self.connection, self.collection.connect_timeout_ms)
        try:
            page = await select_target_page(
                session.browser, self.connection, self.connection.promotion_adapter
            )
            if not self.connection.promotion_adapter.verified:
                await self._discover(page)
            try:
                network, captured_at = await self._capture_verified_response(page, scope)
            except CollectionRejected as exc:
                if (
                    exc.status != "CAPTURE_TIMEOUT"
                    or not self.connection.promotion_adapter.dom_fallback_enabled
                ):
                    raise
                dom_store_id, dom_money = await self._read_dom(page, scope, require_store_id=True)
                assert dom_store_id is not None
                captured_at = utc_now()
                return self._build_draft(
                    store_id=store_id,
                    batch_id=batch_id,
                    dataset_type=dataset_type,
                    scope=scope,
                    limit=limit,
                    requested_at=requested_at,
                    started_at=started_at,
                    captured_at=captured_at,
                    ad_spend_cents=dom_money.displayed_cents,
                    precision=dom_money.precision,
                    capture_method="DOM",
                    observed_platform_store_id=dom_store_id,
                    source_updated_at=None,
                )
            _, dom_money = await self._read_dom(page, scope, require_store_id=False)
            if not dom_money.matches(network.ad_spend_cents):
                raise CollectionRejected("DATA_MISMATCH", "NETWORK_DOM_AD_SPEND_MISMATCH")
            return self._build_draft(
                store_id=store_id,
                batch_id=batch_id,
                dataset_type=dataset_type,
                scope=scope,
                limit=limit,
                requested_at=requested_at,
                started_at=started_at,
                captured_at=captured_at,
                ad_spend_cents=network.ad_spend_cents,
                precision="EXACT",
                capture_method="NETWORK_RESPONSE",
                observed_platform_store_id=network.platform_store_id,
                source_updated_at=network.source_updated_at,
            )
        finally:
            await session.disconnect()

    def _build_draft(
        self,
        *,
        store_id: str,
        batch_id: str | None,
        dataset_type: DatasetType,
        scope: Scope,
        limit: int,
        requested_at: datetime,
        started_at: datetime,
        captured_at: datetime,
        ad_spend_cents: int,
        precision: Literal["EXACT", "APPROXIMATE"],
        capture_method: Literal["NETWORK_RESPONSE", "DOM"],
        observed_platform_store_id: str,
        source_updated_at: datetime | None,
    ) -> SnapshotDraft:
        adapter = self.connection.promotion_adapter
        zone = ZoneInfo(scope.timezone)
        local_midnight = datetime.combine(scope.business_date, time.min, tzinfo=zone)
        payload = PromotionOverviewPayload(
            metrics={
                "ad_spend": MetricValue(
                    value=ad_spend_cents,
                    unit="CNY_CENT",
                    observed_at=captured_at,
                    capture_method=capture_method,
                    precision=precision,
                ),
                "ad_gmv": None,
                "roi": None,
            }
        )
        return SnapshotDraft(
            batch_id=batch_id,
            store_id=store_id,
            dataset_type=dataset_type,
            scope=scope,
            scope_key=scope_key(scope),
            requested_at=requested_at,
            capture_started_at=started_at,
            capture_finished_at=utc_now(),
            captured_at=captured_at,
            metric_window=MetricWindow(
                kind=WindowKind.TODAY,
                timezone=scope.timezone,
                start=local_midnight,
                end=captured_at,
                window_complete=False,
                source_finalized=False,
            ),
            source_updated_at=source_updated_at,
            source="PDD_BROWSER_CDP",
            capture_method=capture_method,
            parser_version=adapter.parser_version,
            identity_evidence=IdentityEvidence(
                expected_platform_store_id=(
                    self.connection.expected_platform_store_id
                    or f"sha256:{self.connection.expected_platform_store_id_sha256}"
                ),
                observed_platform_store_id=observed_platform_store_id,
                evidence_source=capture_method,
                independent_verification_method=adapter.identity_verification_method,
                independent_verification_reference=(
                    adapter.identity_verification_reference or None
                ),
                response_field_path=(
                    (
                        adapter.identity_platform_store_id_path
                        if adapter.identity_response_path
                        else adapter.platform_store_id_path
                    )
                    if capture_method == "NETWORK_RESPONSE"
                    else None
                ),
                dom_selector=adapter.dom_store_id_selector or None,
                dom_attribute=adapter.dom_store_id_attribute or None,
            ),
            field_sources={"metrics.ad_spend": capture_method},
            payload=payload.model_dump(mode="json"),
            missing_fields=["ad_gmv", "roi"],
            quality=Quality(
                status="VALID",
                identity="MATCHED",
                coverage=CoverageStatus.COMPLETE,
                dom_check="MATCHED",
            ),
            coverage=Coverage(
                total_observed=1,
                captured=1,
                limit=limit,
                pages_read=1,
                coverage=CoverageStatus.COMPLETE,
                truncated=False,
            ),
        )

    def _enforce_min_interval(self) -> None:
        directory = safe_child(self.runtime_root, "rate_limits")
        directory.mkdir(parents=True, exist_ok=True)
        path = safe_child(directory, f"{self.connection.connection_id}.json")
        now = utc_now()
        if path.exists():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                previous = datetime.fromisoformat(str(value["started_at"]))
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                raise CollectionRejected("PLATFORM_ERROR", "INVALID_RATE_LIMIT_STATE") from exc
            elapsed = (now - previous).total_seconds()
            if elapsed < 0 or elapsed < self.collection.min_interval_seconds:
                raise CollectionRejected("BUSY", "MIN_COLLECTION_INTERVAL_NOT_ELAPSED")
        temporary = safe_child(directory, f"tmp_{uuid.uuid4().hex}.json")
        try:
            with temporary.open("xb") as handle:
                handle.write(canonical_json({"started_at": now.isoformat()}) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            raise CollectionRejected("PLATFORM_ERROR", "RATE_LIMIT_STATE_WRITE_FAILED") from exc
        finally:
            temporary.unlink(missing_ok=True)

    async def _discover(self, page: Page) -> None:
        discovery = self.connection.discovery
        if not discovery.enabled:
            raise CollectionRejected("ADAPTER_UNVERIFIED", "PROMOTION_ADAPTER_NOT_VERIFIED")
        metadata: list[dict[str, object]] = []
        seen: set[tuple[object, ...]] = set()
        candidate_shapes: dict[str, dict[str, object]] = {}
        probe_tasks: set[asyncio.Task[None]] = set()
        probed_paths: set[str] = set()
        active = True

        async def probe_candidate(response: Response, path: str) -> None:
            try:
                content_length = response.headers.get("content-length")
                if (
                    content_length
                    and int(content_length) > self.collection.max_browser_response_bytes
                ):
                    candidate_shapes[path] = {"status": "RESPONSE_TOO_LARGE", "shape": []}
                    return
                raw = await response.body()
                if len(raw) > self.collection.max_browser_response_bytes:
                    candidate_shapes[path] = {"status": "RESPONSE_TOO_LARGE", "shape": []}
                    return
                payload = decode_json_object(raw)
                candidate_shapes[path] = {
                    "status": "PARSED_IN_MEMORY",
                    "shape": sanitized_json_shape(payload),
                }
            except (CollectionRejected, OSError, ValueError):
                candidate_shapes[path] = {"status": "PARSE_FAILED", "shape": []}

        def on_response(response: Response) -> None:
            if not active:
                return
            parsed = urlsplit(response.url)
            if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
                return
            content_type = response.headers.get("content-type", "").partition(";")[0].casefold()
            item: dict[str, object] = {
                "host": parsed.hostname.casefold(),
                "path": sanitize_discovery_path(parsed.path),
                "method": response.request.method,
                "status": response.status,
                "content_type": content_type,
            }
            signature = tuple(item.values())
            if signature not in seen and len(metadata) < discovery.max_metadata_entries:
                seen.add(signature)
                metadata.append(item)
            if (
                discovery.candidate_body_probe_enabled
                and parsed.hostname.casefold() == discovery.candidate_response_host.casefold()
                and parsed.path in discovery.candidate_response_paths
                and response.request.method == discovery.candidate_response_method
                and response.status == 200
                and content_type == "application/json"
                and parsed.path not in probed_paths
                and len(probe_tasks) < self.collection.max_inflight_responses
            ):
                probed_paths.add(parsed.path)
                task = asyncio.create_task(probe_candidate(response, parsed.path))
                probe_tasks.add(task)
                task.add_done_callback(probe_tasks.discard)

        page.on("response", on_response)
        try:
            if self.connection.promotion_adapter.trigger == "RELOAD":
                await page.reload(
                    wait_until="domcontentloaded",
                    timeout=self.collection.collection_timeout_ms,
                )
            await asyncio.sleep(discovery.observe_seconds)
        finally:
            active = False
            page.remove_listener("response", on_response)
            if probe_tasks:
                _, pending = await asyncio.wait(probe_tasks, timeout=2)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        report_name = self._write_discovery_report(metadata, candidate_shapes)
        raise CollectionRejected(
            "ADAPTER_UNVERIFIED",
            "DISCOVERY_METADATA_RECORDED",
            f"DISCOVERY_ENTRIES:{len(metadata)}",
            f"DISCOVERY_REPORT:{report_name}",
        )

    def _write_discovery_report(
        self,
        metadata: list[dict[str, object]],
        candidate_shapes: dict[str, dict[str, object]],
    ) -> str:
        directory = safe_child(self.runtime_root, "discovery", self.connection.connection_id)
        directory.mkdir(parents=True, exist_ok=True)
        now = utc_now()
        name = f"{now:%Y%m%dT%H%M%S}_{uuid.uuid4().hex}.json"
        destination = safe_child(directory, name)
        temporary = safe_child(directory, f"tmp_{uuid.uuid4().hex}.json")
        content = canonical_json(
            {
                "connection_id": self.connection.connection_id,
                "captured_at": now.isoformat(),
                "entries": metadata,
                "candidate_shapes": candidate_shapes,
                "raw_bodies_persisted": False,
                "candidate_values_persisted": False,
            }
        )
        try:
            with temporary.open("xb") as handle:
                handle.write(content + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return name

    def _matches_response(
        self,
        response: Response,
        *,
        host: str,
        path: str,
        method: str,
        status: int,
    ) -> bool:
        parsed = urlsplit(response.url)
        content_type = response.headers.get("content-type", "").partition(";")[0].casefold()
        return (
            parsed.scheme in {"http", "https"}
            and parsed.hostname is not None
            and parsed.hostname.casefold() == host.casefold()
            and parsed.path == path
            and response.request.method == method
            and response.status == status
            and content_type == "application/json"
        )

    def _response_kind(self, response: Response) -> Literal["metric", "identity"] | None:
        adapter = self.connection.promotion_adapter
        if self._matches_response(
            response,
            host=adapter.response_host,
            path=adapter.response_path,
            method=adapter.response_method,
            status=adapter.response_http_status,
        ):
            return "metric"
        if adapter.identity_response_path and self._matches_response(
            response,
            host=adapter.identity_response_host,
            path=adapter.identity_response_path,
            method=adapter.identity_response_method,
            status=adapter.identity_response_http_status,
        ):
            return "identity"
        return None

    async def _capture_verified_response(
        self, page: Page, scope: Scope
    ) -> tuple[ParsedPromotionResponse, datetime]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[ParsedPromotionResponse, datetime]] = loop.create_future()
        tasks: set[asyncio.Task[None]] = set()
        bodies: dict[str, bytes] = {}
        active = True

        async def consume(response: Response, kind: Literal["metric", "identity"]) -> None:
            if future.done() or not active:
                return
            try:
                raw = await response.body()
                if len(raw) > self.collection.max_browser_response_bytes:
                    raise CollectionRejected("PLATFORM_ERROR", "BROWSER_RESPONSE_TOO_LARGE")
                bodies.setdefault(kind, raw)
                adapter = self.connection.promotion_adapter
                if "metric" not in bodies or (
                    adapter.identity_response_path and "identity" not in bodies
                ):
                    return
                parsed = parse_promotion_response(
                    bodies["metric"],
                    adapter,
                    expected_store_id=self.connection.expected_platform_store_id,
                    expected_business_date=scope.business_date,
                    identity_raw=bodies.get("identity"),
                    expected_store_id_sha256=self.connection.expected_platform_store_id_sha256,
                )
                result = (parsed, utc_now())
                if not future.done() and active:
                    future.set_result(result)
            except Exception as exc:
                if not future.done() and active:
                    future.set_exception(exc)

        def on_response(response: Response) -> None:
            kind = self._response_kind(response)
            if (
                not active
                or future.done()
                or kind is None
                or kind in bodies
                or len(tasks) >= self.collection.max_inflight_responses
            ):
                return
            task = asyncio.create_task(consume(response, kind))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        page.on("response", on_response)
        try:
            adapter = self.connection.promotion_adapter
            if adapter.trigger == "RELOAD":
                await page.reload(
                    wait_until="domcontentloaded",
                    timeout=self.collection.collection_timeout_ms,
                )
            try:
                return await asyncio.wait_for(
                    future, timeout=self.collection.collection_timeout_ms / 1000
                )
            except TimeoutError as exc:
                raise CollectionRejected("CAPTURE_TIMEOUT", "VERIFIED_RESPONSE_TIMEOUT") from exc
        finally:
            active = False
            page.remove_listener("response", on_response)
            if tasks:
                _, pending = await asyncio.wait(tasks, timeout=2)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

    async def _read_dom(
        self, page: Page, scope: Scope, *, require_store_id: bool
    ) -> tuple[str | None, ParsedDomMoney]:
        adapter = self.connection.promotion_adapter
        dom_store_id: str | None = None
        if adapter.dom_store_id_selector:
            dom_store_id = await read_locator_value(
                page,
                adapter.dom_store_id_selector,
                adapter.dom_store_id_attribute,
                failure_status="IDENTITY_UNVERIFIED",
                failure_code="DOM_STORE_ID_MISSING",
            )
            dom_store_id = verify_store_identity(
                dom_store_id,
                self.connection.expected_platform_store_id,
                self.connection.expected_platform_store_id_sha256,
            )
        elif require_store_id:
            raise CollectionRejected("IDENTITY_UNVERIFIED", "DOM_STORE_ID_NOT_CONFIGURED")
        dom_date = await read_locator_value(
            page,
            adapter.dom_business_date_selector,
            adapter.dom_business_date_attribute,
            failure_status="TIME_SCOPE_UNVERIFIED",
            failure_code="DOM_BUSINESS_DATE_MISSING",
        )
        expected_dom_date = adapter.dom_today_label or scope.business_date.isoformat()
        if dom_date != expected_dom_date:
            raise CollectionRejected("TIME_SCOPE_UNVERIFIED", "DOM_BUSINESS_DATE_MISMATCH")
        dom_money_text = await read_locator_value(
            page,
            adapter.dom_ad_spend_selector,
            adapter.dom_ad_spend_attribute,
            failure_status="DATA_MISMATCH",
            failure_code="DOM_AD_SPEND_MISSING",
        )
        dom_money = parse_dom_money(dom_money_text, adapter.dom_ad_spend_unit)
        return dom_store_id, dom_money
