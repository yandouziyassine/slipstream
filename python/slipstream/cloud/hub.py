from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from slipstream.cloud.settings import HubSettings
from slipstream.redaction import redact

MAX_FILES_PER_COMMIT = 97
_REPO_PREFIXES = frozenset({"recordings", "fills", "manifests"})
_SEGMENT = re.compile(r"[A-Za-z0-9._-]+")
_COMMIT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ANY_HF_TOKEN = re.compile(r"hf_[A-Za-z0-9]+")


@dataclass(frozen=True)
class RemoteFile:
    path: str
    size: int
    sha256: str | None
    blob_id: str


class HubError(RuntimeError):
    """A Hub call failed. The message is already redacted: no token, no local path."""


class HubClient(Protocol):
    def head(self) -> str: ...

    def paths_info(self, paths: Sequence[str], revision: str) -> dict[str, RemoteFile]: ...

    def commit(self, files: Sequence[tuple[str, Path]], message: str, parent: str) -> str: ...


class HfApiLike(Protocol):
    """The slice of `huggingface_hub.HfApi` this module uses."""

    def repo_info(self, repo_id: str, *, repo_type: str, token: str) -> Any: ...

    def get_paths_info(
        self, repo_id: str, paths: list[str], *, revision: str, repo_type: str, token: str
    ) -> Any: ...

    def create_commit(
        self,
        repo_id: str,
        operations: list[Any],
        *,
        commit_message: str,
        repo_type: str,
        parent_commit: str,
        token: str,
    ) -> Any: ...


def _check_repo_path(path: str) -> None:
    segments = path.split("/")
    if (
        len(segments) < 2
        or segments[0] not in _REPO_PREFIXES
        or any(_SEGMENT.fullmatch(part) is None or part in (".", "..") for part in segments)
    ):
        raise HubError("refused a repo path outside recordings/, fills/ and manifests/")


def _check_commit_id(value: object, what: str) -> str:
    if not isinstance(value, str) or _COMMIT_ID.fullmatch(value) is None:
        raise HubError(f"{what} is not a commit id")
    return value


class HfHubClient:
    """The only place that talks to Hugging Face. Every call passes the token and
    `repo_type="dataset"` explicitly; nothing is read from or written to the environment."""

    def __init__(self, settings: HubSettings, api: HfApiLike | None = None) -> None:
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        self._token = settings.token
        self._repo_id = settings.repo_id
        self._api = api

    def head(self) -> str:
        try:
            info = self._client().repo_info(self._repo_id, repo_type="dataset", token=self._token)
            sha = getattr(info, "sha", None)
        except Exception as exc:
            raise self._wrap(exc) from None
        return _check_commit_id(sha, "the dataset head")

    def paths_info(self, paths: Sequence[str], revision: str) -> dict[str, RemoteFile]:
        for path in paths:
            _check_repo_path(path)
        _check_commit_id(revision, "revision")
        if not paths:
            return {}
        wanted = set(paths)
        try:
            from huggingface_hub.hf_api import RepoFile

            entries = self._client().get_paths_info(
                self._repo_id,
                list(paths),
                revision=revision,
                repo_type="dataset",
                token=self._token,
            )
            found: dict[str, RemoteFile] = {}
            for entry in entries:
                if isinstance(entry, RepoFile) and entry.path in wanted:
                    found[entry.path] = self._remote_file(entry)
        except HubError:
            raise
        except Exception as exc:
            raise self._wrap(exc) from None
        return found

    def commit(self, files: Sequence[tuple[str, Path]], message: str, parent: str) -> str:
        if not files:
            raise HubError("nothing to commit")
        if len(files) > MAX_FILES_PER_COMMIT:
            raise HubError(f"refused a commit of more than {MAX_FILES_PER_COMMIT} files")
        repo_paths = [path for path, _ in files]
        for path in repo_paths:
            _check_repo_path(path)
        if len(set(repo_paths)) != len(repo_paths):
            raise HubError("refused a commit that names the same repo path twice")
        _check_commit_id(parent, "parent")
        try:
            from huggingface_hub import CommitOperationAdd

            operations: list[Any] = [CommitOperationAdd(path, local) for path, local in files]
            info = self._client().create_commit(
                self._repo_id,
                operations,
                commit_message=message,
                repo_type="dataset",
                parent_commit=parent,
                token=self._token,
            )
            oid = getattr(info, "oid", None)
        except Exception as exc:
            raise self._wrap(exc) from None
        return _check_commit_id(oid, "the new commit")

    def _client(self) -> HfApiLike:
        if self._api is None:
            from huggingface_hub import HfApi

            self._api = HfApi(token=self._token)
        return self._api

    @staticmethod
    def _remote_file(entry: Any) -> RemoteFile:
        size = entry.size
        blob_id = entry.blob_id
        lfs = entry.lfs
        sha256 = None if lfs is None else lfs.sha256
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(blob_id, str)
            or not blob_id
            or (
                sha256 is not None
                and (not isinstance(sha256, str) or not _SHA256.fullmatch(sha256))
            )
        ):
            raise HubError("the Hub returned malformed file metadata")
        return RemoteFile(entry.path, size, sha256, blob_id)

    def _wrap(self, exc: Exception) -> HubError:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        where = f" (HTTP {status})" if isinstance(status, int) else ""
        text = redact(_ANY_HF_TOKEN.sub("<redacted>", str(exc)), [self._token], limit=200)
        return HubError(f"{type(exc).__name__}{where}: {text}")
