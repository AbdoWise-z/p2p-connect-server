"""
ws_manager.py — manages WebSocket connections and Redis pub/sub fan-out.

Each node holds one persistent WS connection to this server.
When a signal needs to reach a node that lives on a *different* server
instance, we publish to Redis and the correct instance delivers it locally.

                Node A ──WS──► Instance 1
                                   │  publish signal:{B}
                                   ▼
                                 Redis
                                   │  subscribed to signal:{B}
                                   ▼
                Node B ──WS──► Instance 2
"""

import asyncio
import json
import logging
from fastapi import WebSocket
from models import Signal
import store

log = logging.getLogger("ws_manager")


class ConnectionManager:
    def __init__(self):
        # uuid → WebSocket (only for connections on THIS instance)
        self._local: dict[str, WebSocket] = {}
        # uuid → asyncio.Task (Redis subscriber task per node)
        self._tasks: dict[str, asyncio.Task] = {}

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self, node_uuid: str, ws: WebSocket):
        await ws.accept()
        self._local[node_uuid] = ws
        # spin up a Redis subscriber so signals from other instances arrive here
        task = asyncio.create_task(
            self._redis_listener(node_uuid),
            name=f"redis-sub-{node_uuid}",
        )
        self._tasks[node_uuid] = task
        log.info("WS connected: %s  (local=%d)", node_uuid, len(self._local))

    async def disconnect(self, node_uuid: str):
        self._local.pop(node_uuid, None)
        task = self._tasks.pop(node_uuid, None)
        if task and not task.done():
            task.cancel()
        log.info("WS disconnected: %s  (local=%d)", node_uuid, len(self._local))

    # ── sending ───────────────────────────────────────────────────────────────

    async def send(self, node_uuid: str, signal: Signal):
        """
        Deliver a signal to node_uuid.
        If it's local → send directly.
        Otherwise    → publish to Redis (another instance will pick it up).
        """
        ws = self._local.get(node_uuid)
        if ws:
            try:
                await ws.send_text(signal.model_dump_json())
                return
            except Exception as e:
                log.warning("Local WS send failed for %s: %s", node_uuid, e)
                await self.disconnect(node_uuid)
        else:
            log.debug("Local WS not found for %s", node_uuid)

        # Not local (or just died) — go via Redis
        await store.publish_signal(node_uuid, signal)

    # ── Redis subscriber (one per connected node) ─────────────────────────────

    async def _redis_listener(self, node_uuid: str):
        """
        Subscribe to signal:{node_uuid} on Redis.
        Any message published by another instance is forwarded to the local WS.
        """
        pubsub = store.make_pubsub()
        channel = f"signal:{node_uuid}"
        await pubsub.subscribe(channel)
        try:
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                ws = self._local.get(node_uuid)
                if ws is None:
                    break   # node disconnected from this instance
                try:
                    await ws.send_text(message["data"])
                except Exception as e:
                    log.warning("Redis→WS forward failed for %s: %s", node_uuid, e)
                    break
        except asyncio.CancelledError:
            pass
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()


# Singleton used across the app
manager = ConnectionManager()