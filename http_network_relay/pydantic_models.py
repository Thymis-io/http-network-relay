from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


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
