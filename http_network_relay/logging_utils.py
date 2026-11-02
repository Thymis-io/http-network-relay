"""stderr logging for the relay clients.

Diagnostics are quiet by default so that `access-client` can be used as an SSH
ProxyCommand without writing into the session. Set
`HTTP_NETWORK_RELAY_LOG_LEVEL` to `info` or `debug` to see connection lifecycle
or protocol payloads. Note that debug output includes start messages, which
contain credentials.
"""

import logging
import os
import sys

DEFAULT_LEVEL = "warning"

_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}
_invalid_level_reported = False


class _StderrHandler(logging.Handler):
    # Resolve sys.stderr at emit time so that replacing it (e.g. in tests)
    # keeps working.
    def emit(self, record):
        try:
            print(self.format(record), file=sys.stderr)
        except Exception:
            self.handleError(record)


logger = logging.getLogger("http_network_relay.cli")
logger.propagate = False
logger.addHandler(_StderrHandler())
logger.setLevel(_LEVELS[DEFAULT_LEVEL])


def _configured_level():
    global _invalid_level_reported
    raw = os.getenv("HTTP_NETWORK_RELAY_LOG_LEVEL", DEFAULT_LEVEL).strip().lower()
    if raw in _LEVELS:
        return _LEVELS[raw]
    if not _invalid_level_reported:
        _invalid_level_reported = True
        logger.setLevel(_LEVELS[DEFAULT_LEVEL])
        logger.warning(
            "Unknown HTTP_NETWORK_RELAY_LOG_LEVEL %r, using %r", raw, DEFAULT_LEVEL
        )
    return _LEVELS[DEFAULT_LEVEL]


def _log(level, *args, **kwargs):
    logger.setLevel(_configured_level())
    logger.log(level, *args, **kwargs)


def debug(*args, **kwargs):
    _log(logging.DEBUG, *args, **kwargs)


def info(*args, **kwargs):
    _log(logging.INFO, *args, **kwargs)


def warning(*args, **kwargs):
    _log(logging.WARNING, *args, **kwargs)


def error(*args, **kwargs):
    _log(logging.ERROR, *args, **kwargs)
