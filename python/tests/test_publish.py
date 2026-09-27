from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from slipstream.site.publish import is_enabled, publish

_GIT_TIMEOUT_S = 30


@pytest.fixture
def log() -> logging.Logger:
    logger = logging.getLogger("test-publish")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return logger


@pytest.fixture
def bare_remote(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", str(remote)],  # noqa: S607
        check=True,
        timeout=_GIT_TIMEOUT_S,
    )
    return remote


@pytest.fixture
def site_dir(tmp_path: Path) -> Path:
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<html>v1</html>", encoding="utf-8")
    return site


@pytest.fixture
def data_dir(tmp_path: Path, bare_remote: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("SLIPSTREAM_PUBLISH_REMOTE", str(bare_remote))
    return data


def _clone_and_read(remote: Path, dest: Path) -> str:
    subprocess.run(
        ["git", "clone", "-q", "--branch", "main", str(remote), str(dest)],  # noqa: S607
        check=True,
        timeout=_GIT_TIMEOUT_S,
    )
    return (dest / "index.html").read_text(encoding="utf-8")


def test_publish_is_disabled_without_the_flag_file(
    site_dir: Path, data_dir: Path, log: logging.Logger
) -> None:
    assert not is_enabled(data_dir)
    result = publish(site_dir, data_dir, log)
    assert result == 0
    assert not (data_dir / "publish-repo").exists()


def test_first_publish_creates_a_commit(
    site_dir: Path, data_dir: Path, bare_remote: Path, log: logging.Logger, tmp_path: Path
) -> None:
    (data_dir / "publish.enabled").touch()

    result = publish(site_dir, data_dir, log)

    assert result == 0
    content = _clone_and_read(bare_remote, tmp_path / "check")
    assert content == "<html>v1</html>"


def test_second_publish_with_no_changes_creates_no_commit(
    site_dir: Path, data_dir: Path, bare_remote: Path, log: logging.Logger
) -> None:
    (data_dir / "publish.enabled").touch()
    publish(site_dir, data_dir, log)
    log_before = subprocess.run(
        ["git", "log", "--format=%H"],  # noqa: S607
        cwd=data_dir / "publish-repo",
        capture_output=True,
        text=True,
        check=True,
        timeout=_GIT_TIMEOUT_S,
    ).stdout

    result = publish(site_dir, data_dir, log)

    log_after = subprocess.run(
        ["git", "log", "--format=%H"],  # noqa: S607
        cwd=data_dir / "publish-repo",
        capture_output=True,
        text=True,
        check=True,
        timeout=_GIT_TIMEOUT_S,
    ).stdout
    assert result == 0
    assert log_before == log_after
    assert log_before.count("\n") == 1


def test_a_changed_file_produces_a_second_commit(
    site_dir: Path, data_dir: Path, bare_remote: Path, log: logging.Logger, tmp_path: Path
) -> None:
    (data_dir / "publish.enabled").touch()
    publish(site_dir, data_dir, log)
    (site_dir / "index.html").write_text("<html>v2</html>", encoding="utf-8")

    result = publish(site_dir, data_dir, log)

    assert result == 0
    content = _clone_and_read(bare_remote, tmp_path / "check2")
    assert content == "<html>v2</html>"


def test_publish_never_logs_the_key_path(
    site_dir: Path, data_dir: Path, log: logging.Logger, caplog: pytest.LogCaptureFixture
) -> None:
    (data_dir / "publish.enabled").touch()
    with caplog.at_level(logging.DEBUG, logger="test-publish"):
        publish(site_dir, data_dir, log)
    for record in caplog.records:
        assert "slipstream_live_deploy" not in record.getMessage()
        assert ".ssh" not in record.getMessage()


def test_publish_failure_returns_nonzero_and_never_raises(
    site_dir: Path, tmp_path: Path, log: logging.Logger, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "publish.enabled").touch()
    monkeypatch.setenv("SLIPSTREAM_PUBLISH_REMOTE", str(tmp_path / "no-such-remote.git"))

    result = publish(site_dir, data, log)

    assert result == 1
