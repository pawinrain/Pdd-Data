from __future__ import annotations

import argparse
import importlib.metadata
import json
import logging
import platform
import sys
from pathlib import Path
from typing import NoReturn
from zoneinfo import ZoneInfo

from pdd_data_mcp.application import PddDataService
from pdd_data_mcp.browser import (
    CoreDataCdpCollector,
    PromotionAccountCdpCollector,
    PromotionMetricsCdpCollector,
    PromotionOverviewCdpCollector,
    RealDatasetCollector,
)
from pdd_data_mcp.collectors import SyntheticCollector
from pdd_data_mcp.config import AppConfig, load_config
from pdd_data_mcp.errors import PddDataMcpError
from pdd_data_mcp.observability import configure_logging
from pdd_data_mcp.schema_export import export_schemas
from pdd_data_mcp.server import create_mcp_server
from pdd_data_mcp.storage import LocalFileSnapshotRepository
from pdd_data_mcp.validation import SnapshotValidator

LOGGER = logging.getLogger("pdd_data_mcp")


def _repository(config: AppConfig) -> LocalFileSnapshotRepository:
    return LocalFileSnapshotRepository(
        config.storage,
        allowed_store_ids=config.allowed_store_ids,
        max_response_bytes=config.collection.max_mcp_response_bytes,
    )


def _service(config: AppConfig, repository: LocalFileSnapshotRepository) -> PddDataService:
    real_collectors = {
        connection.connection_id: RealDatasetCollector(
            promotion=PromotionOverviewCdpCollector(
                connection=connection,
                collection=config.collection,
                runtime_root=config.storage.runtime_root,
            ),
            promotion_account=PromotionAccountCdpCollector(
                connection=connection,
                collection=config.collection,
                runtime_root=config.storage.runtime_root,
            ),
            promotion_metrics=PromotionMetricsCdpCollector(
                connection=connection,
                collection=config.collection,
                runtime_root=config.storage.runtime_root,
            ),
            core=CoreDataCdpCollector(
                connection=connection,
                collection=config.collection,
                runtime_root=config.storage.runtime_root,
            ),
        )
        for connection in config.connections
        if connection.real_collection_enabled
    }
    return PddDataService(
        config=config,
        repository=repository,
        synthetic_collector=SyntheticCollector(),
        validator=SnapshotValidator(),
        real_collectors=real_collectors,
    )


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _serve(config: AppConfig) -> None:
    repository = _repository(config)
    with repository.service_lock(timeout=0):
        repository.initialize()
        recovery = repository.recover()
        LOGGER.info(
            "service_start recovery_indexed=%s recovery_interrupted=%s",
            recovery["indexed_snapshots"],
            recovery["interrupted_requests"],
        )
        server = create_mcp_server(_service(config, repository))
        server.run("stdio")


def _doctor(config: AppConfig) -> dict[str, object]:
    ZoneInfo(config.storage.partition_timezone)
    repository = _repository(config)
    with repository.service_lock(timeout=0):
        repository.initialize()
        recovery = repository.recover()
    return {
        "status": "PASS",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "mcp": importlib.metadata.version("mcp"),
        "pydantic": importlib.metadata.version("pydantic"),
        "filelock": importlib.metadata.version("filelock"),
        "partition_timezone": config.storage.partition_timezone,
        "authorized_store_count": len(config.allowed_store_ids),
        "real_collection": "NOT_RUN",
        "recovery": dict(recovery),
    }


def _maintenance(config: AppConfig, command: str) -> dict[str, object]:
    repository = _repository(config)
    with repository.service_lock(timeout=0):
        repository.initialize()
        if command == "verify-storage":
            return repository.verify_storage()
        if command == "rebuild-index":
            return repository.rebuild_index()
    raise ValueError(command)


def _invalidate_snapshot(
    config: AppConfig,
    *,
    snapshot_id: str,
    reason_code: str,
    replacement_scope_version: str,
) -> dict[str, object]:
    repository = _repository(config)
    with repository.service_lock(timeout=0):
        repository.initialize()
        record = repository.invalidate_snapshot(
            snapshot_id=snapshot_id,
            reason_code=reason_code,
            replacement_scope_version=replacement_scope_version,
        )
    return {
        "status": "PASS",
        "effective_status": "SEMANTICALLY_INVALIDATED",
        "invalidation": record.model_dump(mode="json"),
        "snapshot_manifest_modified": False,
    }


def _fail(message: str, code: int = 2) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pdd-data-mcp")
    parser.add_argument("--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("serve", "doctor", "verify-storage", "rebuild-index"):
        item = subparsers.add_parser(command)
        item.add_argument("--config", type=Path, required=True)
    schema_parser = subparsers.add_parser("export-schemas")
    schema_parser.add_argument("--output", type=Path, required=True)
    invalidation_parser = subparsers.add_parser("invalidate-snapshot")
    invalidation_parser.add_argument("--config", type=Path, required=True)
    invalidation_parser.add_argument("--snapshot-id", required=True)
    invalidation_parser.add_argument(
        "--reason-code",
        required=True,
        choices=["METRIC_WINDOW_END_MISLABELED"],
    )
    invalidation_parser.add_argument("--replacement-scope-version", required=True)
    invalidation_parser.add_argument("--confirm-semantic-invalidation", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)
    try:
        if args.command == "export-schemas":
            written = export_schemas(args.output.resolve(strict=False))
            _print_json({"status": "PASS", "schemas": written})
            return
        if args.command == "invalidate-snapshot" and not args.confirm_semantic_invalidation:
            _fail("--confirm-semantic-invalidation is required")
        config = load_config(args.config)
        if args.command == "invalidate-snapshot":
            _print_json(
                _invalidate_snapshot(
                    config,
                    snapshot_id=args.snapshot_id,
                    reason_code=args.reason_code,
                    replacement_scope_version=args.replacement_scope_version,
                )
            )
            return
        if args.command == "serve":
            _serve(config)
            return
        if args.command == "doctor":
            _print_json(_doctor(config))
            return
        _print_json(_maintenance(config, args.command))
    except (OSError, ValueError, PddDataMcpError) as exc:
        _fail(f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
