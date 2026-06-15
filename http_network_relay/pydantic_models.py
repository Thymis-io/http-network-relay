import uuid
from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

# Maximum bytes read from a socket/stdin per relayed message. Larger chunks mean
# fewer WebSocket frames and less per-message JSON/base64 overhead for bulk
# transfers (e.g. nix store closures during deploy).
READ_CHUNK_SIZE = 65536


def encode_tcp_binary_frame(connection_id: str, payload: bytes) -> bytes:
    """Frame raw TCP payload for the relay<->agent channel as a binary WebSocket
    message: the 16-byte connection UUID followed by the raw bytes.
    The connection UUID prefix is required because many connections are
    multiplexed over the agent's single WebSocket.
    """
    return uuid.UUID(connection_id).bytes + payload


def decode_tcp_binary_frame(frame: bytes) -> tuple[str, bytes]:
    """Inverse of encode_tcp_binary_frame: returns (connection_id, payload)."""
    return str(uuid.UUID(bytes=bytes(frame[:16]))), bytes(frame[16:])


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
