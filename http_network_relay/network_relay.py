#!/usr/bin/env python
import asyncio
import base64
import logging
import threading
import uuid
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from typing import Optional, Type

from fastapi import WebSocket, WebSocketDisconnect
from fastapi.websockets import WebSocketState
from pydantic import BaseModel, ValidationError

from .access_client import (
    AccessClientToRelayMessage,
    AtRStartMessage,
    AtRTCPDataMessage,
    RelayToAccessClientMessage,
    RtAErrorMessage,
    RtAStartOKMessage,
    RtATCPDataMessage,
)
from .pydantic_models import (
    EdgeAgentToRelayMessage,
    EtRConnectionResetMessage,
    EtRInitiateConnectionErrorMessage,
    EtRInitiateConnectionOKMessage,
    EtRKeepAliveMessage,
    EtRStartMessage,
    EtRTCPDataMessage,
    RelayToEdgeAgentMessage,
    RelayToEdgeAgentMessage_Inner,
    RtEConnectionCloseMessage,
    RtEInitiateConnectionMessage,
    RtEKeepAliveMessage,
    RtETCPDataMessage,
)

logger = logging.getLogger(__name__)


class TcpConnection(AbstractContextManager):
    # compare https://docs.paramiko.org/en/2.4/api/proxy.html#paramiko.proxy.ProxyCommand
    def __init__(
        self,
        connection_id: str,
        relay: "NetworkRelay",
        agent_connection: WebSocket,
        loop,
    ):
        self.id = connection_id
        self.relay = relay
        self.recv_buffer = bytearray()
        self.agent_connection = agent_connection
        self.timeout = None
        self.recv_event = threading.Event()
        self.recv_buffer_lock = threading.Lock()
        self.send_buffer = bytearray()
        self.send_buffer_lock = threading.Lock()
        self.loop = loop

    def send(self, content):
        with self.send_buffer_lock:
            self.send_buffer.extend(content)
        asyncio.run_coroutine_threadsafe(self.send_async(), self.loop)
        return len(content)

    async def send_async(self):
        with self.send_buffer_lock:
            content = self.send_buffer
            self.send_buffer = bytearray()
            await self.relay.send_connection_message(
                self.agent_connection,
                RtETCPDataMessage(
                    connection_id=self.id,
                    data_base64=base64.b64encode(content).decode(),
                ),
            )

    def fill_recv(self, data: bytes):
        with self.recv_buffer_lock:
            self.recv_buffer.extend(data)
            self.recv_event.set()

    def recv(self, size):
        # return self.todo.recv(size)
        # send AT MOST size bytes, at least 1 byte if blocking
        with self.recv_buffer_lock:
            if len(self.recv_buffer) == 0:
                if self.closed:
                    return b""
                # wait for data
                self.recv_event.clear()

        res = self.recv_event.wait(self.timeout)
        if not res:
            raise TimeoutError()
        if size == 0:
            return b""  # this is how socket.recv(0) behaves i guess
        with self.recv_buffer_lock:
            result = self.recv_buffer[:size]
            self.recv_buffer = self.recv_buffer[size:]
        return result

    def close(self):
        return asyncio.run_coroutine_threadsafe(
            self.relay.close_relayed_connection(self.id, self.agent_connection),
            self.loop,
        )

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
        self,
        connection_id: str,
        relay: "NetworkRelay",
        agent_connection: WebSocket,
        loop,
    ):
        self.id = connection_id
        self.relay = relay
        self.recv_buffer = bytearray()
        self.agent_connection = agent_connection
        self.timeout = None
        self.event = asyncio.Event()
        self.loop = loop

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
        return await self.relay.close_relayed_connection(self.id, self.agent_connection)

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
    CustomAgentToRelayStartMessage: Type[BaseModel] = None

    def __init__(self):
        self.agent_connections = []
        self.registered_agent_connections: dict[
            str, WebSocket
        ] = {}  # agent_connection_id -> WebSocket for edge agents

        self.initiate_connection_answer_queues: dict[
            str, asyncio.Queue
        ] = {}  # connection_id -> queue

        self.active_relayed_connections: dict[
            str, TcpConnection | TcpConnectionAsync
        ] = {}  # connection_id -> TcpConnection
        self.active_access_client_connections = (
            {}
        )  # connection_id -> WebSocket for access clients
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    async def accept_ws_and_start_msg_loop_for_edge_agents(
        self, edge_agent_connection: WebSocket
    ):
        self.loop = asyncio.get_event_loop()
        await edge_agent_connection.accept()
        self.agent_connections.append(edge_agent_connection)
        start_message_json_data = await edge_agent_connection.receive_text()
        start_message = EdgeAgentToRelayMessage.model_validate_json(
            start_message_json_data
        ).inner
        logger.info("Message received from agent: %s", start_message)
        if not isinstance(start_message, EtRStartMessage):
            logger.warning("Unknown message received from agent: %s", start_message)
            return

        if self.CustomAgentToRelayStartMessage is not None:
            start_message = self.CustomAgentToRelayStartMessage.model_validate(
                start_message, from_attributes=True
            )

        connection_id_or_falsy = (
            await self.get_agent_msg_loop_permission_and_create_connection_id(
                start_message, edge_agent_connection
            )
        )
        if not connection_id_or_falsy:
            logger.warning("Agent %s cannot proceed to tcp relaying", start_message)
            # close the connection
            await edge_agent_connection.close()
            return
        connection_id = connection_id_or_falsy

        # check if the agent is already registered
        if connection_id in self.registered_agent_connections:
            # check wether the other websocket is still open, if not remove it
            if self.registered_agent_connections[connection_id].closed:
                del self.registered_agent_connections[connection_id]
            else:
                logger.warning("Agent already registered: %s", connection_id)
                # close the connection
                await edge_agent_connection.close()
                return

        self.registered_agent_connections[connection_id] = edge_agent_connection
        logger.info("Registered agent connection: %s", connection_id)

        async def keep_alive():
            while True:
                await asyncio.sleep(30)
                if edge_agent_connection.application_state != WebSocketState.CONNECTED:
                    break
                await edge_agent_connection.send_text(
                    RelayToEdgeAgentMessage(
                        inner=RtEKeepAliveMessage()
                    ).model_dump_json()
                )

        if not start_message.last_error or not (
            "Input tag 'successfully_ssh_connected' found using 'kind' does not match any of the expected tags"
            in start_message.last_error
            or "Input tag 'keep_alive' found using 'kind' does not match any of the expected tags"
            in start_message.last_error
        ):
            asyncio.create_task(keep_alive())

        msg_loop_task = asyncio.create_task(
            self._msg_loop(edge_agent_connection, connection_id)
        )
        return msg_loop_task, connection_id

    async def ws_for_edge_agents(self, websocket: WebSocket):
        return_value = await self.accept_ws_and_start_msg_loop_for_edge_agents(
            websocket
        )
        if not return_value:
            return
        msg_loop_task = return_value[0]
        return await msg_loop_task

    async def _msg_loop(self, edge_agent_connection: WebSocket, connection_id: str):
        while True:
            try:
                json_data = await edge_agent_connection.receive_text()
                logger.debug("Received message from agent: %s", json_data)
            except WebSocketDisconnect:
                logger.warning("Agent disconnected: %s", connection_id)
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
            logger.debug("Message received from agent: %s", message)

            try:
                await self.handle_edge_agent_message(
                    message, message_outer, connection_id
                )
            except Exception as e:
                logger.error("Error handling message: %s", e)
                import traceback

                traceback.print_exc()

    async def handle_edge_agent_message(
        self, message: BaseModel, message_outer: BaseModel, connection_id: str
    ):
        if isinstance(message, EtRTCPDataMessage):
            logger.debug("Received TCP data message from agent: %s", message)
            await self.handle_tcp_data_message(message)
        elif isinstance(message, EtRConnectionResetMessage):
            logger.info("Received connection reset message from agent: %s", message)
            await self.handle_connection_reset_message(message)
        elif isinstance(message, EtRKeepAliveMessage):
            logger.debug("Received keep alive message from agent: %s", message)
        elif message_outer is not None:
            # this is a request-response message
            # get queue
            logger.debug("Got request-response message: %s", message)
            response_queue = self.initiate_connection_answer_queues.get(
                message.connection_id
            )
            if response_queue is None:
                logger.warning(
                    "Got a message %s for connection_id: %s "
                    "but no corresponding waiting queue found",
                    message,
                    message.connection_id,
                )
                return
            # put message in queue
            await response_queue.put(message)
        elif self.CustomAgentToRelayMessage is not None and isinstance(
            message,
            self.CustomAgentToRelayMessage,
        ):
            try:
                await self.handle_custom_agent_message(message, connection_id)
            except NotImplementedError:
                logger.warning(
                    "Custom agent message handling not implemented: %s", message
                )
            except Exception as e:
                logger.error("Error handling custom agent message: %s", e)
        else:
            logger.warning("Unknown message received from agent: %s", message)

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
        connection = connection_class(
            connection_id, self, agent_connection, asyncio.get_event_loop()
        )
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
            logger.warning(
                "Unknown connection_id for TCP data: %s", message.connection_id
            )
            return
        connection = self.active_relayed_connections[message.connection_id]
        connection.fill_recv(base64.b64decode(message.data_base64))

    async def handle_connection_reset_message(self, message: EtRConnectionResetMessage):
        if message.connection_id not in self.active_relayed_connections:
            logger.warning(
                f"Unknown connection_id for connection reset: %s", message.connection_id
            )
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
        logger.debug(
            "Waiting for response for connection_id: %s",
            message.connection_id,
        )
        response = await response_queue.get()
        logger.debug("Got response for connection_id: %s", message.connection_id)
        del self.initiate_connection_answer_queues[message.connection_id]
        return response

    async def send_connection_message(
        self, agent_connection: WebSocket, message: RtETCPDataMessage
    ):
        await agent_connection.send_text(
            RelayToEdgeAgentMessage(inner=message).model_dump_json()
        )

    async def close_relayed_connection(
        self, connection_id: str, agent_connection: WebSocket
    ):
        # inform agent
        await agent_connection.send_text(
            RelayToEdgeAgentMessage(
                inner=RtEConnectionCloseMessage(
                    message="Connection closed by relay", connection_id=connection_id
                )
            ).model_dump_json()
        )
        # remove connection
        del self.active_relayed_connections[connection_id]

    async def handle_custom_agent_message(self, message: BaseModel, connection_id: str):
        raise NotImplementedError()

    async def get_agent_msg_loop_permission_and_create_connection_id(
        self, start_message: BaseModel, edge_agent_connection: WebSocket
    ) -> str | None:
        raise NotImplementedError()

    # access client stuff from here

    async def ws_for_access_clients(self, access_client_connection: WebSocket):
        await access_client_connection.accept()
        json_data = await access_client_connection.receive_text()
        message = AccessClientToRelayMessage.model_validate_json(json_data)
        logger.info("Message received from access client: %s", message)
        if not isinstance(message.inner, AtRStartMessage):
            logger.warning("Unknown message received from access client: %s", message)
            return
        start_message = message.inner
        # check if credentials are correct
        if not await self.get_access_client_permission(
            start_message, access_client_connection
        ):
            logger.warning("Access client not allowed: %s", start_message)

            await access_client_connection.close()
            return
        # check if the client is registered
        agent_connection_id = await self.get_agent_connection_id_for_access_client(
            start_message.connection_target
        )
        if not agent_connection_id in self.registered_agent_connections:
            logger.warning(
                "Agent not registered: %s (%s)",
                agent_connection_id,
                start_message.connection_target,
            )
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
                agent_connection_id=agent_connection_id,
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
            logger.info("Error creating connection: %s", e)
            await access_client_connection.send_text(
                RelayToAccessClientMessage(
                    inner=RtAErrorMessage(message=str(e))
                ).model_dump_json()
            )
            await access_client_connection.close()
            return

        logger.info("Connection created: %s", connection)
        self.active_access_client_connections[connection.id] = connection
        reader = asyncio.create_task(
            self.access_client_receive_thread(access_client_connection, connection)
        )
        while connection.id in self.active_access_client_connections:
            try:
                json_data = await access_client_connection.receive_text()
            except WebSocketDisconnect:
                logger.info("access client disconnected: %s", connection.id)
                if connection.id in self.active_access_client_connections:
                    del self.active_access_client_connections[connection.id]
                break
            message = AccessClientToRelayMessage.model_validate_json(json_data)
            if isinstance(message.inner, AtRTCPDataMessage):
                logger.debug(
                    "Received TCP data message from access client: %s", message
                )
                await connection.send(base64.b64decode(message.inner.data_base64))
            else:
                logger.warning(
                    "Unknown message received from access client: %s", message
                )
        if connection.id in self.active_access_client_connections:
            del self.active_access_client_connections[connection.id]
        await reader
        access_client_connection.close()

    async def access_client_receive_thread(
        self,
        access_client_connection: WebSocket,
        relayed_connection: TcpConnectionAsync,
    ):
        while relayed_connection.id in self.active_access_client_connections:
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
                logger.error(f"Error in receive_thread: {e}")
                import traceback

                traceback.print_exc()
                break
        if relayed_connection.id in self.active_access_client_connections:
            del self.active_access_client_connections[relayed_connection.id]

    async def get_access_client_permission(
        self, start_message: AtRStartMessage, access_client_connection
    ):
        raise NotImplementedError()

    async def get_agent_connection_id_for_access_client(self, connection_target: str):
        return connection_target
