import asyncio
import base64
import time
from typing import Optional, Type

import websockets
from pydantic import BaseModel, ValidationError
from websockets.asyncio.client import ClientConnection, connect

from .logging_utils import debug, error, info, warning
from .pydantic_models import (
    READ_CHUNK_SIZE,
    EdgeAgentToRelayMessage,
    EtRConnectionResetMessage,
    EtRInitiateConnectionErrorMessage,
    EtRInitiateConnectionOKMessage,
    EtRKeepAliveMessage,
    EtRStartMessage,
    EtRTCPDataMessage,
    RelayToEdgeAgentMessage,
    RtEConnectionCloseMessage,
    RtEInitiateConnectionMessage,
    RtEKeepAliveMessage,
    RtETCPDataMessage,
    decode_tcp_binary_frame,
    encode_tcp_binary_frame,
)


class EdgeAgent:
    CustomRelayToAgentMessage: Type[BaseModel] = None

    def __init__(self, relay_url):
        self.relay_url = relay_url
        self.active_connections: dict[
            str, tuple[asyncio.StreamReader, asyncio.StreamWriter]
        ] = {}
        self.websocket = None
        self.signal_connected = asyncio.Event()

    async def async_main(self):
        connection_delay = 1
        last_connection_attempt_time = 0
        last_error = None
        while True:
            info("Connecting to server...")
            # exponential backoff
            try:
                await self.connect_to_server(last_error)
            except ConnectionRefusedError as e:
                warning("Connection refused: %s", e)
                last_error = str(e)
            except Exception as e:
                error("Error: %s", e)
                last_error = str(e)
            for connection_id, (
                reader,
                writer,
            ) in self.active_connections.copy().items():
                info(
                    "Closing active connection: %s, due to disconnect from relay",
                    connection_id,
                )
                writer.close()
                del self.active_connections[connection_id]
            if self.websocket is not None:
                await self.websocket.close()
            self.signal_connected.clear()
            self.websocket = None
            await self.on_connection_closed()
            if time.time() - last_connection_attempt_time >= 60:
                # if it's been more than 60 seconds since the last connection attempt
                # then the connection has been stable
                # and we can reset the connection delay
                connection_delay = 1
            info("Connection closed, reconnecting in %s seconds", connection_delay)
            time.sleep(connection_delay)
            connection_delay = min(2 * connection_delay, 60)
            last_connection_attempt_time = time.time()

    async def connect_to_server(self, last_error):
        # disable compression, the data is usually already compressed
        async with connect(
            self.relay_url, max_size=2**32, compression=None
        ) as websocket:
            inner_start = EtRStartMessage.model_validate(
                (await self.create_start_message(last_error)).model_dump(),
                from_attributes=True,
            )
            inner_start.supports_binary = True
            start_message = EdgeAgentToRelayMessage(inner=inner_start)
            await websocket.send(start_message.model_dump_json())
            debug("Sent start message: %s", start_message)
            self.websocket = websocket
            self.signal_connected.set()

            async def keep_alive():
                await self.signal_connected.wait()
                while True:
                    await asyncio.sleep(10)
                    if self.websocket is None or self.websocket != websocket:
                        break
                    try:
                        await websocket.send(
                            EdgeAgentToRelayMessage(
                                inner=EtRKeepAliveMessage()
                            ).model_dump_json()
                        )
                    except websockets.exceptions.ConnectionClosed as e:
                        info("Connection closed: %s quitting keep alive", e)
                        break

            asyncio.create_task(keep_alive())

            while True:
                try:
                    json_data = await websocket.recv()
                except websockets.exceptions.ConnectionClosedError as e:
                    warning("Connection closed with error: %s", e)
                    break
                except websockets.exceptions.ConnectionClosedOK as e:
                    info("Connection closed OK: %s", e)
                    break
                if isinstance(json_data, (bytes, bytearray)):
                    # Hot path: raw TCP payload framed as connection_id + bytes.
                    conn_id, payload = decode_tcp_binary_frame(json_data)
                    await self.write_to_connection(conn_id, payload, websocket)
                    continue
                try:
                    message = RelayToEdgeAgentMessage.model_validate_json(
                        json_data
                    ).inner
                except ValidationError as e:
                    if self.CustomRelayToAgentMessage is None:
                        raise e
                    try:
                        message = self.CustomRelayToAgentMessage.model_validate_json(
                            json_data
                        )  # pylint: disable=E1101
                    except ValidationError as e:
                        error("Error while validating message: %s", e)
                        continue

                debug("Received message: %s", message)
                if isinstance(message, RtEInitiateConnectionMessage):
                    info("Received initiate connection message: %s", message)
                    try:
                        await self.initiate_connection(message, websocket)
                    except Exception as e:
                        error("Error while initiating connection: %s", e)
                        # send an error message back
                        await websocket.send(
                            EdgeAgentToRelayMessage(
                                inner=EtRInitiateConnectionErrorMessage(
                                    message=str(e),
                                    connection_id=message.connection_id,
                                )
                            ).model_dump_json()
                        )
                elif isinstance(message, RtETCPDataMessage):
                    await self.write_to_connection(
                        message.connection_id,
                        base64.b64decode(message.data_base64),
                        websocket,
                    )
                elif isinstance(message, RtEConnectionCloseMessage):
                    connection_close_message = message
                    debug(
                        "Received connection close message: %s",
                        connection_close_message,
                    )
                    if (
                        connection_close_message.connection_id
                        not in self.active_connections
                    ):
                        warning(
                            "Unknown connection_id: %s",
                            connection_close_message.connection_id,
                        )
                        continue
                    reader, writer = self.active_connections[
                        connection_close_message.connection_id
                    ]
                    info(
                        "Closing active connection: %s, due to connection close message from relay",
                        connection_close_message.connection_id,
                    )
                    writer.close()
                    del self.active_connections[connection_close_message.connection_id]
                elif isinstance(message, RtEKeepAliveMessage):
                    debug("Received keep alive message")
                elif self.CustomRelayToAgentMessage is not None and isinstance(
                    message, self.CustomRelayToAgentMessage
                ):
                    try:
                        await self.handle_custom_relay_message(message)
                    except NotImplementedError:
                        warning("Custom relay message not implemented")
                    except Exception as e:
                        error("Error while handling custom relay message: %s", e)
                else:
                    warning("Unknown message received: %s", message)

    async def write_to_connection(self, connection_id, data, server_websocket):
        if connection_id not in self.active_connections:
            warning("Unknown connection_id: %s", connection_id)
            return
        writer = self.active_connections[connection_id][1]
        writer.write(data)
        try:
            await writer.drain()
        except ConnectionResetError:
            warning("Connection reset while writing data")
            info(
                "Closing active connection: %s, due to connection reset", connection_id
            )
            writer.close()
            del self.active_connections[connection_id]
            await server_websocket.send(
                EdgeAgentToRelayMessage(
                    inner=EtRConnectionResetMessage(
                        message="Connection reset while writing data",
                        connection_id=connection_id,
                    )
                ).model_dump_json()
            )

    async def initiate_connection(
        self, message: RtEInitiateConnectionMessage, server_websocket: ClientConnection
    ):
        info(
            "Initiating connection to %s:%s using %s",
            message.target_ip,
            message.target_port,
            message.protocol,
        )
        if message.protocol != "tcp":
            error("Unsupported protocol: %s", message.protocol)
            raise NotImplementedError(f"Unsupported protocol: {message.protocol}")
        reader, writer = await asyncio.open_connection(
            message.target_ip, message.target_port, limit=READ_CHUNK_SIZE
        )
        self.active_connections[message.connection_id] = (reader, writer)
        info("Connected to %s:%s", message.target_ip, message.target_port)
        # send OK message back
        await server_websocket.send(
            EdgeAgentToRelayMessage(
                inner=EtRInitiateConnectionOKMessage(
                    connection_id=message.connection_id
                )
            ).model_dump_json()
        )

        # start async coroutine to read from the TCP connection and send it to the server
        async def read_from_tcp_and_send():
            try:
                while True:
                    data = await reader.read(READ_CHUNK_SIZE)
                    if not data:
                        break
                    if message.supports_binary:
                        await server_websocket.send(
                            encode_tcp_binary_frame(message.connection_id, data)
                        )
                    else:
                        await server_websocket.send(
                            EdgeAgentToRelayMessage(
                                inner=EtRTCPDataMessage(
                                    connection_id=message.connection_id,
                                    data_base64=base64.b64encode(data).decode("utf-8"),
                                )
                            ).model_dump_json()
                        )
            finally:
                # Close the writer and remove from active_connections regardless of
                # how the loop exits (EOF, exception, or relay-initiated close).
                # Without this, every SSH session that ends naturally leaks one fd
                # until the process hits its fd limit.
                writer.close()
                self.active_connections.pop(message.connection_id, None)
                try:
                    await server_websocket.send(
                        EdgeAgentToRelayMessage(
                            inner=EtRConnectionResetMessage(
                                message="Connection closed by target",
                                connection_id=message.connection_id,
                            )
                        ).model_dump_json()
                    )
                except Exception:
                    pass  # websocket may already be closed

        asyncio.create_task(read_from_tcp_and_send())

    async def create_start_message(
        self, last_error: Optional[str] = None
    ) -> EtRStartMessage:
        raise NotImplementedError()

    async def handle_custom_relay_message(self, message: BaseModel):
        raise NotImplementedError()

    async def on_connection_closed(self):
        pass
