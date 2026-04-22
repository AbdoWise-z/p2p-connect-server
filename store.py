"""
store.py — all Redis interactions in one place.

Key layout:
  node:{uuid}              Hash   node info (ip, port, pubkey, joined_at)
  space:{pubkey}:nodes     Set    UUIDs of live nodes in this space
  pending:{conn_id}        Hash   hole-punch rendezvous (TTL = 30 s)
  signal:{uuid}            Pub/Sub channel per node
"""

import json
import uuid
import time
import redis.asyncio as aioredis
from typing import Optional
from models import Address, NodeInfo, Signal
from datetime import datetime, timezone
from config import settings


# ── connection pool (shared across all requests) ──────────────────────────────
pool: aioredis.Redis | None = None


async def init():
    global pool
    pool = aioredis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
    )
    await pool.ping()


async def close():
    if pool:
        await pool.aclose()


# ── helpers ───────────────────────────────────────────────────────────────────

def _node_key(node_uuid: str)   -> str: return f"node:{node_uuid}"
def _space_key(pubkey: str)     -> str: return f"space:{pubkey}:nodes"
def _pending_key(conn_id: str)  -> str: return f"pending:{conn_id}"
def _signal_channel(node_uuid)  -> str: return f"signal:{node_uuid}"


# ── node registry ─────────────────────────────────────────────────────────────

async def register_node(pubkey: str, address: Address) -> str:
    """Create a node entry and add it to the space. Returns new UUID."""
    node_uuid  = str(uuid.uuid4())
    joined_at  = time.time()

    async with pool.pipeline(transaction=True) as pipe:
        pipe.hset(_node_key(node_uuid), mapping={
            "uuid":      node_uuid,
            "pubkey":    pubkey,
            "ip":        address.ip,
            "port":      address.port,
            "joined_at": joined_at,
        })
        pipe.sadd(_space_key(pubkey), node_uuid)
        await pipe.execute()

    return node_uuid


async def unregister_node(node_uuid: str):
    """Remove a node entirely (on WS disconnect)."""
    node = await get_node(node_uuid)
    if not node:
        return
    async with pool.pipeline(transaction=True) as pipe:
        pipe.delete(_node_key(node_uuid))
        pipe.srem(_space_key(node["pubkey"]), node_uuid)
        await pipe.execute()


async def get_node(node_uuid: str) -> Optional[dict]:
    data = await pool.hgetall(_node_key(node_uuid))
    return data if data else None


async def update_node_address(node_uuid: str, address: Address):
    await pool.hset(_node_key(node_uuid), mapping={
        "ip":   address.ip,
        "port": address.port,
    })


async def list_space_nodes(pubkey: str, exclude_uuid: str | None = None) -> list[NodeInfo]:
    uuids = await pool.smembers(_space_key(pubkey))
    nodes = []
    for uid in uuids:
        if uid == exclude_uuid:
            continue
        data = await get_node(uid)
        if data:
            nodes.append(NodeInfo(
                uuid      = data["uuid"],
                address   = Address(ip=data["ip"], port=int(data["port"])),
                joined_at = datetime.fromtimestamp(float(data["joined_at"]), tz=timezone.utc),
            ))
    return nodes


# ── pending connections ───────────────────────────────────────────────────────

PENDING_TTL = 30   # seconds

async def create_pending(initiator_uuid: str, target_uuid: str,
                         initiator_addr: Address, target_addr: Address) -> str:
    conn_id = str(uuid.uuid4())
    await pool.hset(_pending_key(conn_id), mapping={
        "initiator_uuid": initiator_uuid,
        "target_uuid":    target_uuid,
        "initiator_ip":   initiator_addr.ip,
        "initiator_port": initiator_addr.port,
        "target_ip":      target_addr.ip,
        "target_port":    target_addr.port,
    })
    await pool.expire(_pending_key(conn_id), PENDING_TTL)
    return conn_id


async def consume_pending(conn_id: str) -> Optional[dict]:
    """Fetch and immediately delete a pending entry (one-shot)."""
    key  = _pending_key(conn_id)
    data = await pool.hgetall(key)
    if data:
        await pool.delete(key)
    return data if data else None


# ── pub/sub signaling ─────────────────────────────────────────────────────────

async def publish_signal(node_uuid: str, signal: Signal):
    """Publish a signal to any server instance holding node_uuid's WebSocket."""
    await pool.publish(_signal_channel(node_uuid), signal.model_dump_json())


def make_pubsub() -> aioredis.client.PubSub:
    return pool.pubsub()