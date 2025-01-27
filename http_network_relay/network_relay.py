#!/usr/bin/env python
import os
import sys
from typing import Type

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ValidationError

from .pydantic_models import (
    EdgeAgentToRelayMessage,
    EtRConnectionResetMessage,
    EtRInitiateConnectionErrorMessage,
    EtRInitiateConnectionOKMessage,
    EtRStartMessage,
    EtRTCPDataMessage,
)

debug = False
if os.getenv("DEBUG") == "1":
    debug = True


def eprint(*args, only_debug=False, **kwargs):
    if (debug and only_debug) or (not only_debug):
        print(*args, file=sys.stderr, **kwargs)


class NetworkRelay:
    CustomAgentToRelayMessage: Type[BaseModel] = None

    def __init__(self, credentials):
        self.agent_connections = []
        self.registered_agent_connections = {}  # name -> connection

        self.credentials = credentials

    async def ws_for_edge_agents(self, websocket: WebSocket):
        await websocket.accept()
        self.agent_connections.append(websocket)
        start_message_json_data = await websocket.receive_text()
        start_message = EdgeAgentToRelayMessage.model_validate_json(
            start_message_json_data
        ).inner
        eprint(f"Message received from agent: {start_message}")
        if not isinstance(start_message, EtRStartMessage):
            eprint(f"Unknown message received from agent: {start_message}")
            return
        #  check if we know the agent
        if start_message.name not in self.credentials["edge-agents"]:
            eprint(f"Unknown agent: {start_message.name}")
            # close the connection
            await websocket.close()
            return

        # check if the secret is correct
        if self.credentials["edge-agents"][start_message.name] != start_message.secret:
            eprint(f"Invalid secret for agent: {start_message.name}")
            # close the connection
            await websocket.close()
            return

        # check if the agent is already registered
        if start_message.name in self.registered_agent_connections:
            # check wether the other websocket is still open, if not remove it
            if self.registered_agent_connections[start_message.name].closed:
                del self.registered_agent_connections[start_message.name]
            else:
                eprint(f"Agent already registered: {start_message.name}")
                # close the connection
                await websocket.close()
                return

        self.registered_agent_connections[start_message.name] = websocket
        eprint(f"Registered agent connection: {start_message.name}")

        while True:
            try:
                json_data = await websocket.receive_text()
            except WebSocketDisconnect:
                eprint(f"Agent disconnected: {start_message.name}")
                del self.registered_agent_connections[start_message.name]
                break
            try:
                message = EdgeAgentToRelayMessage.model_validate_json(json_data).inner
            except ValidationError as e:
                if self.CustomAgentToRelayMessage is None:
                    raise e
                message = self.CustomAgentToRelayMessage.model_validate_json(
                    json_data
                )  # pylint: disable=E1101
            eprint(f"Message received from agent: {message}", only_debug=True)
            if isinstance(message, EtRInitiateConnectionErrorMessage):
                eprint(
                    f"Received initiate connection error message from agent: {message}"
                )
                await self.handle_initiate_connection_error_message(message)
            elif isinstance(message, EtRInitiateConnectionOKMessage):
                eprint(f"Received initiate connection OK message from agent: {message}")
                await self.handle_initiate_connection_ok_message(message)
            elif isinstance(message, EtRTCPDataMessage):
                eprint(
                    f"Received TCP data message from agent: {message}", only_debug=True
                )
                await self.handle_tcp_data_message(message)
            elif isinstance(message, EtRConnectionResetMessage):
                eprint(f"Received connection reset message from agent: {message}")
                await self.handle_connection_reset_message(message)
            elif self.CustomAgentToRelayMessage is not None and isinstance(
                message, self.CustomAgentToRelayMessage
            ):
                await self.handle_custom_agent_message(message)
            else:
                eprint(f"Unknown message received from agent: {message}")

    async def handle_custom_agent_message(self, message_wrapped: BaseModel):
        raise NotImplementedError()

    async def handle_initiate_connection_error_message(
        self, message: EtRInitiateConnectionErrorMessage
    ):
        raise NotImplementedError()

    async def handle_initiate_connection_ok_message(
        self, message: EtRInitiateConnectionOKMessage
    ):
        raise NotImplementedError()

    async def handle_tcp_data_message(self, message: EtRTCPDataMessage):
        raise NotImplementedError()

    async def handle_connection_reset_message(self, message: EtRConnectionResetMessage):
        raise NotImplementedError()
