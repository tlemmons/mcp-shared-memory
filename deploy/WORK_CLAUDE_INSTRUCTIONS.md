# Instructions for Work Claude: Deploy MCP Shared Memory Server

You are helping deploy an MCP Shared Memory Server to AWS. This server allows Claude Code agents to coordinate, share knowledge, and avoid duplicate work.

## What You're Deploying

A two-container Docker setup:
1. **Chroma** - Vector database for storing memories (port 8001)
2. **MCP Server** - FastMCP server that Claude Code connects to (port 8080)

## Files You Need

You need these files in a folder (e.g., `/tmp/mcp-deploy/`):

### 1. `docker-compose.yml`
```yaml
services:
  chroma:
    image: chromadb/chroma:latest
    container_name: mcp-chroma
    ports:
      - "8001:8000"
    volumes:
      - chroma-data:/chroma/chroma
    environment:
      - ANONYMIZED_TELEMETRY=false
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/api/v2"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s

  mcp-server:
    build: .
    container_name: mcp-server
    ports:
      - "8080:8080"
    environment:
      - CHROMA_HOST=chroma
      - CHROMA_PORT=8000
    depends_on:
      chroma:
        condition: service_healthy
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s

volumes:
  chroma-data:
```

### 2. `Dockerfile`
```dockerfile
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

ENV CHROMA_HOST=chroma
ENV CHROMA_PORT=8000

EXPOSE 8080

CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "8080"]
```

### 3. `requirements.txt`
```
mcp[cli]>=1.0.0
chromadb>=0.4.0
pydantic>=2.0.0
uvicorn>=0.30.0
starlette>=0.38.0
```

### 4. `server.py`
This is a large file (~104KB). The user will provide it to you. It's the main MCP server code.

## Installation Steps

1. **Check prerequisites:**
```bash
docker --version
docker compose version
```

2. **Create and enter deploy directory:**
```bash
mkdir -p /opt/mcp-memory
cd /opt/mcp-memory
```

3. **Copy all files** (docker-compose.yml, Dockerfile, requirements.txt, server.py) to this directory.

4. **Build and start:**
```bash
docker compose build
docker compose up -d
```

5. **Verify health:**
```bash
curl http://localhost:8080/health
```

Expected response:
```json
{"status":"healthy","chroma":"healthy","active_sessions":0,"active_locks":0,"active_signals":0}
```

## Configure Claude Code Clients

Each developer/Claude needs this in `~/.claude.json` or `.mcp.json`:

```json
{
  "mcpServers": {
    "shared-memory": {
      "type": "http",
      "url": "http://<server-ip>:8080/mcp"
    }
  }
}
```

Replace `<server-ip>` with the actual AWS server IP or hostname.

## Available Tools

Once connected, Claudes have access to:

| Tool | Purpose |
|------|---------|
| `memory_start_session` | Start session (required first) |
| `memory_end_session` | End session with summary |
| `memory_query` | Search knowledge base |
| `memory_store` | Store architecture/patterns |
| `memory_record_learning` | Quick learning capture |
| `memory_register_function` | Register function reference |
| `memory_find_function` | Find existing functions |
| `memory_lock_files` | Lock files for editing |
| `memory_unlock_files` | Release file locks |
| `memory_add_backlog_item` | Add task to backlog |
| `memory_list_backlog` | View backlog items |
| `memory_update_backlog_item` | Update backlog item |
| `memory_complete_backlog_item` | Complete backlog item |
| `memory_get_active_work` | See what others are working on |
| `memory_update_work` | Update your work status |

## Management Commands

```bash
cd /opt/mcp-memory

# View logs
docker compose logs -f

# Restart
docker compose restart

# Stop
docker compose down

# Check status
docker compose ps
```

## Upgrading

When you receive updated files:

1. Stop services: `docker compose down`
2. Backup: `cp server.py server.py.bak`
3. Replace files with new versions
4. Rebuild: `docker compose build`
5. Start: `docker compose up -d`
6. Verify: `curl http://localhost:8080/health`

## Troubleshooting

**Container won't start:**
```bash
docker compose logs mcp-server
docker compose logs chroma
```

**Port already in use:**
Edit docker-compose.yml to change ports (e.g., 8080:8080 → 9080:8080)

**Connection refused from client:**
- Check AWS security group allows ports 8080, 8001
- Check server firewall (ufw, iptables)
- Verify correct IP in client config
