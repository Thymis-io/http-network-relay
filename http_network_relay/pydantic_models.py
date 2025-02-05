from typing import Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class EdgeAgentToRelayMessage(BaseModel):
    inner: Union[
        "EtRStartMessage",
        "EtRInitiateConnectionErrorMessage",
        "EtRInitiateConnectionOKMessage",
        "EtRTCPDataMessage",
        "EtRConnectionResetMessage",
    ] = Field(discriminator="kind")


class EtRStartMessage(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    kind: Literal["start"] = "start"


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


RelayToEdgeAgentMessage_Inner = Union[
    "RtEInitiateConnectionMessage", "RtETCPDataMessage", "RtEConnectionCloseMessage"
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
