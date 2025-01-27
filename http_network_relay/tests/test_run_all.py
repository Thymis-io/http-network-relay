import json
import random
import socket
import subprocess
import tempfile
import threading
import time

import pytest


@pytest.mark.timeout(10)
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
                    "http_network_relay.network_relay_for_access_clients",
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
                "http_network_relay.edge_agent",
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
            "http_network_relay.access_client",
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
        time.sleep(1)
        p.kill()

    tcp_thread.join()
    relay_thread.join()
    edge_agent_thread.join()
    access_client.wait()

    assert response == b"olleh\n"
