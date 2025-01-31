import asyncio
import base64
import os
import sys
from typing import Literal, Union

import websockets
from pydantic import BaseModel, Field
from websockets.asyncio.client import connect


class AccessClientToRelayMessage(BaseModel):
    inner: Union["AtRStartMessage", "AtRTCPDataMessage"] = Field(discriminator="kind")


class AtRStartMessage(BaseModel):
    kind: Literal["start"] = "start"
    connection_target: str
    target_ip: str
    target_port: int
    protocol: str
    secret: str


class AtRTCPDataMessage(BaseModel):
    kind: Literal["tcp_data"] = "tcp_data"
    data_base64: str


class RelayToAccessClientMessage(BaseModel):
    inner: Union["RtAErrorMessage", "RtAStartOKMessage", "RtATCPDataMessage"] = Field(
        discriminator="kind"
    )


class RtAErrorMessage(BaseModel):
    kind: Literal["error"] = "error"
    message: str


class RtAStartOKMessage(BaseModel):
    kind: Literal["start_ok"] = "start_ok"


class RtATCPDataMessage(BaseModel):
    kind: Literal["tcp_data"] = "tcp_data"
    data_base64: str


debug = False
if os.getenv("DEBUG") == "1":
    debug = True


def eprint(*args, only_debug=False, **kwargs):
    if (debug and only_debug) or (not only_debug):
        print(*args, file=sys.stderr, **kwargs)


class AccessClient:
    def __init__(
        self,
        target_host_identifier,
        target_ip,
        target_port,
        protocol,
        relay_url,
        secret,
    ):
        self.target_host_identifier = target_host_identifier
        self.target_ip = target_ip
        self.target_port = target_port
        self.protocol = protocol
        self.relay_url = relay_url
        self.secret = secret

    async def async_main(self):
        if self.relay_url is None:
            raise ValueError("relay_url is required")
        if self.secret is None:
            raise ValueError("secret is required")
        async with connect(self.relay_url) as websocket:
            start_message = AccessClientToRelayMessage(
                inner=AtRStartMessage(
                    connection_target=self.target_host_identifier,
                    target_ip=self.target_ip,
                    target_port=self.target_port,
                    protocol=self.protocol,
                    secret=self.secret,
                )
            )
            await websocket.send(start_message.model_dump_json())
            eprint(f"Sent start message: {start_message}")
            start_response_json = await websocket.recv()
            start_response = RelayToAccessClientMessage.model_validate_json(
                start_response_json
            )
            eprint(f"Received start response: {start_response}")
            if isinstance(start_response.inner, RtAStartOKMessage):
                eprint(f"Received OK message: {start_response}")
            elif isinstance(start_response.inner, RtAErrorMessage):
                eprint(f"Received error message: {start_response}")
                return

            # start async coroutine to read stdin and send it to the server
            async def read_stdin_and_send():
                loop = asyncio.get_event_loop()
                reader = asyncio.StreamReader()
                reader_protocol = asyncio.StreamReaderProtocol(reader)
                await loop.connect_read_pipe(lambda: reader_protocol, sys.stdin)
                while True:
                    data = await reader.read(1024)
                    if not data:
                        break
                    await websocket.send(
                        AccessClientToRelayMessage(
                            inner=AtRTCPDataMessage(
                                data_base64=base64.b64encode(data).decode("utf-8")
                            )
                        ).model_dump_json()
                    )

            read_stdin_and_send_task = asyncio.create_task(read_stdin_and_send())

            while True:
                try:
                    json_data = await websocket.recv()
                except websockets.exceptions.ConnectionClosedError as e:
                    eprint(f"Connection closed: Error: {e}")
                    break
                except websockets.exceptions.ConnectionClosedOK as e:
                    eprint(f"Connection closed: OK: {e}")
                    break
                message = RelayToAccessClientMessage.model_validate_json(json_data)
                eprint(f"Received message: {message}", only_debug=True)
                if isinstance(message.inner, RtATCPDataMessage):
                    tcp_data_message = message.inner
                    eprint(
                        f"Received TCP data message: {tcp_data_message}",
                        only_debug=True,
                    )
                    sys.stdout.buffer.write(
                        base64.b64decode(tcp_data_message.data_base64)
                    )
                    sys.stdout.flush()
                elif isinstance(message.inner, RtAErrorMessage):
                    eprint(f"Received error message: {message}")
                else:
                    eprint(f"Unknown message received: {message}")

            eprint("Exiting")
            read_stdin_and_send_task.cancel()

    def run(self):
        asyncio.run(self.async_main())
