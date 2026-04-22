"""
main.py — NAT hole-punch signaling server
-----------------------------------------

REST API:
  POST   /spaces/{pubkey}/nodes          register a node, get peer list
  GET    /spaces/{pubkey}/nodes          list all nodes in a space
  DELETE /spaces/{pubkey}/nodes/{uuid}   manually unregister
  POST   /spaces/{pubkey}/connect/{target_uuid}   request hole-punch with peer
  POST   /nodes/{uuid}/result            report punch result + new address

WebSocket:
  WS /ws/{uuid}    persistent push channel — server notifies the node here
"""

import asyncio
import logging
import uuid as _uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware

import store
import ws_manager as wsm
from models import (
    Address, RegisterRequest, RegisterResponse,
    NodeInfo, ConnectRequest, ResultRequest, Signal,
)
from config import settings

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
log = logging.getLogger("main")


# ── startup / shutdown ────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await store.init()
    log.info("Redis connected: %s", settings.redis_url)
    log.info("Dev mode (open keys): %s", not settings.allowed_keys)
    yield
    await store.close()


app = FastAPI(
    title="NAT Hole-Punch Signaling Server",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── auth helper ───────────────────────────────────────────────────────────────

def verify_pubkey(pubkey: str):
    allowed = settings.allowed_keys
    if allowed and pubkey not in allowed:
        raise HTTPException(status_code=403, detail="Unknown public key")


# ── node registry ─────────────────────────────────────────────────────────────

@app.post("/spaces/{pubkey}/nodes", response_model=RegisterResponse)
async def register_node(pubkey: str, req: RegisterRequest):
    """
    A node announces itself to a Space.
    Returns its new UUID and the list of existing peers.
    """
    verify_pubkey(pubkey)

    node_uuid = await store.register_node(pubkey, req.address)
    peers     = await store.list_space_nodes(pubkey, exclude_uuid=node_uuid)

    log.info("REGISTER  uuid=%s  space=%s  addr=%s:%d  peers=%d",
             node_uuid, pubkey, req.address.ip, req.address.port, len(peers))

    # Notify existing peers that a new node joined
    signal = Signal(
        type      = "peer_joined",
        peer_uuid = node_uuid,
        peer_address = req.address,
        message   = f"New node {node_uuid} joined the space",
    )
    for peer in peers:
        await wsm.manager.send(peer.uuid, signal)

    return RegisterResponse(uuid=node_uuid, nodes=peers)


@app.get("/spaces/{pubkey}/nodes", response_model=list[NodeInfo])
async def list_nodes(pubkey: str):
    verify_pubkey(pubkey)
    return await store.list_space_nodes(pubkey)


@app.delete("/spaces/{pubkey}/nodes/{node_uuid}", status_code=204)
async def unregister_node(pubkey: str, node_uuid: str):
    verify_pubkey(pubkey)
    node = await store.get_node(node_uuid)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    await store.unregister_node(node_uuid)

    # Notify remaining peers
    peers  = await store.list_space_nodes(pubkey)
    signal = Signal(type="peer_left", peer_uuid=node_uuid)
    for peer in peers:
        await wsm.manager.send(peer.uuid, signal)


# ── hole-punch brokering ──────────────────────────────────────────────────────

@app.post("/spaces/{pubkey}/connect/{target_uuid}", status_code=202)
async def request_connection(pubkey: str, target_uuid: str, req: ConnectRequest):
    """
    Initiator asks the server to broker a simultaneous-open with target_uuid.

    Steps:
      1. Validate both nodes exist in this space
      2. Create a short-lived pending entry
      3. Push a "connect" signal to BOTH nodes with each other's address
      4. Delete the pending entry (it was one-shot)
    """
    verify_pubkey(pubkey)

    initiator = await store.get_node(req.initiator_uuid)
    target    = await store.get_node(target_uuid)

    if not initiator:
        raise HTTPException(status_code=404, detail="Initiator not found")
    if not target:
        raise HTTPException(status_code=404, detail="Target not found")
    if initiator["pubkey"] != pubkey or target["pubkey"] != pubkey:
        raise HTTPException(status_code=403, detail="Nodes not in this space")

    initiator_addr = Address(ip=initiator["ip"], port=int(initiator["port"]))
    target_addr    = Address(ip=target["ip"],    port=int(target["port"]))

    conn_id = await store.create_pending(
        req.initiator_uuid, target_uuid,
        initiator_addr, target_addr,
    )

    log.info("CONNECT  conn=%s  %s → %s", conn_id, req.initiator_uuid, target_uuid)

    # Signal the TARGET: "someone wants to punch through to you"
    await wsm.manager.send(target_uuid, Signal(
        type          = "connect",
        connection_id = conn_id,
        your_address  = target_addr,
        peer_address  = initiator_addr,
        peer_uuid     = req.initiator_uuid,
        message       = "Initiate simultaneous open NOW",
    ))

    # Signal the INITIATOR: "go ahead, punch now"
    await wsm.manager.send(req.initiator_uuid, Signal(
        type          = "connect",
        connection_id = conn_id,
        your_address  = initiator_addr,
        peer_address  = target_addr,
        peer_uuid     = target_uuid,
        message       = "Initiate simultaneous open NOW",
    ))

    # Entry has served its purpose — delete it
    await store.consume_pending(conn_id)

    return {"connection_id": conn_id, "status": "signaled"}


# ── post-punch result ─────────────────────────────────────────────────────────

@app.post("/nodes/{node_uuid}/result", status_code=200)
async def report_result(node_uuid: str, req: ResultRequest):
    """
    After a punch attempt, the node reports its confirmed public address.
    This updates the registry so future nodes see the correct address.
    """
    node = await store.get_node(node_uuid)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    await store.update_node_address(node_uuid, req.new_address)

    status = "success" if req.success else "failed"
    log.info("RESULT  uuid=%s  peer=%s  status=%s  new_addr=%s:%d",
             node_uuid, req.peer_uuid, status,
             req.new_address.ip, req.new_address.port)

    return {"status": status, "updated_address": req.new_address}


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws/{node_uuid}")
async def websocket_endpoint(ws: WebSocket, node_uuid: str):
    """
    Persistent push channel.  The node keeps this open for the lifetime of
    its session.  The server delivers Signal messages here whenever another
    node requests a connection or the space topology changes.
    """
    node = await store.get_node(node_uuid)
    if not node:
        await ws.close(code=4404, reason="Unknown node UUID — register first")
        return

    await wsm.manager.connect(node_uuid, ws)
    log.info("WS OPEN  uuid=%s", node_uuid)

    try:
        # Keep the socket alive; we don't expect messages from the client here
        # but we handle pings / unexpected closes cleanly
        while True:
            data = await ws.receive_text()
            # Optional: client can send {"type":"ping"} to test liveness
            if data.strip() == '{"type":"ping"}':
                await ws.send_text('{"type":"pong"}')

    except WebSocketDisconnect:
        log.info("WS CLOSE  uuid=%s", node_uuid)
    finally:
        await wsm.manager.disconnect(node_uuid)
        # Unregister node and notify peers
        pubkey = node["pubkey"]
        await store.unregister_node(node_uuid)
        peers  = await store.list_space_nodes(pubkey)
        signal = Signal(type="peer_left", peer_uuid=node_uuid)
        for peer in peers:
            await wsm.manager.send(peer.uuid, signal)


# ── health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    try:
        await store.pool.ping()
        redis_ok = True
    except Exception:
        redis_ok = False
    return {
        "status":      "ok" if redis_ok else "degraded",
        "redis":       redis_ok,
        "ws_sessions": len(wsm.manager._local),
    }