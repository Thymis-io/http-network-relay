from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

# Maximum bytes read per relayed message. The access-client leg is pipe-bound
# (Linux pipes default to 64 KiB) and SSH frames in ~32-35 KiB packets. Kept
# modestly above that ceiling for the non-pipe-bound local TCP leg, while
# avoiding extra buffer memory and WebSocket head-of-line blocking on
# memory-constrained SBCs.
READ_CHUNK_SIZE = 128 * 1024


def encode_tcp_binary_frame(connection_id: str, payload: bytes) -> bytes:
    """Frame raw TCP payload for the relay<->agent channel as a binary WebSocket
    message: the 16-byte connection UUID followed by the raw bytes.
    The connection UUID prefix is required because many connections are
    multiplexed over the agent's single WebSocket.
    """
    return bytes.fromhex(connection_id.replace("-", "")) + payload


def decode_tcp_binary_frame(frame: bytes) -> tuple[str, memoryview]:
    """Inverse of encode_tcp_binary_frame: returns (connection_id, payload)."""
    if len(frame) < 16:
        raise ValueError(
            f"frame too short to contain a connection id: {len(frame)} bytes"
        )
    h = bytes(frame[:16]).hex()
    connection_id = f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
    return connection_id, memoryview(frame)[16:]


class EdgeAgentToRelayMessage(BaseModel):
    inner: Union[
        "EtRStartMessage",
        "EtRInitiateConnectionErrorMessage",
        "EtRInitiateConnectionOKMessage",
        "EtRTCPDataMessage",
        "EtRConnectionResetMessage",
        "EtRKeepAliveMessage",
    ] = Field(discriminator="kind")


class EtRStartMessage(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    kind: Literal["start"] = "start"
    last_error: Optional[str] = None
    supports_binary: bool = False


class EtRInitiateConnectionErrorMessage(BaseModel):
    kind: Literal["initiate_connection_error"] = "initiate_connection_error"
    message: str
    connection_id: str


class EtRInitiateConnectionOKMessage(BaseModel):
    kind: Literal["initiate_connection_ok"] = "initiate_connection_ok"
    connection_id: str


class EtRTCPDataMessage(BaseModel):
    kind: Literal["tcp_data"] = "tcp_data"
    connection_id: str
    data_base64: str


class EtRConnectionResetMessage(BaseModel):
    kind: Literal["connection_reset"] = "connection_reset"
    message: str
    connection_id: str


class EtRKeepAliveMessage(BaseModel):
    kind: Literal["keep_alive"] = "keep_alive"


RelayToEdgeAgentMessage_Inner = Union[
    "RtEInitiateConnectionMessage",
    "RtETCPDataMessage",
    "RtEConnectionCloseMessage",
    "RtEKeepAliveMessage",
]


class RelayToEdgeAgentMessage(BaseModel):
    inner: RelayToEdgeAgentMessage_Inner = Field(discriminator="kind")


class RtEInitiateConnectionMessage(BaseModel):
    kind: Literal["initiate_connection"] = "initiate_connection"
    target_ip: str
    target_port: int
    protocol: str
    connection_id: str
    supports_binary: bool = False


class RtETCPDataMessage(BaseModel):
    kind: Literal["tcp_data"] = "tcp_data"
    connection_id: str
    data_base64: str


class RtEConnectionCloseMessage(BaseModel):
    kind: Literal["connection_close"] = "connection_close"
    message: str
    connection_id: str


class RtEKeepAliveMessage(BaseModel):
    kind: Literal["keep_alive"] = "keep_alive"
