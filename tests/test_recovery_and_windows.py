from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import make_repository, write_test_config
from test_storage import commit_dataset

from pdd_data_mcp.contracts.models import DatasetType
from pdd_data_mcp.errors import LockBusyError, SnapshotNotFoundError, StorageError


@pytest.mark.parametrize("stage", ["after-body", "after-manifest"])
def test_child_process_interruption_is_quarantined_and_marked_interrupted(
    tmp_path: Path, stage: str
) -> None:
    config_path = tmp_path / "config.toml"
    config = write_test_config(config_path)
    worker = Path(__file__).parent / "helpers" / "fault_worker.py"
    completed = subprocess.run(
        [sys.executable, str(worker), str(config_path), stage],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        check=False,
        timeout=15,
    )
    assert completed.returncode == 91
    repository = make_repository(config)
    with repository.service_lock():
        repository.initialize()
        report = repository.recover()
        assert report["interrupted_requests"] == 1
        assert len(report["quarantined"]) == 1
        request_path = next((config.storage.data_root / "_requests").rglob("*.json"))
        request = json.loads(request_path.read_text(encoding="utf-8"))
        assert request["status"] == "INTERRUPTED"
        assert request["error_code"] == "PROCESS_INTERRUPTED"
        assert list((config.storage.data_root / "_tmp").iterdir()) == []


def test_missing_commit_is_quarantined_and_invisible(tmp_path: Path) -> None:
    config = write_test_config(tmp_path / "config.toml")
    repository = make_repository(config)
    with repository.service_lock():
        repository.initialize()
        manifest, _, _, _ = asyncio.run(
            commit_dataset(repository, DatasetType.STORE_OVERVIEW, idempotency_key="missing-commit")
        )
        directory = next(config.storage.data_root.rglob(f"*{manifest.snapshot_id}"))
        (directory / "COMMIT.json").unlink()
    restarted = make_repository(config)
    with restarted.service_lock():
        restarted.initialize()
        report = restarted.recover()
        assert len(report["quarantined"]) == 1
        with pytest.raises(SnapshotNotFoundError):
            restarted.read_snapshot(snapshot_id=manifest.snapshot_id, cursor=None, page_size=20)


def test_recovery_repairs_commit_that_preceded_request_record_update(tmp_path: Path) -> None:
    config = write_test_config(tmp_path / "config.toml")
    repository = make_repository(config)
    with repository.service_lock():
        repository.initialize()
        manifest, _, reservation, _ = asyncio.run(
            commit_dataset(repository, DatasetType.INVENTORY, idempotency_key="recover-commit")
        )
        request_path = config.storage.data_root / reservation.request_file
        record = json.loads(request_path.read_text(encoding="utf-8"))
        record["status"] = "RUNNING"
        request_path.write_text(json.dumps(record), encoding="utf-8")
    restarted = make_repository(config)
    with restarted.service_lock():
        restarted.initialize()
        report = restarted.recover()
        assert report["recovered_commits"] == 1
        repaired = json.loads(request_path.read_text(encoding="utf-8"))
        assert repaired["status"] == "COMMITTED"
        assert restarted.get_manifest(manifest.snapshot_id).snapshot_id == manifest.snapshot_id


def test_single_writer_lock_contends_across_processes(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config = write_test_config(config_path)
    helper = Path(__file__).parent / "helpers" / "hold_lock.py"
    process = subprocess.Popen(
        [sys.executable, str(helper), str(config_path)],
        cwd=Path(__file__).resolve().parents[1],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "READY"
        repository = make_repository(config)
        with pytest.raises(LockBusyError), repository.service_lock(timeout=0):
            pass
    finally:
        assert process.stdin is not None
        process.stdin.write("\n")
        process.stdin.flush()
        process.wait(timeout=10)
    assert process.returncode == 0


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows file sharing semantics")
def test_windows_file_occupation_is_reported_as_storage_failure(tmp_path: Path) -> None:
    import win32con
    import win32file

    config = write_test_config(tmp_path / "config.toml")
    repository = make_repository(config)
    with repository.service_lock():
        repository.initialize()
        _, _, reservation, _ = asyncio.run(
            commit_dataset(repository, DatasetType.INVENTORY, idempotency_key="occupied-seed")
        )
        request_path = config.storage.data_root / reservation.request_file
        handle = win32file.CreateFile(
            str(request_path),
            win32con.GENERIC_READ,
            0,
            None,
            win32con.OPEN_EXISTING,
            win32con.FILE_ATTRIBUTE_NORMAL,
            None,
        )
        try:
            with pytest.raises(StorageError):
                repository.fail_request(reservation, "INJECTED_AFTER_COMMIT")
        finally:
            handle.Close()
