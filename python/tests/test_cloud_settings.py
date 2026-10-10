from __future__ import annotations

from pathlib import Path

import pytest

from slipstream.cloud import is_enabled
from slipstream.cloud.settings import (
    CloudConfigError,
    HubSettings,
    SupabaseSettings,
    ca_path,
    env_file_path,
    load_hub,
    load_supabase,
)

HOST = "aws-0-us-east-1.pooler.supabase.com"
REF = "abcdefghij0123456789"
PW = "Pw-0123456789abcdefghijklmnop"
TOKEN = "hf_" + "x" * 34

HOST_KEY = "SLIPSTREAM_SUPABASE_POOLER_HOST"
REF_KEY = "SLIPSTREAM_SUPABASE_PROJECT_REF"
PW_KEY = "SLIPSTREAM_SUPABASE_COLLECTOR_PASSWORD"
HF_KEY = "SLIPSTREAM_HF_TOKEN"

GOOD = {HOST_KEY: HOST, REF_KEY: REF, PW_KEY: PW, HF_KEY: TOKEN}


def _env(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "env"
    path.write_text(text, encoding="utf-8")
    return path


def _lines(**overrides: str | None) -> str:
    values = dict(GOOD)
    for key, value in overrides.items():
        full = {
            "host": HOST_KEY,
            "ref": REF_KEY,
            "password": PW_KEY,
            "token": HF_KEY,
        }[key]
        if value is None:
            del values[full]
        else:
            values[full] = value
    return "".join(f"{key}={value}\n" for key, value in values.items())


def test_loads_all_values(tmp_path: Path) -> None:
    env = _env(tmp_path, _lines())

    supabase = load_supabase(env)
    hub = load_hub(env)

    assert supabase == SupabaseSettings(HOST, REF, PW)
    assert hub == HubSettings(TOKEN)
    assert hub.repo_id == "yandouziyassine/slipstream-recordings"


def test_parser_skips_blank_lines_and_comments(tmp_path: Path) -> None:
    env = _env(tmp_path, "# a comment\n\n   \n" + _lines() + "  # indented comment\n")

    assert load_hub(env).token == TOKEN


@pytest.mark.parametrize("quote", ["'", '"'])
def test_parser_strips_matching_quotes(tmp_path: Path, quote: str) -> None:
    env = _env(tmp_path, _lines(token=f"{quote}{TOKEN}{quote}"))

    assert load_hub(env).token == TOKEN


def test_parser_keeps_mismatched_quotes_and_then_rejects_the_value(tmp_path: Path) -> None:
    env = _env(tmp_path, _lines(token=f"'{TOKEN}\""))

    with pytest.raises(CloudConfigError, match=HF_KEY):
        load_hub(env)


def test_parser_allows_spaces_around_the_equals_sign(tmp_path: Path) -> None:
    env = _env(tmp_path, f"{HF_KEY} = {TOKEN}\n")

    assert load_hub(env).token == TOKEN


def test_parser_accepts_crlf_line_endings(tmp_path: Path) -> None:
    path = tmp_path / "env"
    path.write_bytes(_lines().replace("\n", "\r\n").encode())

    assert load_supabase(path).project_ref == REF


def test_parser_ignores_other_keys_even_when_odd(tmp_path: Path) -> None:
    other = "DEPLOY_NOTE=whatever\nexport OTHER=1\nWEIRD LINE WITHOUT EQUALS\nOTHER=a\nOTHER=b\n"
    env = _env(tmp_path, other + _lines())

    assert load_supabase(env).password == PW
    assert load_hub(env).token == TOKEN


def test_parser_rejects_an_export_line_and_names_the_key(tmp_path: Path) -> None:
    env = _env(tmp_path, f"export {HF_KEY}={TOKEN}\n")

    with pytest.raises(CloudConfigError, match=HF_KEY) as raised:
        load_hub(env)
    assert TOKEN not in str(raised.value)


def test_parser_rejects_a_duplicated_key(tmp_path: Path) -> None:
    env = _env(tmp_path, _lines() + f"{REF_KEY}={REF}\n")

    with pytest.raises(CloudConfigError, match=REF_KEY):
        load_supabase(env)


def test_a_duplicate_in_the_other_service_does_not_break_this_one(tmp_path: Path) -> None:
    env = _env(tmp_path, _lines() + f"{HF_KEY}={TOKEN}\n")

    assert load_supabase(env).project_ref == REF


def test_missing_env_file(tmp_path: Path) -> None:
    with pytest.raises(CloudConfigError, match=r"\.env not found"):
        load_supabase(tmp_path / "missing")
    with pytest.raises(CloudConfigError, match=r"\.env not found"):
        load_hub(tmp_path / "missing")


def test_env_file_that_is_a_directory(tmp_path: Path) -> None:
    with pytest.raises(CloudConfigError, match=r"\.env"):
        load_hub(tmp_path)


@pytest.mark.parametrize(
    ("field", "key"),
    [("host", HOST_KEY), ("ref", REF_KEY), ("password", PW_KEY)],
)
def test_missing_supabase_value_names_the_key(tmp_path: Path, field: str, key: str) -> None:
    env = _env(tmp_path, _lines(**{field: None}))

    with pytest.raises(CloudConfigError, match=key):
        load_supabase(env)


def test_missing_token_names_the_key(tmp_path: Path) -> None:
    env = _env(tmp_path, _lines(token=None))

    with pytest.raises(CloudConfigError, match=HF_KEY):
        load_hub(env)


def test_supabase_loader_does_not_need_the_token(tmp_path: Path) -> None:
    env = _env(tmp_path, _lines(token=None))

    assert load_supabase(env).pooler_host == HOST


def test_hub_loader_does_not_need_the_supabase_values(tmp_path: Path) -> None:
    env = _env(tmp_path, f"{HF_KEY}={TOKEN}\n")

    assert load_hub(env).token == TOKEN


@pytest.mark.parametrize(
    "host",
    [
        "aws-0-us-east-1.pooler.supabase.com",
        "aws-1-eu-central-2.pooler.supabase.com",
        "aws-12-ap-southeast-1.pooler.supabase.com",
    ],
)
def test_host_accepts_pooler_hosts(tmp_path: Path, host: str) -> None:
    assert load_supabase(_env(tmp_path, _lines(host=host))).pooler_host == host


@pytest.mark.parametrize(
    "host",
    [
        "",
        "db.abcdefghij0123456789.supabase.co",
        "evil.example.com",
        "aws-0-us-east-1.pooler.supabase.com.evil.example",
        "evil.example.com/aws-0-us-east-1.pooler.supabase.com",
        "aws-x-us-east-1.pooler.supabase.com",
        "aws--us-east-1.pooler.supabase.com",
        "AWS-0-US-EAST-1.pooler.supabase.com",
        "aws-0-us-east-1.pooler.supabase.com:5432",
        "aws-0-us-east-1.pooler.supabase.com\\x",
        "xaws-0-us-east-1.pooler.supabase.com",
        "aws-0-us east-1.pooler.supabase.com",
    ],
)
def test_host_rejects_anything_else(tmp_path: Path, host: str) -> None:
    with pytest.raises(CloudConfigError, match=HOST_KEY) as raised:
        load_supabase(_env(tmp_path, _lines(host=host)))
    assert not host or host not in str(raised.value)


@pytest.mark.parametrize("ref", ["abcdefghij0123456789", "00000000000000000000", "a" * 20])
def test_project_ref_accepts_twenty_lowercase_alphanumerics(tmp_path: Path, ref: str) -> None:
    assert load_supabase(_env(tmp_path, _lines(ref=ref))).project_ref == ref


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "a" * 19,
        "a" * 21,
        "A" * 20,
        "abcdefghij012345678.",
        "abcdefghij0123456 89",
        "abcdefghij0123456789\\n",
        "abcdefghij.0123456789",
    ],
)
def test_project_ref_rejects_anything_else(tmp_path: Path, ref: str) -> None:
    with pytest.raises(CloudConfigError, match=REF_KEY):
        load_supabase(_env(tmp_path, _lines(ref=ref)))


@pytest.mark.parametrize("password", ["p" * 24, "p" * 64, "Aa1-_.~!@#$%^&*()=+" + "z" * 10])
def test_password_accepts_ascii_of_24_or_more(tmp_path: Path, password: str) -> None:
    assert load_supabase(_env(tmp_path, _lines(password=password))).password == password


@pytest.mark.parametrize(
    "password",
    ["", "p" * 23, "p" * 23 + "é", "p" * 30 + "é", "p" * 12 + " " + "p" * 12],
)
def test_password_rejects_short_non_ascii_or_spaced_values(tmp_path: Path, password: str) -> None:
    with pytest.raises(CloudConfigError, match=PW_KEY) as raised:
        load_supabase(_env(tmp_path, _lines(password=password)))
    assert not password or password not in str(raised.value)


@pytest.mark.parametrize("token", ["hf_" + "a" * 30, "hf_" + "aZ09" * 10, TOKEN])
def test_token_accepts_hf_tokens(tmp_path: Path, token: str) -> None:
    assert load_hub(_env(tmp_path, _lines(token=token))).token == token


@pytest.mark.parametrize(
    "token",
    ["", "hf_" + "a" * 29, "xx_" + "a" * 34, "hf_" + "a" * 20 + "-" + "a" * 20, "HF_" + "a" * 34],
)
def test_token_rejects_anything_else(tmp_path: Path, token: str) -> None:
    with pytest.raises(CloudConfigError, match=HF_KEY) as raised:
        load_hub(_env(tmp_path, _lines(token=token)))
    assert not token or token not in str(raised.value)


def test_error_messages_never_contain_a_secret_value(tmp_path: Path) -> None:
    leaky_value = "short-secret"
    env = _env(tmp_path, _lines(password=leaky_value))

    with pytest.raises(CloudConfigError) as raised:
        load_supabase(env)

    assert leaky_value not in str(raised.value)
    assert leaky_value not in repr(raised.value)


def test_repr_never_contains_the_password_or_token() -> None:
    supabase = SupabaseSettings(HOST, REF, PW)
    hub = HubSettings(TOKEN)

    assert PW not in repr(supabase)
    assert PW not in str(supabase)
    assert TOKEN not in repr(hub)
    assert TOKEN not in str(hub)
    assert HOST in repr(supabase)


def test_user_includes_the_project_ref() -> None:
    assert SupabaseSettings(HOST, REF, PW).user == f"slipstream_collector.{REF}"


def test_env_file_path_honours_the_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SLIPSTREAM_ENV_FILE", str(tmp_path / "custom.env"))

    assert env_file_path() == tmp_path / "custom.env"


def test_env_file_path_defaults_to_the_repo_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLIPSTREAM_ENV_FILE", raising=False)

    path = env_file_path()

    assert path.name == ".env"
    assert (path.parent / "CLAUDE.md").exists()


def test_env_file_path_ignores_an_empty_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLIPSTREAM_ENV_FILE", "")

    assert env_file_path().name == ".env"


def test_ca_path(tmp_path: Path) -> None:
    assert ca_path(tmp_path) == tmp_path / "cloud" / "supabase-ca.crt"


def test_is_enabled_follows_the_flag_file(tmp_path: Path) -> None:
    assert not is_enabled(tmp_path)

    (tmp_path / "cloud.enabled").touch()

    assert is_enabled(tmp_path)


def test_loading_settings_does_not_touch_the_process_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import os

    before = dict(os.environ)
    env = _env(tmp_path, _lines())

    load_supabase(env)
    load_hub(env)

    assert dict(os.environ) == before
