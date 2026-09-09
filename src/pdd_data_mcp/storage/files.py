from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from filelock import FileLock, Timeout
from pydantic import ValidationError

from pdd_data_mcp.config import StorageSettings
from pdd_data_mcp.contracts.models import (
    SNAPSHOT_ID_PATTERN,
    CommitRecord,
    DatasetType,
    FileDescriptor,
    LatestSnapshotResult,
    ListSnapshotsResult,
    ReadSnapshotResult,
    Scope,
    SnapshotDraft,
    SnapshotInvalidationRecord,
    SnapshotManifest,
    SnapshotSummary,
    ValidationReport,
)
from pdd_data_mcp.errors import (
    AuthorizationError,
    IdempotencyConflictError,
    LockBusyError,
    ResponseTooLargeError,
    SnapshotCorruptError,
    SnapshotNotFoundError,
    StorageError,
    StorageFullError,
    ValidationFailure,
)
from pdd_data_mcp.security import safe_child
from pdd_data_mcp.storage.records import RequestReservation
from pdd_data_mcp.utils import (
    canonical_json,
    decode_cursor,
    decode_snapshot_list_cursor,
    encode_cursor,
    encode_snapshot_list_cursor,
    hash_object,
    scope_key,
    sha256_bytes,
    utc_now,
)


class RecoveryReport(dict[str, Any]):
    pass


def _json_load_bytes(raw: bytes) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant {value}")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key}")
            result[key] = value
        return result

    return json.loads(
        raw.decode("utf-8"),
        parse_constant=reject_constant,
        object_pairs_hook=reject_duplicates,
    )


def _read_json(path: Path) -> Any:
    try:
        return _json_load_bytes(path.read_bytes())
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise SnapshotCorruptError(f"invalid JSON file {path.name}") from exc


def _fsync_file(handle: Any) -> None:
    handle.flush()
    os.fsync(handle.fileno())


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            _fsync_file(handle)
        os.replace(temporary, path)
    except OSError as exc:
        raise StorageError(f"unable to write {path.name}: {exc}") from exc
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def _atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write_bytes(path, canonical_json(value) + b"\n")


def _atomic_create_json(path: Path, value: Any) -> None:
    """Create append-only metadata without ever replacing an existing path."""

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(canonical_json(value) + b"\n")
            _fsync_file(handle)
    except OSError as exc:
        raise StorageError(f"unable to append {path.name}: {exc}") from exc


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    content = b"".join(canonical_json(record) + b"\n" for record in records)
    _atomic_write_bytes(path, content)


def _descriptor(path: Path, records: int) -> FileDescriptor:
    content = path.read_bytes()
    return FileDescriptor(
        name=cast(Any, path.name),
        sha256=sha256_bytes(content),
        bytes=len(content),
        records=records,
    )


class LocalFileSnapshotRepository:
    def __init__(
        self,
        settings: StorageSettings,
        *,
        allowed_store_ids: frozenset[str],
        max_response_bytes: int,
    ) -> None:
        self.settings = settings
        self.data_root = settings.data_root.resolve(strict=False)
        self.allowed_store_ids = allowed_store_ids
        self.max_response_bytes = max_response_bytes
        self._lock = FileLock(str(self.data_root / "_locks" / "writer.lock"))
        self._lock_held = False

    @contextmanager
    def service_lock(self, timeout: float = 0) -> Iterator[None]:
        self.data_root.mkdir(parents=True, exist_ok=True)
        (self.data_root / "_locks").mkdir(parents=True, exist_ok=True)
        try:
            self._lock.acquire(timeout=timeout)
        except Timeout as exc:
            raise LockBusyError("LOCK_BUSY: data_root is already owned by another service") from exc
        self._lock_held = True
        try:
            yield
        finally:
            self._lock_held = False
            self._lock.release()

    def _require_lock(self) -> None:
        if not self._lock_held:
            raise StorageError("writer service lock is required")

    def initialize(self) -> None:
        self._require_lock()
        for relative in (
            "_meta",
            "_locks",
            "_requests",
            "_runs",
            "_tmp",
            "_quarantine",
            "_indexes",
            "_invalidations",
            "_pointers",
            "snapshots",
            "audit",
        ):
            safe_child(self.data_root, relative).mkdir(parents=True, exist_ok=True)
        metadata_path = safe_child(self.data_root, "_meta", "storage.json")
        expected = {
            "layout_version": self.settings.layout_version,
            "partition_timezone": self.settings.partition_timezone,
            "platform": "pdd",
        }
        if metadata_path.exists():
            actual = _read_json(metadata_path)
            if actual != expected:
                raise StorageError("storage metadata mismatch; automatic migration is disabled")
        else:
            _atomic_write_json(metadata_path, expected)
        self._check_writable()

    def _check_writable(self) -> None:
        probe = safe_child(self.data_root, "_tmp", f"write_probe_{uuid.uuid4().hex}")
        try:
            with probe.open("xb") as handle:
                handle.write(b"ok")
                _fsync_file(handle)
        except OSError as exc:
            raise StorageError(f"STORAGE_PERMISSION_DENIED: {exc}") from exc
        finally:
            probe.unlink(missing_ok=True)

    def _check_capacity(self, estimated_bytes: int) -> None:
        try:
            free = shutil.disk_usage(self.data_root).free
        except OSError as exc:
            raise StorageError(f"unable to inspect disk capacity: {exc}") from exc
        if free - estimated_bytes < self.settings.min_free_bytes:
            raise StorageFullError("STORAGE_FULL: minimum free space would be violated")
        used = 0
        snapshots_root = safe_child(self.data_root, "snapshots")
        for path in snapshots_root.rglob("*"):
            if path.is_file():
                try:
                    used += path.stat().st_size
                except OSError as exc:
                    raise StorageError(f"unable to inspect storage usage: {exc}") from exc
        if used + estimated_bytes > self.settings.max_bytes:
            raise StorageFullError("STORAGE_FULL: configured storage quota would be exceeded")

    def _authorize_store(self, store_id: str) -> None:
        if store_id not in self.allowed_store_ids:
            raise AuthorizationError("UNAUTHORIZED_STORE")

    def _request_path(self, store_id: str, dataset_type: DatasetType, key_hash: str) -> Path:
        self._authorize_store(store_id)
        return safe_child(
            self.data_root,
            "_requests",
            store_id,
            dataset_type.value,
            f"{key_hash}.json",
        )

    def begin_request(
        self,
        *,
        store_id: str,
        dataset_type: DatasetType,
        idempotency_key: str,
        parameters: dict[str, object],
        scope_key: str,
    ) -> RequestReservation:
        self._require_lock()
        key_hash = sha256_bytes(idempotency_key.encode("utf-8"))
        parameter_hash = hash_object(parameters)
        request_path = self._request_path(store_id, dataset_type, key_hash)
        if request_path.exists():
            record = _read_json(request_path)
            if not isinstance(record, dict):
                raise SnapshotCorruptError("invalid idempotency request record")
            if record.get("parameter_hash") != parameter_hash:
                raise IdempotencyConflictError("IDEMPOTENCY_CONFLICT")
            state = str(record.get("status"))
            if state not in {"COMMITTED", "RUNNING", "FAILED", "INTERRUPTED"}:
                raise SnapshotCorruptError("invalid idempotency request state")
            return RequestReservation(
                request_id=str(record["request_id"]),
                snapshot_id=str(record["snapshot_id"]),
                store_id=store_id,
                dataset_type=dataset_type.value,
                idempotency_key_hash=key_hash,
                parameter_hash=parameter_hash,
                scope_key=str(record["scope_key"]),
                request_file=str(request_path.relative_to(self.data_root).as_posix()),
                state=cast(Any, state),
                existing=record,
            )
        reservation = RequestReservation(
            request_id=f"r_{uuid.uuid4().hex}",
            snapshot_id=f"s_{uuid.uuid4().hex}",
            store_id=store_id,
            dataset_type=dataset_type.value,
            idempotency_key_hash=key_hash,
            parameter_hash=parameter_hash,
            scope_key=scope_key,
            request_file=str(request_path.relative_to(self.data_root).as_posix()),
            state="NEW",
        )
        record = {
            "request_id": reservation.request_id,
            "snapshot_id": reservation.snapshot_id,
            "store_id": store_id,
            "dataset_type": dataset_type.value,
            "idempotency_key_hash": key_hash,
            "parameter_hash": parameter_hash,
            "scope_key": scope_key,
            "status": "RUNNING",
            "created_at": utc_now().isoformat(),
            "updated_at": utc_now().isoformat(),
        }
        _atomic_write_json(request_path, record)
        self._write_last_attempt(
            store_id, dataset_type, scope_key, reservation.snapshot_id, "RUNNING"
        )
        return reservation

    def fail_request(self, reservation: RequestReservation, error_code: str) -> None:
        self._require_lock()
        path = self.data_root / Path(reservation.request_file)
        record = _read_json(path)
        if not isinstance(record, dict):
            raise SnapshotCorruptError("invalid request record")
        record["status"] = "FAILED"
        record["error_code"] = error_code
        record["updated_at"] = utc_now().isoformat()
        _atomic_write_json(path, record)
        self._write_last_attempt(
            reservation.store_id,
            DatasetType(reservation.dataset_type),
            reservation.scope_key,
            reservation.snapshot_id,
            "FAILED",
        )

    def commit_reserved(
        self,
        reservation: RequestReservation,
        draft: SnapshotDraft,
        validation: ValidationReport,
    ) -> tuple[SnapshotManifest, list[str]]:
        self._require_lock()
        if reservation.state != "NEW":
            raise StorageError("only a new reservation can be committed")
        if not validation.valid:
            self.fail_request(reservation, "VALIDATION_FAILED")
            raise ValidationFailure("snapshot validation failed")
        if (
            draft.store_id != reservation.store_id
            or draft.dataset_type.value != reservation.dataset_type
        ):
            self.fail_request(reservation, "REQUEST_DRAFT_MISMATCH")
            raise ValidationFailure("draft identity does not match request reservation")
        if draft.scope_key != reservation.scope_key:
            self.fail_request(reservation, "REQUEST_DRAFT_SCOPE_MISMATCH")
            raise ValidationFailure("draft scope does not match request reservation")
        payload_bytes = canonical_json(draft.payload)
        self._check_capacity(len(payload_bytes) + 16_384)
        temp_dir = safe_child(self.data_root, "_tmp", reservation.snapshot_id)
        if temp_dir.exists():
            raise StorageError("snapshot temporary directory already exists")
        temp_dir.mkdir(parents=True)
        try:
            is_records = isinstance(draft.payload, list)
            payload_name = "records.jsonl" if is_records else "data.json"
            payload_path = safe_child(temp_dir, payload_name)
            if is_records:
                _write_jsonl(payload_path, cast(list[dict[str, Any]], draft.payload))
            else:
                _atomic_write_json(payload_path, draft.payload)
            validation_path = safe_child(temp_dir, "validation.json")
            _atomic_write_json(validation_path, validation.model_dump(mode="json"))
            committed_at = utc_now()
            manifest = SnapshotManifest(
                snapshot_id=reservation.snapshot_id,
                request_id=reservation.request_id,
                batch_id=draft.batch_id,
                idempotency_key_hash=reservation.idempotency_key_hash,
                parameter_hash=reservation.parameter_hash,
                schema_version=(
                    "1.1.0"
                    if draft.scope.version == "d4-account-v3"
                    and draft.promotion_account_time_evidence is not None
                    else "1.0.0"
                ),
                layout_version=self.settings.layout_version,
                store_id=draft.store_id,
                dataset_type=draft.dataset_type,
                scope=draft.scope,
                scope_key=draft.scope_key,
                requested_at=draft.requested_at,
                capture_started_at=draft.capture_started_at,
                capture_finished_at=draft.capture_finished_at,
                captured_at=draft.captured_at,
                metric_window=draft.metric_window,
                source_updated_at=draft.source_updated_at,
                promotion_account_time_evidence=draft.promotion_account_time_evidence,
                committed_at=committed_at,
                source=draft.source,
                capture_method=draft.capture_method,
                parser_version=draft.parser_version,
                identity_evidence=draft.identity_evidence,
                field_sources=draft.field_sources,
                missing_fields=draft.missing_fields,
                quality=draft.quality,
                coverage=draft.coverage,
                payload_file=cast(Any, payload_name),
                files=[
                    _descriptor(payload_path, draft.coverage.captured),
                    _descriptor(validation_path, 1),
                ],
                record_count=draft.coverage.captured,
            )
            manifest_path = safe_child(temp_dir, "manifest.json")
            _atomic_write_json(manifest_path, manifest.model_dump(mode="json"))
            SnapshotManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
            local_time = draft.captured_at.astimezone(ZoneInfo(self.settings.partition_timezone))
            final_dir = safe_child(
                self.data_root,
                "snapshots",
                "pdd",
                draft.store_id,
                draft.dataset_type.value,
                f"{local_time:%Y}",
                f"{local_time:%m}",
                f"{local_time:%d}",
                (
                    f"{local_time:%H%M%S}_{local_time.microsecond // 1000:03d}_"
                    f"{reservation.snapshot_id}"
                ),
            )
            final_dir.parent.mkdir(parents=True, exist_ok=True)
            if final_dir.exists():
                raise StorageError("snapshot destination already exists")
            os.replace(temp_dir, final_dir)
            manifest_bytes = safe_child(final_dir, "manifest.json").read_bytes()
            commit = CommitRecord(
                snapshot_id=reservation.snapshot_id,
                manifest_sha256=sha256_bytes(manifest_bytes),
                committed_at=committed_at,
            )
            _atomic_write_json(safe_child(final_dir, "COMMIT.json"), commit.model_dump(mode="json"))
            self._verify_snapshot_dir(final_dir)
        except Exception:
            if temp_dir.exists():
                self._quarantine(temp_dir, "commit-failed")
            raise
        warnings: list[str] = []
        request_path = self.data_root / Path(reservation.request_file)
        request_record = _read_json(request_path)
        if not isinstance(request_record, dict):
            warnings.append("REQUEST_RECORD_UPDATE_FAILED")
        else:
            request_record["status"] = "COMMITTED"
            request_record["snapshot_path"] = str(final_dir.relative_to(self.data_root).as_posix())
            request_record["committed_at"] = committed_at.isoformat()
            request_record["updated_at"] = utc_now().isoformat()
            try:
                _atomic_write_json(request_path, request_record)
            except StorageError:
                warnings.append("REQUEST_RECORD_UPDATE_FAILED")
        try:
            self.rebuild_index()
        except StorageError:
            warnings.append("INDEX_UPDATE_FAILED")
        self._audit(
            {
                "event": "SNAPSHOT_COMMITTED",
                "snapshot_id": reservation.snapshot_id,
                "request_id": reservation.request_id,
                "store_id": reservation.store_id,
                "dataset_type": reservation.dataset_type,
                "status": "PARTIAL" if draft.coverage.truncated else "SUCCEEDED",
                "record_count": draft.coverage.captured,
                "at": committed_at.isoformat(),
            }
        )
        return manifest, warnings

    def _audit(self, event: dict[str, object]) -> None:
        now = utc_now().astimezone(ZoneInfo(self.settings.partition_timezone))
        path = safe_child(
            self.data_root,
            "audit",
            f"{now:%Y}",
            f"{now:%m}",
            f"{now:%d}",
            "events.jsonl",
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("ab") as handle:
                handle.write(canonical_json(event) + b"\n")
                _fsync_file(handle)
        except OSError:
            return

    def _quarantine(self, path: Path, reason: str) -> Path:
        destination = safe_child(
            self.data_root,
            "_quarantine",
            f"{reason}_{uuid.uuid4().hex}_{path.name}",
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(path), str(destination))
        except OSError as exc:
            raise StorageError(f"unable to quarantine {path.name}: {exc}") from exc
        return destination

    def _verify_snapshot_metadata(self, directory: Path) -> SnapshotManifest:
        try:
            commit_path = safe_child(directory, "COMMIT.json")
            manifest_path = safe_child(directory, "manifest.json")
            if not commit_path.is_file() or not manifest_path.is_file():
                raise SnapshotCorruptError("snapshot is not committed")
            commit = CommitRecord.model_validate_json(commit_path.read_text(encoding="utf-8"))
            manifest_bytes = manifest_path.read_bytes()
            if sha256_bytes(manifest_bytes) != commit.manifest_sha256:
                raise SnapshotCorruptError("manifest checksum mismatch")
            manifest = SnapshotManifest.model_validate_json(manifest_bytes)
            if manifest.snapshot_id != commit.snapshot_id:
                raise SnapshotCorruptError("snapshot identity mismatch")
            if manifest.scope_key != scope_key(manifest.scope):
                raise SnapshotCorruptError("snapshot scope key mismatch")
            local_time = manifest.captured_at.astimezone(ZoneInfo(self.settings.partition_timezone))
            expected_directory = safe_child(
                self.data_root,
                "snapshots",
                "pdd",
                manifest.store_id,
                manifest.dataset_type.value,
                f"{local_time:%Y}",
                f"{local_time:%m}",
                f"{local_time:%d}",
                (
                    f"{local_time:%H%M%S}_{local_time.microsecond // 1000:03d}_"
                    f"{manifest.snapshot_id}"
                ),
            ).resolve(strict=False)
            if directory.resolve(strict=False) != expected_directory:
                raise SnapshotCorruptError("snapshot physical partition mismatch")
            return manifest
        except (OSError, ValidationError, ValueError, ValidationFailure) as exc:
            if isinstance(exc, SnapshotCorruptError):
                raise
            raise SnapshotCorruptError(f"invalid snapshot {directory.name}") from exc

    def _verify_snapshot_dir(
        self, directory: Path
    ) -> tuple[SnapshotManifest, dict[str, Any] | list[dict[str, Any]]]:
        manifest = self._verify_snapshot_metadata(directory)
        try:
            expected_file_names = {manifest.payload_file, "validation.json"}
            actual_file_names = [item.name for item in manifest.files]
            if (
                len(actual_file_names) != len(expected_file_names)
                or set(actual_file_names) != expected_file_names
            ):
                raise SnapshotCorruptError("snapshot file descriptors are incomplete or duplicate")
            descriptors = {item.name: item for item in manifest.files}
            if (
                descriptors[manifest.payload_file].records != manifest.record_count
                or descriptors["validation.json"].records != 1
            ):
                raise SnapshotCorruptError("snapshot descriptor record count mismatch")
            for name, descriptor in descriptors.items():
                file_path = safe_child(directory, name)
                if not file_path.is_file():
                    raise SnapshotCorruptError(f"missing snapshot file {name}")
                content = file_path.read_bytes()
                if len(content) != descriptor.bytes or sha256_bytes(content) != descriptor.sha256:
                    raise SnapshotCorruptError(f"checksum mismatch for {name}")
            validation = ValidationReport.model_validate_json(
                safe_child(directory, "validation.json").read_bytes()
            )
            if (
                not validation.valid
                or validation.errors
                or validation.missing_fields != manifest.missing_fields
            ):
                raise SnapshotCorruptError("snapshot validation report mismatch")
            payload_path = safe_child(directory, manifest.payload_file)
            if manifest.payload_file == "records.jsonl":
                records: list[dict[str, Any]] = []
                for line in payload_path.read_bytes().splitlines():
                    value = _json_load_bytes(line)
                    if not isinstance(value, dict):
                        raise SnapshotCorruptError("JSONL line is not an object")
                    records.append(value)
                payload: dict[str, Any] | list[dict[str, Any]] = records
            else:
                value = _read_json(payload_path)
                if not isinstance(value, dict):
                    raise SnapshotCorruptError("data.json is not an object")
                payload = value
            actual_count = len(payload) if isinstance(payload, list) else 1
            if actual_count != manifest.record_count:
                raise SnapshotCorruptError("record count mismatch")
            return manifest, payload
        except (OSError, ValidationError, ValueError, ValidationFailure) as exc:
            if isinstance(exc, SnapshotCorruptError):
                raise
            raise SnapshotCorruptError(f"invalid snapshot {directory.name}") from exc

    def _load_invalidations(
        self,
    ) -> tuple[dict[str, SnapshotInvalidationRecord], set[str], list[str]]:
        """Load append-only invalidations; corrupt target state is fail-closed."""

        root = safe_child(self.data_root, "_invalidations")
        if not root.exists():
            return {}, set(), []
        records: dict[str, SnapshotInvalidationRecord] = {}
        unknown: set[str] = set()
        invalid_files: list[str] = []
        try:
            children = list(root.iterdir())
        except OSError as exc:
            raise StorageError(f"unable to inspect invalidations: {exc}") from exc
        for directory in children:
            if not directory.is_dir() or re.fullmatch(SNAPSHOT_ID_PATTERN, directory.name) is None:
                raise StorageError("INVALIDATION_STATE_CORRUPT: invalid top-level entry")
            snapshot_id = directory.name
            try:
                paths = list(directory.iterdir())
            except OSError:
                unknown.add(snapshot_id)
                invalid_files.append(f"{snapshot_id}/<unreadable>")
                continue
            parsed: list[SnapshotInvalidationRecord] = []
            if not paths:
                unknown.add(snapshot_id)
                invalid_files.append(f"{snapshot_id}/<empty>")
            for path in paths:
                relative = f"{snapshot_id}/{path.name}"
                try:
                    if not path.is_file() or path.suffix != ".json":
                        raise SnapshotCorruptError("invalid invalidation entry")
                    record = SnapshotInvalidationRecord.model_validate_json(
                        canonical_json(_read_json(path))
                    )
                    if record.snapshot_id != snapshot_id or path.stem != record.invalidation_id:
                        raise SnapshotCorruptError("invalidation identity mismatch")
                    parsed.append(record)
                except (OSError, ValidationError, SnapshotCorruptError, ValueError):
                    unknown.add(snapshot_id)
                    invalid_files.append(relative)
            if len(parsed) > 1:
                unknown.add(snapshot_id)
                invalid_files.extend(
                    f"{snapshot_id}/{item.invalidation_id}.json" for item in parsed
                )
            elif len(parsed) == 1 and snapshot_id not in unknown:
                records[snapshot_id] = parsed[0]
        return records, unknown, sorted(set(invalid_files))

    @staticmethod
    def _effective_state(
        snapshot_id: str,
        records: dict[str, SnapshotInvalidationRecord],
        unknown: set[str],
    ) -> tuple[
        Literal["ACTIVE", "SEMANTICALLY_INVALIDATED", "INVALIDATION_STATE_UNKNOWN"],
        SnapshotInvalidationRecord | None,
    ]:
        if snapshot_id in unknown:
            return "INVALIDATION_STATE_UNKNOWN", None
        record = records.get(snapshot_id)
        if record is not None:
            return "SEMANTICALLY_INVALIDATED", record
        return "ACTIVE", None

    @staticmethod
    def _invalidation_matches_manifest(
        record: SnapshotInvalidationRecord, manifest: SnapshotManifest
    ) -> bool:
        return (
            record.snapshot_id == manifest.snapshot_id
            and record.store_id == manifest.store_id
            and record.dataset_type is manifest.dataset_type
            and record.prior_scope_version == manifest.scope.version
            and record.prior_parser_version == manifest.parser_version
        )

    def _effective_manifest_state(
        self,
        manifest: SnapshotManifest,
        records: dict[str, SnapshotInvalidationRecord],
        unknown: set[str],
    ) -> tuple[
        Literal["ACTIVE", "SEMANTICALLY_INVALIDATED", "INVALIDATION_STATE_UNKNOWN"],
        SnapshotInvalidationRecord | None,
    ]:
        status, record = self._effective_state(manifest.snapshot_id, records, unknown)
        if record is not None and not self._invalidation_matches_manifest(record, manifest):
            return "INVALIDATION_STATE_UNKNOWN", None
        return status, record

    def recover(self) -> RecoveryReport:
        self._require_lock()
        quarantined: list[str] = []
        temp_root = safe_child(self.data_root, "_tmp")
        for child in list(temp_root.iterdir()):
            if child.is_dir():
                quarantined.append(self._quarantine(child, "recovered-temp").name)
        snapshots_root = safe_child(self.data_root, "snapshots")
        committed_by_id: defaultdict[str, list[Path]] = defaultdict(list)
        for commit_path in list(snapshots_root.rglob("COMMIT.json")):
            try:
                commit = CommitRecord.model_validate_json(commit_path.read_bytes())
            except (OSError, ValidationError):
                continue
            committed_by_id[commit.snapshot_id].append(commit_path.parent)
        duplicate_directories = {
            directory
            for directories in committed_by_id.values()
            if len(directories) > 1
            for directory in directories
        }
        for directory in sorted(
            duplicate_directories,
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            if directory.exists():
                quarantined.append(self._quarantine(directory, "duplicate-snapshot-id").name)
        for manifest_path in list(snapshots_root.rglob("manifest.json")):
            directory = manifest_path.parent
            try:
                self._verify_snapshot_dir(directory)
            except SnapshotCorruptError:
                quarantined.append(self._quarantine(directory, "invalid-snapshot").name)
        index_report = self.rebuild_index()
        snapshot_ids = set(index_report["snapshot_ids"])
        interrupted = 0
        recovered_commits = 0
        requests_root = safe_child(self.data_root, "_requests")
        for request_path in requests_root.rglob("*.json"):
            try:
                record = _read_json(request_path)
                if not isinstance(record, dict) or record.get("status") != "RUNNING":
                    continue
                snapshot_id = str(record.get("snapshot_id"))
                if snapshot_id in snapshot_ids:
                    record["status"] = "COMMITTED"
                    recovered_commits += 1
                else:
                    record["status"] = "INTERRUPTED"
                    record["error_code"] = "PROCESS_INTERRUPTED"
                    interrupted += 1
                record["updated_at"] = utc_now().isoformat()
                _atomic_write_json(request_path, record)
            except (StorageError, SnapshotCorruptError):
                continue
        return RecoveryReport(
            quarantined=quarantined,
            interrupted_requests=interrupted,
            recovered_commits=recovered_commits,
            indexed_snapshots=len(snapshot_ids),
        )

    def _scan_index(
        self, *, verify_payload: bool
    ) -> tuple[dict[str, str], list[dict[str, Any]], list[str], list[str]]:
        """Build canonical index values from immutable committed snapshot metadata."""

        snapshots: dict[str, str] = {}
        entries: list[dict[str, Any]] = []
        invalid: list[str] = []
        invalidations, unknown_invalidations, invalid_invalidation_records = (
            self._load_invalidations()
        )
        root = safe_child(self.data_root, "snapshots")
        observed_directories: defaultdict[str, list[Path]] = defaultdict(list)
        verified: list[tuple[Path, SnapshotManifest]] = []
        for commit_path in root.rglob("COMMIT.json"):
            directory = commit_path.parent
            try:
                commit = CommitRecord.model_validate_json(commit_path.read_bytes())
                observed_directories[commit.snapshot_id].append(directory)
            except (OSError, ValidationError):
                pass
            try:
                manifest = (
                    self._verify_snapshot_dir(directory)[0]
                    if verify_payload
                    else self._verify_snapshot_metadata(directory)
                )
            except SnapshotCorruptError:
                invalid.append(directory.name)
                continue
            verified.append((directory, manifest))
        duplicate_snapshot_ids = {
            snapshot_id
            for snapshot_id, directories in observed_directories.items()
            if len(directories) > 1
        }
        for snapshot_id in duplicate_snapshot_ids:
            invalid.extend(path.name for path in observed_directories[snapshot_id])
        for directory, manifest in verified:
            if manifest.snapshot_id in duplicate_snapshot_ids:
                continue
            relative = str(directory.relative_to(self.data_root).as_posix())
            snapshots[manifest.snapshot_id] = relative
            effective_status, invalidation = self._effective_manifest_state(
                manifest, invalidations, unknown_invalidations
            )
            if effective_status == "INVALIDATION_STATE_UNKNOWN" and (
                manifest.snapshot_id in invalidations
                and manifest.snapshot_id not in unknown_invalidations
            ):
                unknown_invalidations.add(manifest.snapshot_id)
                invalid_invalidation_records.append(f"{manifest.snapshot_id}/<manifest-mismatch>")
            summary = self._summary(
                manifest,
                effective_status=effective_status,
                invalidation=invalidation,
            ).model_dump(mode="json")
            entries.append({**summary, "path": relative})
        referenced = set(invalidations) | unknown_invalidations
        for orphan in sorted(referenced - set(snapshots)):
            invalid_invalidation_records.append(f"{orphan}/<snapshot-not-found>")
        entries.sort(
            key=lambda item: (
                datetime.fromisoformat(str(item["captured_at"])),
                str(item["snapshot_id"]),
            ),
            reverse=True,
        )
        return (
            snapshots,
            entries,
            sorted(set(invalid)),
            sorted(set(invalid_invalidation_records)),
        )

    def _scan_snapshot_index(
        self,
    ) -> tuple[dict[str, str], list[dict[str, Any]], list[str], list[str]]:
        return self._scan_index(verify_payload=True)

    def _scan_snapshot_metadata_index(
        self,
    ) -> tuple[dict[str, str], list[dict[str, Any]], list[str], list[str]]:
        return self._scan_index(verify_payload=False)

    def rebuild_index(self) -> dict[str, Any]:
        self._require_lock()
        snapshots, entries, invalid, invalid_invalidation_records = self._scan_snapshot_index()
        index_root = safe_child(self.data_root, "_indexes")
        _atomic_write_json(safe_child(index_root, "snapshot-map.json"), {"snapshots": snapshots})
        _atomic_write_json(safe_child(index_root, "catalog.json"), {"items": entries})
        groups: defaultdict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(
            list
        )
        for entry in entries:
            captured = datetime.fromisoformat(str(entry["captured_at"]))
            local = captured.astimezone(ZoneInfo(self.settings.partition_timezone))
            key = (
                str(entry["store_id"]),
                str(entry["dataset_type"]),
                f"{local:%Y}",
                f"{local:%m}",
                f"{local:%d}",
            )
            groups[key].append(entry)
        for (store_id, dataset, year, month, day), items in groups.items():
            path = safe_child(index_root, "pdd", store_id, dataset, year, month, f"{day}.json")
            _atomic_write_json(path, {"items": items})
        self._rebuild_pointers(entries)
        return {
            "indexed": len(entries),
            "invalid": invalid,
            "snapshot_ids": list(snapshots),
            "semantically_invalidated": sum(
                item.get("effective_status", "ACTIVE") == "SEMANTICALLY_INVALIDATED"
                for item in entries
            ),
            "invalidation_state_unknown": sum(
                item.get("effective_status", "ACTIVE") == "INVALIDATION_STATE_UNKNOWN"
                for item in entries
            ),
            "invalid_invalidation_records": invalid_invalidation_records,
        }

    def _rebuild_pointers(self, entries: list[dict[str, Any]]) -> None:
        grouped: defaultdict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for entry in entries:
            group_key = (
                str(entry["store_id"]),
                str(entry["dataset_type"]),
                str(entry["scope_key"]),
            )
            grouped[group_key].append(entry)
        for (store_id, dataset, key), items in grouped.items():
            ordered = sorted(
                items,
                key=lambda item: (
                    datetime.fromisoformat(str(item["captured_at"])),
                    str(item["snapshot_id"]),
                ),
                reverse=True,
            )
            usable = next(
                (
                    item
                    for item in ordered
                    if item["quality_status"] == "VALID"
                    and item.get("effective_status", "ACTIVE") == "ACTIVE"
                ),
                None,
            )
            complete = next(
                (
                    item
                    for item in ordered
                    if item["quality_status"] == "VALID"
                    and item.get("effective_status", "ACTIVE") == "ACTIVE"
                    and item["coverage"] == "COMPLETE"
                ),
                None,
            )
            pointer = {
                "scope_key": key,
                "last_attempt": ordered[0]["snapshot_id"] if ordered else None,
                "latest_usable": usable["snapshot_id"] if usable else None,
                "latest_complete": complete["snapshot_id"] if complete else None,
            }
            pointer_path = safe_child(
                self.data_root,
                "_pointers",
                "pdd",
                store_id,
                dataset,
                f"{sha256_bytes(key.encode('utf-8'))}.json",
            )
            _atomic_write_json(pointer_path, pointer)

    def _write_last_attempt(
        self,
        store_id: str,
        dataset_type: DatasetType,
        key: str,
        snapshot_id: str,
        status: str,
    ) -> None:
        path = safe_child(
            self.data_root,
            "_pointers",
            "pdd",
            store_id,
            dataset_type.value,
            f"{sha256_bytes(key.encode('utf-8'))}.json",
        )
        existing: dict[str, Any] = {}
        if path.exists():
            value = _read_json(path)
            if isinstance(value, dict):
                existing = value
        existing.update(
            {
                "scope_key": key,
                "last_attempt": snapshot_id,
                "last_attempt_status": status,
                "updated_at": utc_now().isoformat(),
            }
        )
        _atomic_write_json(path, existing)

    def _read_validated_catalog(self, path: Path) -> list[dict[str, Any]]:
        value = _read_json(path)
        if (
            not isinstance(value, dict)
            or set(value) != {"items"}
            or not isinstance(value.get("items"), list)
        ):
            raise SnapshotCorruptError("invalid catalog index")
        items = cast(list[Any], value["items"])
        seen_snapshot_ids: set[str] = set()
        seen_paths: set[str] = set()
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise SnapshotCorruptError("invalid catalog entry")
            public = {key: entry_value for key, entry_value in item.items() if key != "path"}
            try:
                summary = SnapshotSummary.model_validate_json(canonical_json(public))
            except ValidationError as exc:
                raise SnapshotCorruptError("invalid catalog entry") from exc
            relative = str(item["path"])
            directory = (self.data_root / Path(relative)).resolve(strict=False)
            snapshots_root = safe_child(self.data_root, "snapshots")
            if snapshots_root not in directory.parents:
                raise SnapshotCorruptError("catalog path escapes snapshot root")
            canonical_relative = str(directory.relative_to(self.data_root).as_posix())
            if relative != canonical_relative:
                raise SnapshotCorruptError("catalog path is not canonical")
            parts = Path(relative).parts
            if (
                len(parts) != 8
                or parts[:2] != ("snapshots", "pdd")
                or parts[2] != summary.store_id
                or parts[3] != summary.dataset_type.value
                or not parts[7].endswith(f"_{summary.snapshot_id}")
            ):
                raise SnapshotCorruptError("catalog path identity mismatch")
            if summary.snapshot_id in seen_snapshot_ids or relative in seen_paths:
                raise SnapshotCorruptError("duplicate catalog entry")
            seen_snapshot_ids.add(summary.snapshot_id)
            seen_paths.add(relative)
        typed_items = cast(list[dict[str, Any]], items)
        ordered = sorted(
            typed_items,
            key=lambda item: (
                datetime.fromisoformat(str(item["captured_at"])),
                str(item["snapshot_id"]),
            ),
            reverse=True,
        )
        if typed_items != ordered:
            raise SnapshotCorruptError("catalog entries are not in canonical order")
        snapshot_map = self._read_validated_snapshot_map(
            safe_child(self.data_root, "_indexes", "snapshot-map.json")
        )
        catalog_map = {str(item["snapshot_id"]): str(item["path"]) for item in typed_items}
        if snapshot_map != catalog_map:
            raise SnapshotCorruptError("catalog and snapshot map disagree")
        canonical_map, canonical_items, _, _ = self._scan_snapshot_metadata_index()
        if snapshot_map != canonical_map or typed_items != canonical_items:
            raise SnapshotCorruptError("catalog index omits or changes committed snapshots")
        return typed_items

    def _load_catalog(self, *, store_id: str, dataset_type: DatasetType) -> list[dict[str, Any]]:
        path = safe_child(self.data_root, "_indexes", "catalog.json")

        def read_selected() -> list[dict[str, Any]]:
            entries = self._read_validated_catalog(path)
            selected = [
                entry
                for entry in entries
                if entry.get("store_id") == store_id
                and entry.get("dataset_type") == dataset_type.value
            ]
            return selected

        try:
            return read_selected()
        except SnapshotCorruptError:
            self.rebuild_index()
        try:
            return read_selected()
        except SnapshotCorruptError as exc:
            raise StorageError("INDEX_REBUILD_FAILED") from exc

    def _read_validated_snapshot_map(self, path: Path) -> dict[str, str]:
        value = _read_json(path)
        if (
            not isinstance(value, dict)
            or set(value) != {"snapshots"}
            or not isinstance(value.get("snapshots"), dict)
        ):
            raise SnapshotCorruptError("invalid snapshot map")
        snapshots = cast(dict[Any, Any], value["snapshots"])
        if any(
            not isinstance(snapshot_id, str)
            or re.fullmatch(SNAPSHOT_ID_PATTERN, snapshot_id) is None
            or not isinstance(relative, str)
            for snapshot_id, relative in snapshots.items()
        ):
            raise SnapshotCorruptError("invalid snapshot map entry")
        return cast(dict[str, str], snapshots)

    def _resolve_snapshot_map_entry(self, path: Path, snapshot_id: str) -> Path:
        snapshot_map = self._read_validated_snapshot_map(path)
        relative = snapshot_map.get(snapshot_id)
        if not isinstance(relative, str):
            raise SnapshotNotFoundError("SNAPSHOT_NOT_FOUND")
        directory = (self.data_root / Path(relative)).resolve(strict=False)
        snapshots_root = safe_child(self.data_root, "snapshots")
        if snapshots_root not in directory.parents:
            raise SnapshotCorruptError("snapshot index escapes snapshot root")
        canonical_relative = str(directory.relative_to(self.data_root).as_posix())
        if relative != canonical_relative:
            raise SnapshotCorruptError("snapshot index path is not canonical")
        committed_directories: list[Path] = []
        root = safe_child(self.data_root, "snapshots")
        try:
            commit_paths = list(root.rglob("COMMIT.json"))
        except OSError as exc:
            raise StorageError(f"unable to inspect committed snapshots: {exc}") from exc
        for commit_path in commit_paths:
            try:
                commit = CommitRecord.model_validate_json(commit_path.read_bytes())
            except (OSError, ValidationError):
                continue
            if commit.snapshot_id == snapshot_id:
                committed_directories.append(commit_path.parent.resolve(strict=False))
        if committed_directories != [directory]:
            raise SnapshotCorruptError("snapshot identity is not uniquely mapped")
        manifest = self._verify_snapshot_metadata(directory)
        if manifest.snapshot_id != snapshot_id:
            raise SnapshotCorruptError("snapshot map identity mismatch")
        return directory

    def _find_snapshot_dir(self, snapshot_id: str) -> Path:
        if re.fullmatch(SNAPSHOT_ID_PATTERN, snapshot_id) is None:
            raise SnapshotNotFoundError("SNAPSHOT_NOT_FOUND")
        map_path = safe_child(self.data_root, "_indexes", "snapshot-map.json")
        try:
            return self._resolve_snapshot_map_entry(map_path, snapshot_id)
        except SnapshotNotFoundError:
            raise
        except SnapshotCorruptError:
            self.rebuild_index()
        try:
            return self._resolve_snapshot_map_entry(map_path, snapshot_id)
        except SnapshotNotFoundError:
            raise
        except SnapshotCorruptError as exc:
            raise StorageError("INDEX_REBUILD_FAILED") from exc

    def get_manifest(self, snapshot_id: str) -> SnapshotManifest:
        manifest, _ = self._verify_snapshot_dir(self._find_snapshot_dir(snapshot_id))
        self._authorize_store(manifest.store_id)
        return manifest

    def get_snapshot_effective_status(
        self, snapshot_id: str
    ) -> Literal["ACTIVE", "SEMANTICALLY_INVALIDATED", "INVALIDATION_STATE_UNKNOWN"]:
        manifest = self.get_manifest(snapshot_id)
        invalidations, unknown_invalidations, _ = self._load_invalidations()
        status, _ = self._effective_manifest_state(manifest, invalidations, unknown_invalidations)
        return status

    def invalidate_snapshot(
        self,
        *,
        snapshot_id: str,
        reason_code: str,
        replacement_scope_version: str,
    ) -> SnapshotInvalidationRecord:
        """Append a semantic invalidation without modifying snapshot history."""

        self._require_lock()
        manifest = self.get_manifest(snapshot_id)
        if reason_code != "METRIC_WINDOW_END_MISLABELED":
            raise ValidationFailure("unsupported semantic invalidation reason")
        if not (
            manifest.dataset_type is DatasetType.PROMOTION_OVERVIEW
            and manifest.scope.kind.value == "YESTERDAY"
            and manifest.scope.version == "d4-account-v2"
            and manifest.parser_version == "d4-account-v2"
            and replacement_scope_version == "d4-account-v3"
        ):
            raise ValidationFailure("semantic invalidation reason does not apply to snapshot")
        records, unknown, _ = self._load_invalidations()
        if snapshot_id in unknown:
            raise StorageError("INVALIDATION_STATE_CORRUPT")
        existing = records.get(snapshot_id)
        if existing is not None:
            if (
                existing.reason_code == reason_code
                and existing.replacement_scope_version == replacement_scope_version
                and self._invalidation_matches_manifest(existing, manifest)
            ):
                self._rebuild_and_verify_invalidation(existing)
                return existing
            raise ValidationFailure("snapshot already has a different semantic invalidation")
        record = SnapshotInvalidationRecord(
            invalidation_id=f"i_{uuid.uuid4().hex}",
            snapshot_id=manifest.snapshot_id,
            store_id=manifest.store_id,
            dataset_type=manifest.dataset_type,
            invalidated_at=utc_now(),
            reason_code="METRIC_WINDOW_END_MISLABELED",
            prior_scope_version=manifest.scope.version,
            prior_parser_version=manifest.parser_version,
            replacement_scope_version=replacement_scope_version,
        )
        self._check_capacity(4096)
        path = safe_child(
            self.data_root,
            "_invalidations",
            snapshot_id,
            f"{record.invalidation_id}.json",
        )
        _atomic_create_json(path, record.model_dump(mode="json"))
        self._rebuild_and_verify_invalidation(record)
        self._audit(
            {
                "event": "SNAPSHOT_SEMANTICALLY_INVALIDATED",
                "snapshot_id": snapshot_id,
                "invalidation_id": record.invalidation_id,
                "reason_code": record.reason_code,
                "replacement_scope_version": record.replacement_scope_version,
                "at": record.invalidated_at.isoformat(),
            }
        )
        return record

    def _rebuild_and_verify_invalidation(self, record: SnapshotInvalidationRecord) -> None:
        self.rebuild_index()
        entries = self._read_validated_catalog(
            safe_child(self.data_root, "_indexes", "catalog.json")
        )
        matching = [item for item in entries if item.get("snapshot_id") == record.snapshot_id]
        if (
            len(matching) != 1
            or matching[0].get("effective_status") != "SEMANTICALLY_INVALIDATED"
            or not isinstance(matching[0].get("invalidation"), dict)
            or matching[0]["invalidation"].get("invalidation_id") != record.invalidation_id
        ):
            raise StorageError("INVALIDATION_INDEX_UPDATE_FAILED")

    def list_snapshots(
        self,
        *,
        store_id: str,
        dataset_type: DatasetType,
        captured_from: datetime | None,
        captured_to: datetime | None,
        cursor: str | None,
        limit: int,
    ) -> ListSnapshotsResult:
        self._authorize_store(store_id)
        if not 1 <= limit <= 100:
            raise ValidationFailure("list limit must be between 1 and 100")
        for value in (captured_from, captured_to):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValidationFailure("query datetimes require timezone")
        if captured_from and captured_to:
            if captured_to <= captured_from:
                raise ValidationFailure("captured_to must be after captured_from")
            if (captured_to - captured_from).days > 31:
                raise ValidationFailure("history query range cannot exceed 31 days")
        query = {
            "store_id": store_id,
            "dataset_type": dataset_type.value,
            "captured_from": captured_from.isoformat() if captured_from else None,
            "captured_to": captured_to.isoformat() if captured_to else None,
        }
        query_hash = hash_object(query)
        anchor = decode_snapshot_list_cursor(cursor, query_hash=query_hash) if cursor else None
        matches: list[SnapshotSummary] = []
        for entry in self._load_catalog(store_id=store_id, dataset_type=dataset_type):
            captured = datetime.fromisoformat(str(entry["captured_at"]))
            if captured_from and captured < captured_from:
                continue
            if captured_to and captured >= captured_to:
                continue
            if anchor is not None and (captured, str(entry["snapshot_id"])) >= anchor:
                continue
            public = {key: value for key, value in entry.items() if key != "path"}
            matches.append(SnapshotSummary.model_validate_json(canonical_json(public)))
        page = matches[:limit]
        next_cursor = (
            encode_snapshot_list_cursor(
                query_hash,
                captured_at=page[-1].captured_at,
                snapshot_id=page[-1].snapshot_id,
            )
            if page and len(matches) > len(page)
            else None
        )
        result = ListSnapshotsResult(items=page, next_cursor=next_cursor)
        self._check_response_size(result.model_dump(mode="json"))
        return result

    def read_snapshot(
        self, *, snapshot_id: str, cursor: str | None, page_size: int
    ) -> ReadSnapshotResult:
        if not 1 <= page_size <= 200:
            raise ValidationFailure("page_size must be between 1 and 200")
        manifest, payload = self._verify_snapshot_dir(self._find_snapshot_dir(snapshot_id))
        self._authorize_store(manifest.store_id)
        invalidations, unknown_invalidations, _ = self._load_invalidations()
        effective_status, invalidation = self._effective_manifest_state(
            manifest, invalidations, unknown_invalidations
        )
        query_hash = hash_object({"snapshot_id": snapshot_id})
        if isinstance(payload, list):
            offset = (
                decode_cursor(cursor, kind="snapshot-records", query_hash=query_hash)
                if cursor
                else 0
            )
            records = payload[offset : offset + page_size]
            while records:
                next_offset = offset + len(records)
                next_cursor = (
                    encode_cursor("snapshot-records", query_hash, next_offset)
                    if next_offset < len(payload)
                    else None
                )
                result = ReadSnapshotResult(
                    snapshot_id=snapshot_id,
                    manifest=manifest.model_dump(mode="json"),
                    effective_status=effective_status,
                    invalidation=invalidation,
                    records=records,
                    next_cursor=next_cursor,
                )
                if len(canonical_json(result.model_dump(mode="json"))) <= self.max_response_bytes:
                    return result
                records = records[:-1]
            raise ResponseTooLargeError("MCP_RESPONSE_TOO_LARGE")
        if cursor:
            raise ValidationFailure("object snapshots do not use a cursor")
        result = ReadSnapshotResult(
            snapshot_id=snapshot_id,
            manifest=manifest.model_dump(mode="json"),
            effective_status=effective_status,
            invalidation=invalidation,
            data=payload,
        )
        self._check_response_size(result.model_dump(mode="json"))
        return result

    def latest_snapshot(
        self,
        *,
        store_id: str,
        dataset_type: DatasetType,
        scope: Scope,
        require_complete: bool,
    ) -> LatestSnapshotResult:
        self._authorize_store(store_id)
        key = scope_key(scope)
        matching = [
            entry
            for entry in self._load_catalog(store_id=store_id, dataset_type=dataset_type)
            if entry.get("scope_key") == key
            and entry.get("quality_status") == "VALID"
            and entry.get("effective_status", "ACTIVE") == "ACTIVE"
        ]
        if not matching:
            return LatestSnapshotResult(status="NOT_FOUND", message="no usable snapshot for scope")
        matching.sort(
            key=lambda item: (
                datetime.fromisoformat(str(item["captured_at"])),
                str(item["snapshot_id"]),
            ),
            reverse=True,
        )
        if require_complete:
            complete = next((item for item in matching if item.get("coverage") == "COMPLETE"), None)
            if complete is None:
                return LatestSnapshotResult(
                    status="PARTIAL_ONLY",
                    message="only partial or truncated snapshots exist for scope",
                )
            selected = complete
        else:
            selected = matching[0]
        public = {key_name: value for key_name, value in selected.items() if key_name != "path"}
        return LatestSnapshotResult(
            status="FOUND",
            snapshot=SnapshotSummary.model_validate_json(canonical_json(public)),
        )

    def verify_storage(self) -> dict[str, Any]:
        self._require_lock()
        report = self.rebuild_index()
        invalid_invalidation_records = report["invalid_invalidation_records"]
        return {
            "status": (
                "PASS" if not report["invalid"] and not invalid_invalidation_records else "FAIL"
            ),
            "valid_snapshots": report["indexed"],
            "invalid_snapshots": report["invalid"],
            "semantically_invalidated_snapshots": report["semantically_invalidated"],
            "invalidation_state_unknown_snapshots": report["invalidation_state_unknown"],
            "invalid_invalidation_records": invalid_invalidation_records,
            "layout_version": self.settings.layout_version,
            "partition_timezone": self.settings.partition_timezone,
        }

    def _summary(
        self,
        manifest: SnapshotManifest,
        *,
        effective_status: Literal[
            "ACTIVE", "SEMANTICALLY_INVALIDATED", "INVALIDATION_STATE_UNKNOWN"
        ] = "ACTIVE",
        invalidation: SnapshotInvalidationRecord | None = None,
    ) -> SnapshotSummary:
        return SnapshotSummary(
            snapshot_id=manifest.snapshot_id,
            store_id=manifest.store_id,
            dataset_type=manifest.dataset_type,
            scope_key=manifest.scope_key,
            captured_at=manifest.captured_at,
            committed_at=manifest.committed_at,
            source=manifest.source,
            quality_status=manifest.quality.status,
            effective_status=effective_status,
            invalidation=invalidation,
            coverage=manifest.coverage.coverage,
            record_count=manifest.record_count,
        )

    def _check_response_size(self, value: Any) -> None:
        if len(canonical_json(value)) > self.max_response_bytes:
            raise ResponseTooLargeError("MCP_RESPONSE_TOO_LARGE")
