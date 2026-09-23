import pytest

from slipstream.config import ConfigError, load_settings, validate_engine_address


def test_defaults() -> None:
    assert load_settings({}).engine_address == "127.0.0.1:50051"


def test_live_mode_is_refused() -> None:
    with pytest.raises(ConfigError, match="live trading"):
        load_settings({"SLIPSTREAM_PAPER_MODE": "false"})


def test_paper_mode_value_is_normalised() -> None:
    load_settings({"SLIPSTREAM_PAPER_MODE": " TRUE "})


def test_engine_address_from_env() -> None:
    env = {"SLIPSTREAM_ENGINE_ADDRESS": "localhost:6000"}
    assert load_settings(env).engine_address == "localhost:6000"


@pytest.mark.parametrize("address", ["127.0.0.1:50051", "localhost:1", "[::1]:65535"])
def test_accepts_loopback(address: str) -> None:
    validate_engine_address(address)


@pytest.mark.parametrize(
    "address",
    [
        "0.0.0.0:50051",
        "10.0.0.5:50051",
        "example.com:443",
        "127.0.0.1",
        "127.0.0.1:0",
        "127.0.0.1:70000",
        "127.0.0.1:50051/extra",
    ],
)
def test_rejects_non_loopback(address: str) -> None:
    with pytest.raises(ConfigError):
        validate_engine_address(address)
