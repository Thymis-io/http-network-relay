#!/usr/bin/env python
import argparse
import json
import os
import sys

import uvicorn
from fastapi import FastAPI

from .access_client import AtRStartMessage, RelayToAccessClientMessage, RtAErrorMessage
from .network_relay import NetworkRelay
from .pydantic_models import EtRStartMessage

CREDENTIALS_FILE = os.getenv("HTTP_NETWORK_RELAY_CREDENTIALS_FILE", "credentials.json")
CREDENTIALS = None

debug = False
if os.getenv("DEBUG") == "1":
    debug = True


def eprint(*args, only_debug=False, **kwargs):
    if (debug and only_debug) or (not only_debug):
        print(*args, file=sys.stderr, **kwargs)


class EdgeAgentToRelayStartMessage(EtRStartMessage):
    name: str
    secret: str


class NetworkRelayForAccessClients(NetworkRelay):
    CustomAgentToRelayStartMessage = EdgeAgentToRelayStartMessage

    def __init__(self, credentials):
        super().__init__()
        self.credentials = credentials

    async def get_access_client_permission(
        self, start_message: AtRStartMessage, access_client_connection
    ):
        if start_message.secret not in self.credentials["access-client-secrets"]:
            eprint(f"Invalid access client secret: {start_message.secret}")
            await access_client_connection.send_text(
                RelayToAccessClientMessage(
                    inner=RtAErrorMessage(message="Invalid access client secret")
                ).model_dump_json()
            )
            return False
        return True

    async def get_agent_msg_loop_permission_and_create_connection_id(
        self, start_message: EdgeAgentToRelayStartMessage, edge_agent_connection
    ):
        #  check if we know the agent
        if start_message.name not in self.credentials["edge-agents"]:
            eprint(f"Unknown agent: {start_message.name}")
            return None

        # check if the secret is correct
        if self.credentials["edge-agents"][start_message.name] != start_message.secret:
            eprint(f"Invalid secret for agent: {start_message.name}")
            return None

        return start_message.name


def main():
    parser = argparse.ArgumentParser(description="Run the HTTP network relay server")
    parser.add_argument(
        "--host",
        help="The host to bind to",
        default=os.getenv("HTTP_NETWORK_RELAY_SERVER_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port",
        help="The port to bind to",
        type=int,
        default=int(os.getenv("HTTP_NETWORK_RELAY_SERVER_PORT", "8000")),
    )
    parser.add_argument(
        "--credentials-file",
        help="The credentials file",
        default=CREDENTIALS_FILE,
    )
    args = parser.parse_args()

    app = FastAPI()

    credentials_file = args.credentials_file
    with open(credentials_file) as f:
        CREDENTIALS = json.load(f)

    network_relay = NetworkRelayForAccessClients(CREDENTIALS)

    app.add_websocket_route("/ws_for_edge_agents", network_relay.ws_for_edge_agents)
    app.add_websocket_route(
        "/ws_for_access_clients", network_relay.ws_for_access_clients
    )

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
