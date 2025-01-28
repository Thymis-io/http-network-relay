#!/usr/bin/env python
import asyncio
import base64
import os
import sys
import threading
import uuid
from contextlib import AbstractAsyncContextManager, AbstractContextManager
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
    RelayToEdgeAgentMessage,
    RelayToEdgeAgentMessage_Inner,
    RtEInitiateConnectionMessage,
    RtETCPDataMessage,
)

debug = False
if os.getenv("DEBUG") == "1":
    debug = True


def eprint(*args, only_debug=False, **kwargs):
    if (debug and only_debug) or (not only_debug):
        print(*args, file=sys.stderr, **kwargs)


class TcpConnection(AbstractContextManager):
    # compare https://docs.paramiko.org/en/2.4/api/proxy.html#paramiko.proxy.ProxyCommand
    def __init__(
        self, connection_id: str, relay: "NetworkRelay", agent_connection: WebSocket
    ):
        self.id = connection_id
        self.relay = relay
        self.recv_buffer = bytearray()
        self.agent_connection = agent_connection
        self.timeout = None
        self.event = threading.Event()

    async def send(self, content):
        return self.relay.send_connection_message(
            self.agent_connection,
            RtETCPDataMessage(
                connection_id=self.id, data_base64=base64.b64encode(content).decode()
            ),
        )

    def fill_recv(self, data: bytes):
        self.recv_buffer.extend(data)
        self.event.set()

    def recv(self, size):
        # return self.todo.recv(size)
        # send AT MOST size bytes, at least 1 byte if blocking
        if len(self.recv_buffer) == 0:
            if self.closed:
                return b""
            # wait for data
            self.event.clear()
            res = self.event.wait(self.timeout)
            if not res:
                raise TimeoutError()
        if size == 0:
            return b""  # this is how socket.recv(0) behaves i guess
        result = self.recv_buffer[:size]
        self.recv_buffer = self.recv_buffer[size:]
        return result

    async def close(self):
        return await self.relay.close_connection(self.id, self.agent_connection)

    @property
    def closed(self):
        return self.id not in self.relay.active_relayed_connections

    @property
    def _closed(
        self,
    ):  # Concession to Python 3 socket-like API, again, see paramiko.proxy.ProxyCommand
        return self.closed

    def settimeout(self, timeout: float | None):
        self.timeout = timeout

    # from ClosingContextManager of paramiko.util:
    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.close()


class TcpConnectionAsync(AbstractAsyncContextManager):
    def __init__(
        self, connection_id: str, relay: "NetworkRelay", agent_connection: WebSocket
    ):
        self.id = connection_id
        self.relay = relay
        self.recv_buffer = bytearray()
        self.agent_connection = agent_connection
        self.timeout = None
        self.event = asyncio.Event()

    async def send(self, content):
        return await self.relay.send_connection_message(
            self.agent_connection,
            RtETCPDataMessage(
                connection_id=self.id, data_base64=base64.b64encode(content).decode()
            ),
        )

    def fill_recv(self, data: bytes):
        self.recv_buffer.extend(data)
        self.event.set()

    async def read(self, n: int = -1) -> bytes:
        if n == -1:
            while not self.closed:
                await self.event.wait()
                self.event.clear()
            result = self.recv_buffer
            self.recv_buffer = bytearray()
            return result
        if n == 0:
            return b""
        while len(self.recv_buffer) == 0:
            if self.closed:
                return b""
            await self.event.wait()
            self.event.clear()
        result = self.recv_buffer[:n]
        self.recv_buffer = self.recv_buffer[n:]
        return result

    async def close(self):
        return await self.relay.close_connection(self.id, self.agent_connection)

    @property
    def closed(self):
        return self.id not in self.relay.active_relayed_connections

    @property
    def _closed(self):
        return self.closed

    async def __aenter__(self):
        return self

    async def __aexit__(self, type, value, traceback):
        await self.close()


class NetworkRelay:
    CustomAgentToRelayMessage: Type[BaseModel] = None

    def __init__(self):
        self.agent_connections = []
        self.registered_agent_connections: dict[
            str, WebSocket
        ] = {}  # agent_connection_id -> websocket

        self.initiate_connection_answer_queues: dict[
            str, asyncio.Queue
        ] = {}  # connection_id -> queue

        self.active_relayed_connections: dict[uuid.UUID, TcpConnection] = {}

    async def ws_for_edge_agents(self, edge_agent_connection: WebSocket):
        await edge_agent_connection.accept()
        self.agent_connections.append(edge_agent_connection)
        start_message_json_data = await edge_agent_connection.receive_text()
        start_message = EdgeAgentToRelayMessage.model_validate_json(
            start_message_json_data
        ).inner
        eprint(f"Message received from agent: {start_message}")
        if not isinstance(start_message, EtRStartMessage):
            eprint(f"Unknown message received from agent: {start_message}")
            return

        if not await self.check_agent_start_message_auth(
            start_message, edge_agent_connection
        ):
            eprint(f"Authentication failed for agent: {start_message}")
            # close the connection
            await edge_agent_connection.close()
            return

        connection_id = await self.get_agent_connection_id_from_start_message(
            start_message, edge_agent_connection
        )

        # check if the agent is already registered
        if connection_id in self.registered_agent_connections:
            # check wether the other websocket is still open, if not remove it
            if self.registered_agent_connections[connection_id].closed:
                del self.registered_agent_connections[connection_id]
            else:
                eprint(f"Agent already registered: {connection_id}")
                # close the connection
                await edge_agent_connection.close()
                return

        self.registered_agent_connections[connection_id] = edge_agent_connection
        eprint(f"Registered agent connection: {connection_id}")

        while True:
            try:
                json_data = await edge_agent_connection.receive_text()
                eprint(f"Received message from agent: {json_data}")
            except WebSocketDisconnect:
                eprint(f"Agent disconnected: {connection_id}")
                del self.registered_agent_connections[connection_id]
                break
            message_outer = None
            try:
                message_outer = EdgeAgentToRelayMessage.model_validate_json(json_data)
                message = message_outer.inner
            except ValidationError as e:
                if self.CustomAgentToRelayMessage is None:
                    raise e
                message = self.CustomAgentToRelayMessage.model_validate_json(
                    json_data
                )  # pylint: disable=E1101
            eprint(f"Message received from agent: {message}", only_debug=True)
            if isinstance(message, EtRTCPDataMessage):
                eprint(
                    f"Received TCP data message from agent: {message}", only_debug=True
                )
                await self.handle_tcp_data_message(message)
            elif isinstance(message, EtRConnectionResetMessage):
                eprint(f"Received connection reset message from agent: {message}")
                await self.handle_connection_reset_message(message)
            elif message_outer is not None:
                # this is a request-response message
                # get queue
                eprint(f"Got request-response message: {message}")
                response_queue = self.initiate_connection_answer_queues.get(
                    message.connection_id
                )
                if response_queue is None:
                    eprint(
                        f"Unknown connection_id {message.connection_id} for message: {message}"
                    )
                    continue
                # put message in queue
                await response_queue.put(message)
            elif self.CustomAgentToRelayMessage is not None and isinstance(
                message,
                self.CustomAgentToRelayMessage,
            ):
                await self.handle_custom_agent_message(message)
            else:
                eprint(f"Unknown message received from agent: {message}")

    async def _create_connection(
        self,
        agent_connection_id: str,
        target_ip: str,
        target_port: int,
        protocol: str,
        connection_class,
    ) -> TcpConnection:
        agent_connection = self.registered_agent_connections.get(agent_connection_id)
        if agent_connection is None:
            raise ValueError(f"Unknown agent: {agent_connection_id}")
        # step 0. create TcpConnection object so that messages sent immediately after tcp creation can be received by the main loop
        connection_id = str(uuid.uuid4())
        connection = connection_class(connection_id, self, agent_connection)
        self.active_relayed_connections[connection_id] = connection
        # step 1. send initiate connection message to agent
        response = await self.send_message_and_wait_for_answer(
            agent_connection,
            RtEInitiateConnectionMessage(
                target_ip=target_ip,
                target_port=target_port,
                protocol=protocol,
                connection_id=str(connection_id),
            ),
        )
        match response:
            case EtRInitiateConnectionErrorMessage(
                message=message, connection_id=connection_id
            ):
                raise ValueError(f"Error initiating connection: {message}")
            case EtRInitiateConnectionOKMessage(connection_id=connection_id):
                pass
            case _:
                raise ValueError(
                    f"Unknown response: {response} to initiate connection message for connection_id: {connection_id}"
                )
        return connection

    async def create_connection(
        self, agent_connection_id: str, target_ip: str, target_port: int, protocol: str
    ) -> TcpConnection:
        return await self._create_connection(
            agent_connection_id, target_ip, target_port, protocol, TcpConnection
        )

    async def create_connection_async(
        self, agent_connection_id: str, target_ip: str, target_port: int, protocol: str
    ) -> TcpConnectionAsync:
        return await self._create_connection(
            agent_connection_id, target_ip, target_port, protocol, TcpConnectionAsync
        )

    async def handle_tcp_data_message(self, message: EtRTCPDataMessage):
        if message.connection_id not in self.active_relayed_connections:
            eprint(f"Unknown connection_id: {message.connection_id}")
            return
        connection = self.active_relayed_connections[message.connection_id]
        connection.fill_recv(base64.b64decode(message.data_base64))

    async def handle_connection_reset_message(self, message: EtRConnectionResetMessage):
        if message.connection_id not in self.active_relayed_connections:
            eprint(f"Unknown connection_id: {message.connection_id}")
            return
        del self.active_relayed_connections[message.connection_id]

    async def send_message_and_wait_for_answer(
        self, agent_connection: WebSocket, message: RelayToEdgeAgentMessage_Inner
    ):
        response_queue = asyncio.Queue(maxsize=1)
        self.initiate_connection_answer_queues[message.connection_id] = response_queue
        await agent_connection.send_text(
            RelayToEdgeAgentMessage(inner=message).model_dump_json()
        )
        eprint(f"Waiting for response for connection_id: {message.connection_id}")
        response = await response_queue.get()
        eprint(f"Got response for connection_id: {message.connection_id}")
        del self.initiate_connection_answer_queues[message.connection_id]
        return response

    async def send_connection_message(
        self, agent_connection: WebSocket, message: RtETCPDataMessage
    ):
        await agent_connection.send_text(
            RelayToEdgeAgentMessage(inner=message).model_dump_json()
        )

    async def close_connection(self, connection_id: str, agent_connection: WebSocket):
        # inform agent
        await agent_connection.send_text(
            RelayToEdgeAgentMessage(
                inner=EtRConnectionResetMessage(
                    message="Connection closed by relay", connection_id=connection_id
                )
            ).model_dump_json()
        )
        # remove connection
        del self.active_relayed_connections[connection_id]

    async def handle_custom_agent_message(self, message: BaseModel):
        raise NotImplementedError()

    async def check_agent_start_message_auth(
        self, start_message: EtRStartMessage, edge_agent_connection: WebSocket
    ):
        raise NotImplementedError()

    async def get_agent_connection_id_from_start_message(
        self, start_message: EtRStartMessage, edge_agent_connection: WebSocket
    ):
        raise NotImplementedError()
