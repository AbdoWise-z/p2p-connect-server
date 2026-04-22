# NAT Hole-Punch Signaling Server

A lightweight signaling server that brokers UDP hole-punching between peers.
It **never relays file data** — it only introduces peers to each other.

## Quick start (local)

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Start Redis (or use docker-compose)
docker compose up redis -d

# 3. Run the server
uvicorn main:app --reload
```

Or run everything at once:
```bash
docker compose up
```

Scale to 3 server instances (tests the Redis pub/sub backplane):
```bash
docker compose up --scale server=3
```

---

## API Reference

### Register a node
```
POST /spaces/{pubkey}/nodes
{
  "address": { "ip": "1.2.3.4", "port": 5000 },
  "public_key": "{pubkey}"
}
→ { "uuid": "...", "nodes": [...] }
```

### List peers
```
GET /spaces/{pubkey}/nodes
→ [ { "uuid": "...", "address": {...}, "joined_at": "..." }, ... ]
```

### Request hole-punch with a peer
```
POST /spaces/{pubkey}/connect/{target_uuid}
{ "initiator_uuid": "your-uuid" }
→ 202  { "connection_id": "...", "status": "signaled" }
```
Both you and the target will immediately receive a `connect` signal
over the WebSocket telling you to fire simultaneously.

### Report punch result
```
POST /nodes/{uuid}/result
{
  "peer_uuid": "...",
  "success": true,
  "new_address": { "ip": "1.2.3.4", "port": 5000 }
}
```

### WebSocket (persistent push channel)
```
WS /ws/{uuid}
```
Connect **after** registering. Keep open for the lifetime of the session.
You will receive JSON `Signal` objects:

| type         | when                                         |
|--------------|----------------------------------------------|
| `connect`    | server wants you to punch to `peer_address`  |
| `peer_joined`| a new node joined your space                 |
| `peer_left`  | a node disconnected from your space          |

Send `{"type":"ping"}` to get `{"type":"pong"}` (liveness check).

---

## Signal format
```json
{
  "type": "connect",
  "connection_id": "uuid",
  "your_address":  { "ip": "...", "port": ... },
  "peer_address":  { "ip": "...", "port": ... },
  "peer_uuid":     "uuid",
  "message":       "Initiate simultaneous open NOW"
}
```

---

## Deploying for free (Fly.io + Upstash)

1. Create an [Upstash](https://upstash.com) Redis database → copy the `rediss://` URL
2. Install [flyctl](https://fly.io/docs/hands-on/install-flyctl/) and run `fly launch`
3. Set secrets:
   ```bash
   fly secrets set REDIS_URL="rediss://default:token@host.upstash.io:6379"
   fly secrets set KNOWN_PUBKEYS="your-space-key"
   ```
4. Deploy: `fly deploy`

Health check endpoint: `GET /health`
Interactive API docs: `GET /docs`
