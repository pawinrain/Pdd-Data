from __future__ import annotations

from datetime import datetime
from typing import Annotated, NoReturn

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from pdd_data_mcp.application import PddDataService
from pdd_data_mcp.contracts.models import (
    CapabilitiesResult,
    CollectResult,
    ConnectionStatusResult,
    DatasetType,
    IdempotencyKey,
    InternalId,
    LatestSnapshotResult,
    ListSnapshotsResult,
    ReadSnapshotResult,
    Scope,
    SnapshotId,
)
from pdd_data_mcp.errors import (
    AuthorizationError,
    IdempotencyConflictError,
    LockBusyError,
    PddDataMcpError,
    ResponseTooLargeError,
    SnapshotCorruptError,
    SnapshotNotFoundError,
    StorageFullError,
    ValidationFailure,
)

READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
COLLECTS_LOCAL_DATA = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)


def _raise_tool_error(exc: Exception) -> NoReturn:
    if isinstance(exc, AuthorizationError):
        raise ToolError("UNAUTHORIZED: configured connection or store is not allowed") from exc
    if isinstance(exc, IdempotencyConflictError):
        raise ToolError("IDEMPOTENCY_CONFLICT: key was reused with different parameters") from exc
    if isinstance(exc, LockBusyError):
        raise ToolError("LOCK_BUSY: another writer owns the data root") from exc
    if isinstance(exc, StorageFullError):
        raise ToolError("STORAGE_FULL: snapshot was not committed") from exc
    if isinstance(exc, SnapshotNotFoundError):
        raise ToolError("SNAPSHOT_NOT_FOUND") from exc
    if isinstance(exc, SnapshotCorruptError):
        raise ToolError("CHECKSUM_MISMATCH: snapshot is not readable") from exc
    if isinstance(exc, ResponseTooLargeError):
        raise ToolError("MCP_RESPONSE_TOO_LARGE: request a smaller page") from exc
    if isinstance(exc, ValidationFailure):
        raise ToolError(f"INVALID_REQUEST: {exc}") from exc
    if isinstance(exc, PddDataMcpError):
        raise ToolError("STORAGE_FAILURE: operation did not complete") from exc
    raise exc


def create_mcp_server(service: PddDataService) -> MCPServer:
    server = MCPServer(
        name="pdd-data-mcp",
        title="PDD Data MCP",
        description="Read-only platform data snapshots with immutable local storage.",
        version="0.1.0",
        log_level="WARNING",
    )

    @server.tool(annotations=READ_ONLY)
    def pdd_get_capabilities() -> CapabilitiesResult:
        """Report storage, synthetic-test, and real-adapter support separately."""
        return service.capabilities()

    @server.tool(annotations=READ_ONLY)
    def pdd_get_connection_status(connection_id: InternalId) -> ConnectionStatusResult:
        """Return configured status without opening Chrome or claiming a real connection."""
        try:
            return service.connection_status(connection_id)
        except Exception as exc:
            _raise_tool_error(exc)

    @server.tool(annotations=COLLECTS_LOCAL_DATA)
    async def pdd_collect_snapshot(
        connection_id: InternalId,
        dataset_type: DatasetType,
        scope: Scope,
        idempotency_key: IdempotencyKey,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        batch_id: InternalId | None = None,
    ) -> CollectResult:
        """Collect a snapshot; this navigates a future browser adapter and writes local files.

        Stage A/B permits only explicitly enabled synthetic collection in an isolated test root.
        """
        try:
            return await service.collect_snapshot(
                connection_id=connection_id,
                dataset_type=dataset_type,
                scope=scope,
                limit=limit,
                idempotency_key=idempotency_key,
                batch_id=batch_id,
            )
        except Exception as exc:
            _raise_tool_error(exc)

    @server.tool(annotations=READ_ONLY)
    def pdd_list_snapshots(
        store_id: InternalId,
        dataset_type: DatasetType,
        captured_from: datetime | None = None,
        captured_to: datetime | None = None,
        cursor: str | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> ListSnapshotsResult:
        """List authorized committed snapshots with bounded cursor pagination."""
        try:
            return service.list_snapshots(
                store_id=store_id,
                dataset_type=dataset_type,
                captured_from=captured_from,
                captured_to=captured_to,
                cursor=cursor,
                limit=limit,
            )
        except Exception as exc:
            _raise_tool_error(exc)

    @server.tool(annotations=READ_ONLY)
    def pdd_read_snapshot(
        snapshot_id: SnapshotId,
        cursor: str | None = None,
        page_size: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> ReadSnapshotResult:
        """Read one authorized committed snapshot; incomplete or corrupt data is rejected."""
        try:
            return service.read_snapshot(
                snapshot_id=snapshot_id, cursor=cursor, page_size=page_size
            )
        except Exception as exc:
            _raise_tool_error(exc)

    @server.tool(annotations=READ_ONLY)
    def pdd_get_latest_snapshot(
        store_id: InternalId,
        dataset_type: DatasetType,
        scope: Scope,
        require_complete: bool = False,
    ) -> LatestSnapshotResult:
        """Return the latest authorized snapshot for exactly the requested business scope."""
        try:
            return service.latest_snapshot(
                store_id=store_id,
                dataset_type=dataset_type,
                scope=scope,
                require_complete=require_complete,
            )
        except Exception as exc:
            _raise_tool_error(exc)

    return server
