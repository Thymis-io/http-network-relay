import json
import os
import random
import socket
import subprocess
import tempfile
import threading
import time

import pytest


@pytest.mark.timeout(3)
def test_can_run_and_proxy_tcp():
    # start 3 threads to supervise 3 processes each, 1 more to listen to tcp
    # 0. start tcp listening thread
    # 1. start the relay server
    # 2. start the edge agent
    # 3. start the access client and connect to the edge agent

    agent_secret = random.randbytes(16).hex()
    relay_secret = random.randbytes(16).hex()
    agent_name = "test_agent"
    port_listener = random.randint(10000, 20000)
    port_relay = random.randint(20000, 30000)

    started_subprocesses = []

    def tcp_listening_thread():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", port_listener))
        s.listen(1)
        conn, addr = s.accept()
        # echo reverse server
        buf = b""
        while True:
            data = conn.recv(1024)
            if not data:
                break
            buf += data
            # until we have a newline
            newline = buf.find(b"\n")
            if newline != -1:
                conn.sendall(buf[:newline][::-1] + b"\n")
                buf = buf[:newline]
        conn.close()
        s.close()

    def relay_server_thread():
        # make tmpfile for credentials
        with tempfile.NamedTemporaryFile() as f:
            f.write(
                json.dumps(
                    {
                        "edge-agents": {"test_agent": agent_secret},
                        "access-client-secrets": [relay_secret],
                    }
                ).encode()
            )
            f.flush()

            # env = os.environ.copy()
            # env["HTTP_NETWORK_RELAY_CREDENTIALS_FILE"] = f.name

            relay_server = subprocess.Popen(
                [
                    "python",
                    "-m",
                    "http_network_relay.network_relay_example",
                    "--port",
                    str(port_relay),
                    "--credentials-file",
                    f.name,
                ],
                # env=env,
            )
            started_subprocesses.append(relay_server)
            relay_server.wait()

    def edge_agent_thread():
        edge_agent = subprocess.Popen(
            [
                "python",
                "-m",
                "http_network_relay.edge_agent_example",
                "--secret",
                agent_secret,
                "--relay-url",
                f"ws://127.0.0.1:{port_relay}/ws_for_edge_agents",
                "--name",
                agent_name,
            ]
        )
        started_subprocesses.append(edge_agent)
        edge_agent.wait()

    tcp_thread = threading.Thread(target=tcp_listening_thread)
    relay_thread = threading.Thread(target=relay_server_thread)
    edge_agent_thread = threading.Thread(target=edge_agent_thread)

    tcp_thread.start()
    time.sleep(0.2)
    relay_thread.start()
    time.sleep(0.5)
    edge_agent_thread.start()
    time.sleep(0.5)

    access_client = subprocess.Popen(
        [
            "python",
            "-m",
            "http_network_relay.access_client_example",
            "--secret",
            relay_secret,
            agent_name,
            "127.0.0.1",
            str(port_listener),
            "tcp",
            "--relay-url",
            f"ws://127.0.0.1:{port_relay}/ws_for_access_clients",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )

    access_client.stdin.write(b"hello\n")
    access_client.stdin.flush()
    response = access_client.stdout.readline()
    access_client.stdin.close()
    access_client.terminate()
    access_client.kill()

    # kill other threads
    for p in started_subprocesses:
        p.terminate()
        time.sleep(0.2)
        p.kill()

    tcp_thread.join()
    relay_thread.join()
    edge_agent_thread.join()
    access_client.wait()

    assert response == b"olleh\n"


def test_binary_frame_roundtrip():
    import uuid as _uuid

    from http_network_relay.access_client import AtRStartMessage, RtAStartOKMessage
    from http_network_relay.pydantic_models import (
        EtRStartMessage,
        RtEInitiateConnectionMessage,
        decode_tcp_binary_frame,
        encode_tcp_binary_frame,
    )

    cid = str(_uuid.uuid4())
    payload = random.randbytes(1000)
    frame = encode_tcp_binary_frame(cid, payload)
    assert len(frame) == 16 + len(payload)
    out_cid, out_payload = decode_tcp_binary_frame(frame)
    assert out_cid == cid
    assert out_payload == payload

    # Backward compatibility: a message from an older peer omits supports_binary,
    # which must default to False so the new side falls back to JSON framing.
    assert EtRStartMessage().supports_binary is False
    assert (
        RtEInitiateConnectionMessage(
            target_ip="x", target_port=1, protocol="tcp", connection_id=cid
        ).supports_binary
        is False
    )
    assert (
        AtRStartMessage(
            connection_target="a",
            target_ip="x",
            target_port=1,
            protocol="tcp",
            secret="s",
        ).supports_binary
        is False
    )
    assert RtAStartOKMessage().supports_binary is False


@pytest.mark.timeout(30)
def test_large_binary_payload_roundtrip():
    # Push several MB through the relay to exercise the 64 KiB binary-framing
    # data path and assert byte-for-byte integrity (the nix-copy deploy case).
    agent_secret = random.randbytes(16).hex()
    relay_secret = random.randbytes(16).hex()
    agent_name = "test_agent"
    port_listener = random.randint(10000, 20000)
    port_relay = random.randint(20000, 30000)
    payload = random.randbytes(3 * 1024 * 1024)

    started = []

    def echo_server():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port_listener))
        s.listen(1)
        conn, _ = s.accept()
        try:
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                conn.sendall(data)
        except OSError:
            pass
        finally:
            conn.close()
            s.close()

    credf = tempfile.NamedTemporaryFile(delete=False)
    credf.write(
        json.dumps(
            {
                "edge-agents": {agent_name: agent_secret},
                "access-client-secrets": [relay_secret],
            }
        ).encode()
    )
    credf.flush()
    credf.close()

    echo_thread = threading.Thread(target=echo_server, daemon=True)
    echo_thread.start()
    time.sleep(0.2)

    relay = subprocess.Popen(
        [
            "python",
            "-m",
            "http_network_relay.network_relay_example",
            "--port",
            str(port_relay),
            "--credentials-file",
            credf.name,
        ]
    )
    started.append(relay)
    time.sleep(0.8)

    agent = subprocess.Popen(
        [
            "python",
            "-m",
            "http_network_relay.edge_agent_example",
            "--secret",
            agent_secret,
            "--relay-url",
            f"ws://127.0.0.1:{port_relay}/ws_for_edge_agents",
            "--name",
            agent_name,
        ]
    )
    started.append(agent)
    time.sleep(0.8)

    access_client = subprocess.Popen(
        [
            "python",
            "-m",
            "http_network_relay.access_client_example",
            "--secret",
            relay_secret,
            agent_name,
            "127.0.0.1",
            str(port_listener),
            "tcp",
            "--relay-url",
            f"ws://127.0.0.1:{port_relay}/ws_for_access_clients",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    started.append(access_client)

    def feed():
        try:
            access_client.stdin.write(payload)
            access_client.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass

    threading.Thread(target=feed, daemon=True).start()

    received = bytearray()
    result = {"err": None}

    def drain():
        try:
            while len(received) < len(payload):
                chunk = access_client.stdout.read1(65536)
                if not chunk:
                    break
                received.extend(chunk)
        except Exception as e:  # pragma: no cover
            result["err"] = e

    drainer = threading.Thread(target=drain, daemon=True)
    drainer.start()
    drainer.join(timeout=20)

    for p in started:
        p.terminate()
        time.sleep(0.1)
        p.kill()
    os.unlink(credf.name)

    assert result["err"] is None, result["err"]
    assert len(received) == len(payload), f"got {len(received)} of {len(payload)} bytes"
    assert bytes(received) == payload
