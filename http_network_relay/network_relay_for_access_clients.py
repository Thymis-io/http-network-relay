#!/usr/bin/env python
import argparse
import asyncio
import base64
import json
import os
import sys

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .access_client import (
    AccessClientToRelayMessage,
    AtRStartMessage,
    AtRTCPDataMessage,
    RelayToAccessClientMessage,
    RtAErrorMessage,
    RtAStartOKMessage,
    RtATCPDataMessage,
)
from .network_relay import NetworkRelay, TcpConnectionAsync
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
        self.active_connections = {}
        self.access_client_connections = []
        self.initiate_connection_answer_queue = asyncio.Queue()
        self.credentials = credentials

    async def ws_for_access_clients(self, access_client_connection: WebSocket):
        await access_client_connection.accept()
        self.access_client_connections.append(access_client_connection)
        json_data = await access_client_connection.receive_text()
        message = AccessClientToRelayMessage.model_validate_json(json_data)
        eprint(f"Message received from access client: {message}")
        if not isinstance(message.inner, AtRStartMessage):
            eprint(f"Unknown message received from access client: {message}")
            return
        start_message = message.inner
        # check if credentials are correct
        if start_message.secret not in self.credentials["access-client-secrets"]:
            eprint(f"Invalid access client secret: {start_message.secret}")
            # send a message back and kill the connection
            await access_client_connection.send_text(
                RelayToAccessClientMessage(
                    inner=RtAErrorMessage(message="Invalid access client secret")
                ).model_dump_json()
            )
        # check if the client is registered
        if not start_message.connection_target in self.registered_agent_connections:
            eprint(f"Agent not registered: {start_message.connection_target}")
            # send a message back and kill the connection
            await access_client_connection.send_text(
                RelayToAccessClientMessage(
                    inner=RtAErrorMessage(message="Agent not registered")
                ).model_dump_json()
            )
            await access_client_connection.close()
            return
        try:
            connection = await self.create_connection_async(
                agent_connection_id=start_message.connection_target,
                target_ip=start_message.target_ip,
                target_port=start_message.target_port,
                protocol=start_message.protocol,
            )
            await access_client_connection.send_text(
                RelayToAccessClientMessage(
                    inner=RtAStartOKMessage(connection_id=connection.id)
                ).model_dump_json()
            )
        except ValueError as e:
            eprint(f"Error creating connection: {e}")
            await access_client_connection.send_text(
                RelayToAccessClientMessage(
                    inner=RtAErrorMessage(message=str(e))
                ).model_dump_json()
            )
            await access_client_connection.close()
            return

        eprint(f"Connection created: {connection}")
        self.active_connections[connection.id] = connection
        reader = asyncio.create_task(
            self.receive_thread(access_client_connection, connection)
        )
        while connection.id in self.active_connections:
            try:
                json_data = await access_client_connection.receive_text()
            except WebSocketDisconnect:
                eprint(f"access client disconnected: {connection.id}")
                if connection.id in self.active_connections:
                    del self.active_connections[connection.id]
                break
            message = AccessClientToRelayMessage.model_validate_json(json_data)
            if isinstance(message.inner, AtRTCPDataMessage):
                eprint(
                    f"Received TCP data message from access client: {message}",
                    only_debug=True,
                )
                await connection.send(base64.b64decode(message.inner.data_base64))
            else:
                eprint(f"Unknown message received from access client: {message}")
        if connection.id in self.active_connections:
            del self.active_connections[connection.id]
        await reader
        access_client_connection.close()

    async def receive_thread(
        self,
        access_client_connection: WebSocket,
        relayed_connection: TcpConnectionAsync,
    ):
        while relayed_connection.id in self.active_connections:
            try:
                data = await relayed_connection.read(1024)
                if not data:
                    break
                await access_client_connection.send_text(
                    RelayToAccessClientMessage(
                        inner=RtATCPDataMessage(
                            connection_id=relayed_connection.id,
                            data_base64=base64.b64encode(data).decode("utf-8"),
                        )
                    ).model_dump_json()
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                eprint(f"Error in receive_thread: {e}")
                import traceback

                traceback.print_exc()
                break
        if relayed_connection.id in self.active_connections:
            del self.active_connections[relayed_connection.id]

    async def get_msg_loop_permission_and_create_connection_id(
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

    # app.add_websocket_route("/ws_for_edge_agents", network_relay.ws_for_edge_agents)
    # need to double await the function result
    # app.add_websocket_route("/ws_for_edge_agents",
    #                         async lambda ws: await network_relay.ws_for_edge_agents(ws))
    # no such thing as async lambda
    @app.websocket("/ws_for_edge_agents")
    async def ws_for_edge_agents(websocket: WebSocket):
        msg_loop_task = (
            await network_relay.accept_ws_and_start_msg_loop_for_edge_agents(websocket)
        )
        await msg_loop_task

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
