import pytest

from http_network_relay import logging_utils

LEVEL_ENV = "HTTP_NETWORK_RELAY_LOG_LEVEL"
MESSAGES = ("msg-debug", "msg-info", "msg-warning", "msg-error")


def emit_all():
    logging_utils.debug("msg-debug")
    logging_utils.info("msg-info")
    logging_utils.warning("msg-warning")
    logging_utils.error("msg-error")


@pytest.mark.parametrize(
    "level, expected",
    [
        (None, {"msg-warning", "msg-error"}),
        ("warning", {"msg-warning", "msg-error"}),
        ("info", {"msg-info", "msg-warning", "msg-error"}),
        ("debug", set(MESSAGES)),
        ("error", {"msg-error"}),
    ],
)
def test_level_gates_stderr(monkeypatch, capsys, level, expected):
    if level is None:
        monkeypatch.delenv(LEVEL_ENV, raising=False)
    else:
        monkeypatch.setenv(LEVEL_ENV, level)

    emit_all()

    assert set(capsys.readouterr().err.splitlines()) == expected


def test_level_is_case_insensitive(monkeypatch, capsys):
    monkeypatch.setenv(LEVEL_ENV, "INFO")

    emit_all()

    assert "msg-info" in capsys.readouterr().err


def test_invalid_level_falls_back_to_warning(monkeypatch, capsys):
    monkeypatch.setattr(logging_utils, "_invalid_level_reported", False)
    monkeypatch.setenv(LEVEL_ENV, "loud")

    emit_all()

    stderr = capsys.readouterr().err
    assert "msg-warning" in stderr
    assert "msg-error" in stderr
    assert "msg-info" not in stderr
    assert "Unknown HTTP_NETWORK_RELAY_LOG_LEVEL 'loud'" in stderr
