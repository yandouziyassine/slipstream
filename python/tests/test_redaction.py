from __future__ import annotations

import pytest

from slipstream.redaction import redact, redact_paths


@pytest.mark.parametrize(
    "text",
    [
        "[Errno 2] No such file or directory: '/home/alice/slipstream-data/recordings/x.gz'",
        "cannot open /mnt/c/Users/alice/slipstream/build/engine",
        r"cannot open C:\Users\alice\slipstream\x.log",
    ],
)
def test_redact_paths_hides_local_paths(text: str) -> None:
    out = redact_paths(text)

    assert "alice" not in out
    assert "<path>" in out


def test_redact_paths_keeps_urls_readable() -> None:
    text = "feed closed: wss://ws.kraken.com/v2"

    assert redact_paths(text) == text


def test_redact_paths_keeps_closing_tags() -> None:
    assert redact_paths("<b>x</b>") == "<b>x</b>"


def test_redact_replaces_every_secret() -> None:
    assert redact("x hf_abc y hf_abc z", ["hf_abc"]) == "x <redacted> y <redacted> z"


def test_redact_replaces_several_secrets() -> None:
    assert redact("a one b two c", ["one", "two"]) == "a <redacted> b <redacted> c"


def test_redact_ignores_empty_secrets() -> None:
    assert redact("plain text", ["", ""]) == "plain text"


def test_redact_prefers_the_longer_secret_when_one_contains_the_other() -> None:
    assert redact("tok-abcdef", ["tok-abc", "tok-abcdef"]) == "<redacted>"


def test_redact_treats_a_path_shaped_secret_as_a_secret() -> None:
    out = redact("bad value /home/alice/secret here", ["/home/alice/secret"])

    assert out == "bad value <redacted> here"


def test_redact_also_hides_paths_that_are_not_secrets() -> None:
    out = redact("cannot open /home/alice/x.log with tok", ["tok"])

    assert out == "cannot open <path> with <redacted>"


def test_redact_truncates_to_the_limit() -> None:
    assert redact("a" * 1000, []) == "a" * 300
    assert redact("abcdef", [], limit=3) == "abc"


def test_redact_truncates_after_redacting() -> None:
    secret = "s" * 50
    out = redact(f"{secret} tail", [secret], limit=12)

    assert out == "<redacted> t"
    assert secret not in out
