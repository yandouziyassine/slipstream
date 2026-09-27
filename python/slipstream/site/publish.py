from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

_REMOTE_ENV = "SLIPSTREAM_PUBLISH_REMOTE"
_DEFAULT_REMOTE = "git@github.com:yandouziyassine/slipstream-live.git"
_DEPLOY_KEY = Path.home() / ".ssh" / "slipstream_live_deploy"
_BRANCH = "main"
_COMMIT_AUTHOR_NAME = "slipstream-bot"
_COMMIT_AUTHOR_EMAIL = "160782497+yandouziyassine@users.noreply.github.com"
_GIT_TIMEOUT_S = 60


def is_enabled(data_dir: Path) -> bool:
    return (data_dir / "publish.enabled").exists()


def _remote_url() -> str:
    return os.environ.get(_REMOTE_ENV) or _DEFAULT_REMOTE


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_SSH_COMMAND"] = (
        f"ssh -i {_DEPLOY_KEY} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
    )
    env["GIT_AUTHOR_NAME"] = _COMMIT_AUTHOR_NAME
    env["GIT_AUTHOR_EMAIL"] = _COMMIT_AUTHOR_EMAIL
    env["GIT_COMMITTER_NAME"] = _COMMIT_AUTHOR_NAME
    env["GIT_COMMITTER_EMAIL"] = _COMMIT_AUTHOR_EMAIL
    return env


def _run_git(args: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is not on PATH")
    return subprocess.run(  # noqa: S603 - fixed argv, resolved executable, no shell
        [git, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_S,
        check=True,
    )


def _sync_tree(site_dir: Path, clone_dir: Path) -> None:
    for entry in clone_dir.iterdir():
        if entry.name == ".git":
            continue
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    for entry in site_dir.iterdir():
        dest = clone_dir / entry.name
        if entry.is_dir():
            shutil.copytree(entry, dest)
        else:
            shutil.copy2(entry, dest)


def _has_staged_changes(clone_dir: Path, env: dict[str, str]) -> bool:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is not on PATH")
    result = subprocess.run(  # noqa: S603 - fixed argv, resolved executable, no shell
        [git, "diff", "--cached", "--quiet"],
        cwd=clone_dir,
        env=env,
        capture_output=True,
        timeout=_GIT_TIMEOUT_S,
        check=False,
    )
    return result.returncode != 0


def _failure_summary(exc: Exception) -> str:
    # Never the exception text or git's stderr: ssh can name the deploy key's path there.
    if isinstance(exc, subprocess.CalledProcessError):
        return f"git {exc.cmd[1]} exited with {exc.returncode}"
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"git {exc.cmd[1]} timed out"
    return type(exc).__name__


def publish(site_dir: Path, data_dir: Path, log: logging.Logger) -> int:
    """Publishes site_dir to the slipstream-live repo. Never raises: any failure is logged and
    returns nonzero. The SSH key path is used but its path or contents are never logged."""
    if not is_enabled(data_dir):
        log.info("publishing disabled", extra={"fields": {"event": "publish_disabled"}})
        return 0
    clone_dir = data_dir / "publish-repo"
    env = _git_env()
    try:
        if not clone_dir.exists():
            clone_dir.parent.mkdir(parents=True, exist_ok=True)
            _run_git(["clone", _remote_url(), str(clone_dir)], cwd=clone_dir.parent, env=env)
        _run_git(["symbolic-ref", "HEAD", f"refs/heads/{_BRANCH}"], cwd=clone_dir, env=env)
        _sync_tree(site_dir, clone_dir)
        _run_git(["add", "-A"], cwd=clone_dir, env=env)
        if not _has_staged_changes(clone_dir, env):
            log.info("publish: nothing changed", extra={"fields": {"event": "publish_no_change"}})
            return 0
        _run_git(["commit", "-m", "publish: update research site"], cwd=clone_dir, env=env)
        _run_git(["push", "origin", f"HEAD:refs/heads/{_BRANCH}"], cwd=clone_dir, env=env)
    except Exception as exc:  # publish must never raise into the hourly collector
        log.error(
            f"publish failed: {_failure_summary(exc)}",
            extra={"fields": {"event": "publish_failed"}},
        )
        return 1
    log.info("publish: pushed a new commit", extra={"fields": {"event": "publish_pushed"}})
    return 0
