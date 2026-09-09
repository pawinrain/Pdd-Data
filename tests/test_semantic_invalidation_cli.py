from __future__ import annotations

from pathlib import Path

import pytest

from pdd_data_mcp.cli import build_parser, main


def invalidation_args(config: Path) -> list[str]:
    return [
        "invalidate-snapshot",
        "--config",
        str(config),
        "--snapshot-id",
        "s_0123456789abcdef0123456789abcdef",
        "--reason-code",
        "METRIC_WINDOW_END_MISLABELED",
        "--replacement-scope-version",
        "d4-account-v3",
    ]


def test_invalidation_cli_requires_explicit_confirmation_before_config_read(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing_config = tmp_path / "does-not-exist.toml"

    with pytest.raises(SystemExit) as caught:
        main(invalidation_args(missing_config))

    assert caught.value.code == 2
    assert capsys.readouterr().err.strip() == "--confirm-semantic-invalidation is required"


def test_invalidation_cli_locks_reason_to_reviewed_semantic_error(tmp_path: Path) -> None:
    parser = build_parser()
    args = invalidation_args(tmp_path / "config.toml")
    args[args.index("METRIC_WINDOW_END_MISLABELED")] = "UNREVIEWED_REASON"

    with pytest.raises(SystemExit):
        parser.parse_args(args)
