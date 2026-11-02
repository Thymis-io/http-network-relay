import asyncio
import json
import os
import socket
import subprocess
import sys
import threading

import pytest
from websockets.asyncio.server import serve

from http_network_relay.access_client import (
    RelayToAccessClientMessage,
    RtAStartOKMessage,
)

LEVEL_ENV = "HTTP_NETWORK_RELAY_LOG_LEVEL"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_access_client(log_level):
    port = _free_port()
    ready = threading.Event()
    finished = threading.Event()
    server_errors = []

    async def handler(websocket):
        try:
            start = json.loads(await websocket.recv())
            assert start["inner"]["kind"] == "start"
            await websocket.send(
                RelayToAccessClientMessage(inner=RtAStartOKMessage()).model_dump_json()
            )
        finally:
            await websocket.close()
            finished.set()

    async def run_server():
        async with serve(handler, "127.0.0.1", port):
            ready.set()
            await asyncio.to_thread(finished.wait)

    def server_thread():
        try:
            asyncio.run(run_server())
        except Exception as e:  # pragma: no cover - surfaced via server_errors
            server_errors.append(e)
            ready.set()

    thread = threading.Thread(target=server_thread, daemon=True)
    thread.start()
    assert ready.wait(5), "relay stub did not start"

    env = os.environ.copy()
    env.pop(LEVEL_ENV, None)
    if log_level is not None:
        env[LEVEL_ENV] = log_level

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "http_network_relay.access_client_example",
            "--secret",
            "test-secret",
            "test-agent",
            "127.0.0.1",
            "1",
            "tcp",
            "--relay-url",
            f"ws://127.0.0.1:{port}/",
        ],
        input=b"",
        capture_output=True,
        timeout=10,
        env=env,
    )

    thread.join(timeout=5)
    assert not thread.is_alive()
    assert not server_errors
    return completed


@pytest.mark.timeout(15)
def test_access_client_is_quiet_by_default():
    completed = _run_access_client(None)

    assert completed.returncode == 0
    assert b"Sent start message" not in completed.stderr
    assert b"Received OK message" not in completed.stderr


@pytest.mark.timeout(15)
def test_access_client_logs_when_debug_is_requested():
    completed = _run_access_client("debug")

    assert completed.returncode == 0
    assert b"Sent start message" in completed.stderr
    assert b"Received OK message" in completed.stderr
