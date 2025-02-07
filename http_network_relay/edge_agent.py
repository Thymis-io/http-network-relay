import asyncio
import base64
import os
import sys
import time
from typing import Optional, Type

import websockets
from pydantic import BaseModel, ValidationError
from websockets.asyncio.client import ClientConnection, connect

from .pydantic_models import (
    EdgeAgentToRelayMessage,
    EtRConnectionResetMessage,
    EtRInitiateConnectionErrorMessage,
    EtRInitiateConnectionOKMessage,
    EtRStartMessage,
    EtRTCPDataMessage,
    RelayToEdgeAgentMessage,
    RtEConnectionCloseMessage,
    RtEInitiateConnectionMessage,
    RtETCPDataMessage,
)

debug = False
if os.getenv("DEBUG") == "1":
    debug = True


def eprint(*args, only_debug=False, **kwargs):
    if (debug and only_debug) or (not only_debug):
        print(*args, file=sys.stderr, **kwargs)


class EdgeAgent:
    CustomRelayToAgentMessage: Type[BaseModel] = None

    def __init__(self, relay_url):
        self.relay_url = relay_url
        self.active_connections: dict[
            str, tuple[asyncio.StreamReader, asyncio.StreamWriter]
        ] = {}
        self.websocket = None

    async def async_main(self):
        connection_delay = 1
        last_connection_attempt_time = 0
        last_error = None
        while True:
            eprint("Connecting to server...")
            # exponential backoff
            try:
                await self.connect_to_server(last_error)
            except ConnectionRefusedError as e:
                eprint(f"Connection refused: {e}")
                last_error = str(e)
            except Exception as e:
                eprint(f"Error: {e}")
                last_error = str(e)
            self.websocket = None
            if time.time() - last_connection_attempt_time >= 60:
                # if it's been more than 60 seconds since the last connection attempt
                # then the connection has been stable
                # and we can reset the connection delay
                connection_delay = 1
            eprint(f"Connection closed, reconnecting in {connection_delay} seconds")
            time.sleep(connection_delay)
            connection_delay = min(2 * connection_delay, 60)
            last_connection_attempt_time = time.time()

    async def connect_to_server(self, last_error):
        async with connect(self.relay_url) as websocket:
            start_message = EdgeAgentToRelayMessage(
                inner=EtRStartMessage.model_validate(
                    (await self.create_start_message(last_error)).model_dump(),
                    from_attributes=True,
                )
            )
            await websocket.send(start_message.model_dump_json())
            eprint(f"Sent start message: {start_message}")
            self.websocket = websocket

            while True:
                try:
                    json_data = await websocket.recv()
                except websockets.exceptions.ConnectionClosedError as e:
                    eprint(f"Connection closed with error: {e}")
                    break
                except websockets.exceptions.ConnectionClosedOK as e:
                    eprint(f"Connection closed OK: {e}")
                    break
                try:
                    message = RelayToEdgeAgentMessage.model_validate_json(
                        json_data
                    ).inner
                except ValidationError as e:
                    if self.CustomRelayToAgentMessage is None:
                        raise e
                    message = self.CustomRelayToAgentMessage.model_validate_json(
                        json_data
                    )  # pylint: disable=E1101

                eprint(f"Received message: {message}", only_debug=True)
                if isinstance(message, RtEInitiateConnectionMessage):
                    eprint(f"Received initiate connection message: {message}")
                    try:
                        await self.initiate_connection(message, websocket)
                    except Exception as e:
                        eprint(f"Error while initiating connection: {e}")
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
                    tcp_data_message = message
                    eprint(
                        f"Received TCP data message: {tcp_data_message}",
                        only_debug=True,
                    )
                    # associate the connection_id with the websocket
                    if tcp_data_message.connection_id not in self.active_connections:
                        eprint(
                            f"Unknown connection_id: {tcp_data_message.connection_id}"
                        )
                        continue
                    reader, writer = self.active_connections[
                        tcp_data_message.connection_id
                    ]
                    writer.write(base64.b64decode(tcp_data_message.data_base64))
                    try:
                        await writer.drain()
                    except ConnectionResetError:
                        eprint(f"Connection reset while writing data")
                        writer.close()
                        del self.active_connections[tcp_data_message.connection_id]
                        await websocket.send(
                            EdgeAgentToRelayMessage(
                                inner=EtRConnectionResetMessage(
                                    message="Connection reset while writing data",
                                    connection_id=tcp_data_message.connection_id,
                                )
                            ).model_dump_json()
                        )
                elif isinstance(message, RtEConnectionCloseMessage):
                    connection_close_message = message
                    eprint(
                        f"Received connection close message: {connection_close_message}",
                        only_debug=True,
                    )
                    if (
                        connection_close_message.connection_id
                        not in self.active_connections
                    ):
                        eprint(
                            f"Unknown connection_id: {connection_close_message.connection_id}"
                        )
                        continue
                    reader, writer = self.active_connections[
                        connection_close_message.connection_id
                    ]
                    writer.close()
                    del self.active_connections[connection_close_message.connection_id]
                elif self.CustomRelayToAgentMessage is not None and isinstance(
                    message, self.CustomRelayToAgentMessage
                ):
                    try:
                        await self.handle_custom_relay_message(message)
                    except NotImplementedError:
                        eprint(f"Custom relay message not implemented")
                    except Exception as e:
                        eprint(f"Error while handling custom relay message: {e}")
                else:
                    eprint(f"Unknown message received: {message}")

    async def initiate_connection(
        self, message: RtEInitiateConnectionMessage, server_websocket: ClientConnection
    ):
        eprint(
            f"Initiating connection to {message.target_ip}:{message.target_port} using {message.protocol}"
        )
        if message.protocol != "tcp":
            eprint(f"Unsupported protocol: {message.protocol}")
            raise NotImplementedError(f"Unsupported protocol: {message.protocol}")
        reader, writer = await asyncio.open_connection(
            message.target_ip, message.target_port
        )
        self.active_connections[message.connection_id] = (reader, writer)
        eprint(f"Connected to {message.target_ip}:{message.target_port}")
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
            while True:
                data = await reader.read(1024)
                if not data:
                    break
                await server_websocket.send(
                    EdgeAgentToRelayMessage(
                        inner=EtRTCPDataMessage(
                            connection_id=message.connection_id,
                            data_base64=base64.b64encode(data).decode("utf-8"),
                        )
                    ).model_dump_json()
                )

        asyncio.create_task(read_from_tcp_and_send())

    async def create_start_message(
        self, last_error: Optional[str] = None
    ) -> EtRStartMessage:
        raise NotImplementedError()

    async def handle_custom_relay_message(self, message: BaseModel):
        raise NotImplementedError()
