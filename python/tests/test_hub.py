from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from huggingface_hub import CommitOperationAdd
from huggingface_hub.errors import HfHubHTTPError
from huggingface_hub.hf_api import RepoFile, RepoFolder

from slipstream.cloud.hub import HfHubClient, HubError, RemoteFile
from slipstream.cloud.settings import HubSettings

TOKEN = "hf_" + "x" * 34
REPO = "yandouziyassine/slipstream-recordings"
HEAD = "1" * 40
PARENT = "2" * 40
NEW_OID = "3" * 40
SHA = "ab" * 32


class StubApi:
    """Records every call and returns canned answers, or raises `fail`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.info = SimpleNamespace(sha=HEAD)
        self.entries: list[Any] = []
        self.commit_result: Any = SimpleNamespace(oid=NEW_OID)
        self.fail: BaseException | None = None

    def _call(self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        self.calls.append((name, args, kwargs))
        if self.fail is not None:
            raise self.fail

    def repo_info(self, *args: Any, **kwargs: Any) -> Any:
        self._call("repo_info", args, kwargs)
        return self.info

    def get_paths_info(self, *args: Any, **kwargs: Any) -> Any:
        self._call("get_paths_info", args, kwargs)
        return self.entries

    def create_commit(self, *args: Any, **kwargs: Any) -> Any:
        self._call("create_commit", args, kwargs)
        return self.commit_result


@pytest.fixture
def api() -> StubApi:
    return StubApi()


@pytest.fixture
def client(api: StubApi) -> HfHubClient:
    return HfHubClient(HubSettings(TOKEN), api)


def _lfs_file(path: str, size: int = 10, sha: str = SHA) -> RepoFile:
    return RepoFile(
        path=path, size=size, oid="f" * 40, lfs={"size": size, "oid": sha, "pointerSize": 134}
    )


def _git_file(path: str, size: int = 5, oid: str = "e" * 40) -> RepoFile:
    return RepoFile(path=path, size=size, oid=oid)


def _local(tmp_path: Path, name: str = "a.bin") -> Path:
    path = tmp_path / name
    path.write_bytes(b"data")
    return path


def test_head_returns_the_dataset_sha(client: HfHubClient, api: StubApi) -> None:
    assert client.head() == HEAD

    name, args, kwargs = api.calls[0]
    assert name == "repo_info"
    assert args == (REPO,)
    assert kwargs["repo_type"] == "dataset"
    assert kwargs["token"] == TOKEN


@pytest.mark.parametrize("sha", [None, "", "not-a-sha", 12345, "G" * 40])
def test_head_rejects_an_unexpected_sha(client: HfHubClient, api: StubApi, sha: Any) -> None:
    api.info = SimpleNamespace(sha=sha)

    with pytest.raises(HubError):
        client.head()


def test_paths_info_passes_revision_and_repo_type(client: HfHubClient, api: StubApi) -> None:
    api.entries = [_lfs_file("recordings/2026/09/27/10-0p01.jsonl.gz")]

    client.paths_info(["recordings/2026/09/27/10-0p01.jsonl.gz"], NEW_OID)

    name, args, kwargs = api.calls[0]
    assert name == "get_paths_info"
    assert args == (REPO, ["recordings/2026/09/27/10-0p01.jsonl.gz"])
    assert kwargs == {"revision": NEW_OID, "repo_type": "dataset", "token": TOKEN}


def test_paths_info_maps_lfs_and_git_files(client: HfHubClient, api: StubApi) -> None:
    api.entries = [
        _lfs_file("recordings/2026/09/27/10-0p01.jsonl.gz", size=2831044),
        _git_file("manifests/2026/09/27.jsonl", size=77, oid="d" * 40),
    ]

    found = client.paths_info(
        ["recordings/2026/09/27/10-0p01.jsonl.gz", "manifests/2026/09/27.jsonl"], HEAD
    )

    assert found == {
        "recordings/2026/09/27/10-0p01.jsonl.gz": RemoteFile(
            "recordings/2026/09/27/10-0p01.jsonl.gz", 2831044, SHA, "f" * 40
        ),
        "manifests/2026/09/27.jsonl": RemoteFile("manifests/2026/09/27.jsonl", 77, None, "d" * 40),
    }


def test_paths_info_ignores_folders_and_unrequested_paths(
    client: HfHubClient, api: StubApi
) -> None:
    api.entries = [
        RepoFolder(path="recordings/2026", oid="c" * 40),
        _git_file("manifests/2026/09/27.jsonl"),
        _git_file("manifests/other.jsonl"),
    ]

    found = client.paths_info(["recordings/2026", "manifests/2026/09/27.jsonl"], HEAD)

    assert list(found) == ["manifests/2026/09/27.jsonl"]


def test_paths_info_with_no_paths_makes_no_call(client: HfHubClient, api: StubApi) -> None:
    assert client.paths_info([], HEAD) == {}
    assert api.calls == []


@pytest.mark.parametrize("revision", ["", "main", "../x", "1" * 39, "refs/pr/1"])
def test_paths_info_rejects_a_revision_that_is_not_a_commit_id(
    client: HfHubClient, api: StubApi, revision: str
) -> None:
    with pytest.raises(HubError):
        client.paths_info(["fills/2026/09/27.csv.gz"], revision)
    assert api.calls == []


def test_paths_info_rejects_bad_repo_paths(client: HfHubClient, api: StubApi) -> None:
    with pytest.raises(HubError):
        client.paths_info(["/etc/passwd"], HEAD)
    assert api.calls == []


def test_paths_info_rejects_a_malformed_answer(client: HfHubClient, api: StubApi) -> None:
    api.entries = [RepoFile(path="fills/2026/09/27.csv.gz", size=-1, oid="e" * 40)]

    with pytest.raises(HubError):
        client.paths_info(["fills/2026/09/27.csv.gz"], HEAD)


def test_commit_builds_one_add_operation_per_file(
    client: HfHubClient, api: StubApi, tmp_path: Path
) -> None:
    first = _local(tmp_path, "a.bin")
    second = _local(tmp_path, "b.bin")

    oid = client.commit(
        [
            ("recordings/2026/09/27/10-0p01.jsonl.gz", first),
            ("manifests/2026/09/27.jsonl", second),
        ],
        "archive 2026-09-27: 1 recordings, 3 fills",
        PARENT,
    )

    assert oid == NEW_OID
    name, args, kwargs = api.calls[0]
    assert name == "create_commit"
    assert args[0] == REPO
    operations = args[1]
    assert all(isinstance(op, CommitOperationAdd) for op in operations)
    assert [(op.path_in_repo, Path(op.path_or_fileobj)) for op in operations] == [
        ("recordings/2026/09/27/10-0p01.jsonl.gz", first),
        ("manifests/2026/09/27.jsonl", second),
    ]
    assert kwargs["parent_commit"] == PARENT
    assert kwargs["repo_type"] == "dataset"
    assert kwargs["token"] == TOKEN
    assert kwargs["commit_message"] == "archive 2026-09-27: 1 recordings, 3 fills"


def test_commit_accepts_97_files_and_refuses_98(
    client: HfHubClient, api: StubApi, tmp_path: Path
) -> None:
    local = _local(tmp_path)
    ok = [(f"recordings/2026/09/27/{i:02d}-0p01.jsonl.gz", local) for i in range(97)]

    client.commit(ok, "m", PARENT)

    assert len(api.calls) == 1
    too_many = [*ok, ("fills/2026/09/27.csv.gz", local)]
    with pytest.raises(HubError, match="97"):
        client.commit(too_many, "m", PARENT)
    assert len(api.calls) == 1


def test_commit_refuses_an_empty_commit(client: HfHubClient, api: StubApi) -> None:
    with pytest.raises(HubError):
        client.commit([], "m", PARENT)
    assert api.calls == []


@pytest.mark.parametrize(
    "path",
    [
        "/recordings/x.gz",
        "recordings/../../etc/passwd",
        "recordings/../fills/x.csv.gz",
        "recordings\\x.gz",
        "recordings/a\\b.gz",
        "README.md",
        ".git/config",
        "other/x.gz",
        "recordings",
        "recordings/",
        "recordings//x.gz",
        "recordings/./x.gz",
        "recordings/x\x00.gz",
        "recordings/x\n.gz",
        "C:/recordings/x.gz",
        "recordings/x y.gz",
        "",
        "../recordings/x.gz",
    ],
)
def test_commit_refuses_unsafe_repo_paths(
    client: HfHubClient, api: StubApi, tmp_path: Path, path: str
) -> None:
    with pytest.raises(HubError):
        client.commit([(path, _local(tmp_path))], "m", PARENT)
    assert api.calls == []


def test_commit_refuses_duplicate_repo_paths(
    client: HfHubClient, api: StubApi, tmp_path: Path
) -> None:
    local = _local(tmp_path)

    with pytest.raises(HubError):
        client.commit([("fills/a.csv.gz", local), ("fills/a.csv.gz", local)], "m", PARENT)
    assert api.calls == []


@pytest.mark.parametrize("parent", ["", "main", "1" * 39, "../x"])
def test_commit_refuses_a_parent_that_is_not_a_commit_id(
    client: HfHubClient, api: StubApi, tmp_path: Path, parent: str
) -> None:
    with pytest.raises(HubError):
        client.commit([("fills/a.csv.gz", _local(tmp_path))], "m", parent)
    assert api.calls == []


@pytest.mark.parametrize("result", [SimpleNamespace(oid=""), SimpleNamespace(oid=5), object()])
def test_commit_rejects_an_answer_without_a_commit_id(
    client: HfHubClient, api: StubApi, tmp_path: Path, result: Any
) -> None:
    api.commit_result = result

    with pytest.raises(HubError):
        client.commit([("fills/a.csv.gz", _local(tmp_path))], "m", PARENT)


def test_a_missing_local_file_becomes_a_hub_error_without_the_local_path(
    client: HfHubClient, tmp_path: Path
) -> None:
    missing = tmp_path / "private-dir" / "nope.bin"

    with pytest.raises(HubError) as raised:
        client.commit([("fills/a.csv.gz", missing)], "m", PARENT)

    assert str(tmp_path) not in str(raised.value)


def _http_error(status: int, message: str) -> HfHubHTTPError:
    response = SimpleNamespace(headers={}, request=None, status_code=status)
    return HfHubHTTPError(message, response=response)  # type: ignore[arg-type]


@pytest.mark.parametrize("call", ["head", "paths_info", "commit"])
def test_http_errors_become_hub_errors_without_the_token(
    client: HfHubClient, api: StubApi, tmp_path: Path, call: str
) -> None:
    api.fail = _http_error(401, f"401 Client Error: invalid Bearer {TOKEN} for url")
    local = _local(tmp_path)

    with pytest.raises(HubError) as raised:
        if call == "head":
            client.head()
        elif call == "paths_info":
            client.paths_info(["fills/a.csv.gz"], HEAD)
        else:
            client.commit([("fills/a.csv.gz", local)], "m", PARENT)

    text = str(raised.value)
    assert TOKEN not in text
    assert "HfHubHTTPError" in text
    assert "401" in text
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__


def test_os_errors_become_hub_errors_without_the_token_or_paths(
    client: HfHubClient, api: StubApi
) -> None:
    api.fail = OSError(f"connection to https://x/?token={TOKEN} failed at /home/alice/x.log")

    with pytest.raises(HubError) as raised:
        client.head()

    text = str(raised.value)
    assert TOKEN not in text
    assert "alice" not in text
    assert "OSError" in text


def test_any_other_hf_token_shape_is_scrubbed_too(client: HfHubClient, api: StubApi) -> None:
    other = "hf_" + "Z" * 40
    api.fail = RuntimeError(f"bad {other}")

    with pytest.raises(HubError) as raised:
        client.head()

    assert other not in str(raised.value)


def test_hub_error_messages_are_bounded(client: HfHubClient, api: StubApi) -> None:
    api.fail = RuntimeError("x" * 5000)

    with pytest.raises(HubError) as raised:
        client.head()

    assert len(str(raised.value)) < 500


def test_keyboard_interrupt_is_not_swallowed(client: HfHubClient, api: StubApi) -> None:
    api.fail = KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        client.head()


def test_the_constructor_disables_telemetry(monkeypatch: pytest.MonkeyPatch, api: StubApi) -> None:
    monkeypatch.setenv("HF_HUB_DISABLE_TELEMETRY", "placeholder")
    monkeypatch.delenv("HF_HUB_DISABLE_TELEMETRY")

    HfHubClient(HubSettings(TOKEN), api)

    assert os.environ["HF_HUB_DISABLE_TELEMETRY"] == "1"


def test_the_constructor_keeps_an_explicit_telemetry_setting(
    monkeypatch: pytest.MonkeyPatch, api: StubApi
) -> None:
    monkeypatch.setenv("HF_HUB_DISABLE_TELEMETRY", "0")

    HfHubClient(HubSettings(TOKEN), api)

    assert os.environ["HF_HUB_DISABLE_TELEMETRY"] == "0"


def test_the_client_never_exports_the_token(api: StubApi) -> None:
    client = HfHubClient(HubSettings(TOKEN), api)
    client.head()

    assert TOKEN not in os.environ.values()
    assert TOKEN not in repr(client)


def test_the_real_api_is_built_lazily_with_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    import huggingface_hub

    built: list[dict[str, Any]] = []

    class FakeHfApi:
        def __init__(self, **kwargs: Any) -> None:
            built.append(kwargs)

        def repo_info(self, *args: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(sha=HEAD)

    monkeypatch.setattr(huggingface_hub, "HfApi", FakeHfApi)

    client = HfHubClient(HubSettings(TOKEN))
    assert built == []

    assert client.head() == HEAD
    assert built == [{"token": TOKEN}]
    client.head()
    assert len(built) == 1


def test_a_custom_repo_id_is_used(api: StubApi) -> None:
    HfHubClient(HubSettings(TOKEN, repo_id="someone/else"), api).head()

    assert api.calls[0][1] == ("someone/else",)
