from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


class Address(BaseModel):
    ip:   str
    port: int


class RegisterRequest(BaseModel):
    """Node registers itself into a Space."""
    address:    Address
    public_key: str   # space identifier (known by server)


class RegisterResponse(BaseModel):
    uuid:  str        # assigned identity for this session
    nodes: list["NodeInfo"]   # existing peers in this space


class NodeInfo(BaseModel):
    uuid:      str
    address:   Address
    joined_at: datetime


class ConnectRequest(BaseModel):
    """Initiator asks server to broker a hole-punch with target_uuid."""
    initiator_uuid: str


class ResultRequest(BaseModel):
    """After punch attempt, node reports new confirmed address."""
    peer_uuid:   str
    success:     bool
    new_address: Address


class Signal(BaseModel):
    """Pushed over WebSocket to a node."""
    type:             str          # "connect" | "peer_joined" | "peer_left"
    connection_id:    Optional[str] = None
    your_address:     Optional[Address] = None
    peer_address:     Optional[Address] = None
    peer_uuid:        Optional[str] = None
    message:          Optional[str] = None


RegisterResponse.model_rebuild()