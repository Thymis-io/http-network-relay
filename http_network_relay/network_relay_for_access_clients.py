#!/usr/bin/env python
import argparse
import asyncio
import json
import os
import sys
import uuid

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
from .network_relay import NetworkRelay
from .pydantic_models import (
    EtRConnectionResetMessage,
    EtRInitiateConnectionErrorMessage,
    EtRInitiateConnectionOKMessage,
    EtRTCPDataMessage,
    RelayToEdgeAgentMessage,
    RtEInitiateConnectionMessage,
    RtETCPDataMessage,
)

CREDENTIALS_FILE = os.getenv("HTTP_NETWORK_RELAY_CREDENTIALS_FILE", "credentials.json")
CREDENTIALS = None

debug = False
if os.getenv("DEBUG") == "1":
    debug = True


def eprint(*args, only_debug=False, **kwargs):
    if (debug and only_debug) or (not only_debug):
        print(*args, file=sys.stderr, **kwargs)


class NetworkRelayForAccessClients(NetworkRelay):
    def __init__(self, credentials):
        super().__init__(credentials)
        self.active_connections = {}
        self.access_client_connections = []
        self.initiate_connection_answer_queue = asyncio.Queue()

    async def ws_for_access_clients(self, websocket: WebSocket):
        await websocket.accept()
        self.access_client_connections.append(websocket)
        json_data = await websocket.receive_text()
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
            await websocket.send_text(
                RelayToAccessClientMessage(
                    inner=RtAErrorMessage(message="Invalid access client secret")
                ).model_dump_json()
            )
        # check if the client is registered
        if not start_message.connection_target in self.registered_agent_connections:
            eprint(f"Agent not registered: {start_message.connection_target}")
            # send a message back and kill the connection
            await websocket.send_text(
                RelayToAccessClientMessage(
                    inner=RtAErrorMessage(message="Agent not registered")
                ).model_dump_json()
            )
            await websocket.close()
            return
        agent_connection = self.registered_agent_connections[
            start_message.connection_target
        ]
        await self.start_connection(
            agent_connection=agent_connection,
            access_client_connection=websocket,
            connection_target=start_message.connection_target,
            target_ip=start_message.target_ip,
            target_port=start_message.target_port,
            protocol=start_message.protocol,
        )

    async def start_connection(
        self,
        agent_connection,
        access_client_connection,
        connection_target,
        target_ip,
        target_port,
        protocol,
    ):
        connection_id = str(uuid.uuid4())
        eprint(
            f"Starting connection to {target_ip}:{target_port} for {connection_target} using {protocol} with connection_id {connection_id}"
        )
        self.active_connections[connection_id] = (
            agent_connection,
            access_client_connection,
        )
        await agent_connection.send_text(
            RelayToEdgeAgentMessage(
                inner=RtEInitiateConnectionMessage(
                    target_ip=target_ip,
                    target_port=target_port,
                    protocol=protocol,
                    connection_id=connection_id,
                )
            ).model_dump_json()
        )
        # wait for the client to respond
        message = await self.initiate_connection_answer_queue.get()
        if not isinstance(
            message,
            (
                EtRInitiateConnectionErrorMessage,
                EtRInitiateConnectionOKMessage,
            ),
        ):
            raise ValueError(f"Unexpected message: {message}")
        if message.connection_id != connection_id:
            raise ValueError(f"Unexpected connection_id: {message.connection_id}")
        if isinstance(message, EtRInitiateConnectionErrorMessage):
            eprint(f"Received error message from client: {message}")
            await access_client_connection.send_text(
                RelayToAccessClientMessage(
                    inner=RtAErrorMessage(
                        message=f"Initiating connection failed: {message.message}"
                    )
                ).model_dump_json()
            )
            # close the connection
            await access_client_connection.close()
            del self.active_connections[connection_id]
            return
        if isinstance(message, EtRInitiateConnectionOKMessage):
            eprint(f"Received OK message from client: {message}")
        await access_client_connection.send_text(
            RelayToAccessClientMessage(inner=RtAStartOKMessage()).model_dump_json()
        )

        while True:
            try:
                json_data = await access_client_connection.receive_text()
            except WebSocketDisconnect:
                eprint(f"access client disconnected: {connection_id}")
                if connection_id in self.active_connections:
                    del self.active_connections[connection_id]
                break
            message = AccessClientToRelayMessage.model_validate_json(json_data)
            if isinstance(message.inner, AtRTCPDataMessage):
                eprint(
                    f"Received TCP data message from access client: {message}",
                    only_debug=True,
                )
                await agent_connection.send_text(
                    RelayToEdgeAgentMessage(
                        inner=RtETCPDataMessage(
                            connection_id=connection_id,
                            data_base64=message.inner.data_base64,
                        )
                    ).model_dump_json()
                )
            else:
                eprint(f"Unknown message received from access client: {message}")

    async def handle_initiate_connection_error_message(
        self, message: EtRInitiateConnectionErrorMessage
    ):
        await self.initiate_connection_answer_queue.put(message)

    async def handle_initiate_connection_ok_message(
        self, message: EtRInitiateConnectionOKMessage
    ):
        await self.initiate_connection_answer_queue.put(message)

    async def handle_tcp_data_message(self, message: EtRTCPDataMessage):
        if message.connection_id not in self.active_connections:
            eprint(f"Unknown connection_id: {message.connection_id}")
            return
        _agent_connection, access_client_connection = self.active_connections[
            message.connection_id
        ]
        await access_client_connection.send_text(
            RelayToAccessClientMessage(
                inner=RtATCPDataMessage(data_base64=message.data_base64)
            ).model_dump_json()
        )

    async def handle_connection_reset_message(self, message: EtRConnectionResetMessage):
        if message.connection_id not in self.active_connections:
            eprint(f"Unknown connection_id: {message.connection_id}")
            return
        _agent_connection, access_client_connection = self.active_connections[
            message.connection_id
        ]
        await access_client_connection.send_text(
            RelayToAccessClientMessage(
                inner=RtAErrorMessage(message=f"Connection reset: {message.message}")
            ).model_dump_json()
        )
        # close the connection
        await access_client_connection.close()
        del self.active_connections[message.connection_id]


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
