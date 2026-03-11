# MCP Shared Memory Server - Deployment Package

A shared memory system for Claude Code agents to coordinate, share knowledge, and avoid duplicate work.

## Features

- **Session coordination** - Track active Claude sessions, detect file conflicts
- **Knowledge base** - Store/query architecture docs, learnings, patterns
- **Function references** - Register and find functions across projects
- **Backlog management** - Track tasks for humans and agents
- **File locking** - Prevent conflicting edits

## Requirements

- Docker
- Docker Compose (v1 or v2)
- curl

## Quick Install

```bash
# Copy deploy folder to your server
scp -r deploy/ user@server:/tmp/mcp-deploy

# SSH to server and run install
ssh user@server
cd /tmp/mcp-deploy
chmod +x install.sh
./install.sh
```

Default install location: `/opt/mcp-memory`

Set `INSTALL_DIR` to change:
```bash
INSTALL_DIR=/home/user/mcp-memory ./install.sh
```

## Configure Claude Code

Add to `~/.claude.json` (global) or `.mcp.json` (per-project):

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

## Services

| Service | Port | Description |
|---------|------|-------------|
| MCP Server | 8080 | Claude Code connects here |
| Chroma | 8001 | Vector database (internal) |

## Management

```bash
cd /opt/mcp-memory

# View logs
docker compose logs -f

# Restart
docker compose restart

# Stop
docker compose down

# Start
docker compose up -d

# Check health
curl http://localhost:8080/health
```

## Upgrading

When you have a new version:

```bash
cd /opt/mcp-memory
./upgrade.sh /path/to/new/deploy-folder
```

The upgrade script will:
1. Backup current files
2. Copy new files
3. Rebuild and restart containers
4. Verify health

To rollback, follow the instructions printed by the upgrade script.

## Data Persistence

Chroma data is stored in a Docker volume `mcp-memory_chroma-data`.

To backup:
```bash
docker run --rm -v mcp-memory_chroma-data:/data -v $(pwd):/backup alpine tar czf /backup/chroma-backup.tar.gz /data
```

To restore:
```bash
docker run --rm -v mcp-memory_chroma-data:/data -v $(pwd):/backup alpine tar xzf /backup/chroma-backup.tar.gz -C /
```

## Ports

If you need different ports, edit `docker-compose.yml`:

```yaml
services:
  chroma:
    ports:
      - "9001:8000"  # Change 9001 to your port
  mcp-server:
    ports:
      - "9080:8080"  # Change 9080 to your port
```

## Security Notes

- By default, ports are exposed to all interfaces
- For production, restrict with firewall rules or AWS security groups
- No authentication built-in - rely on network-level security

## Troubleshooting

**Container won't start:**
```bash
docker compose logs mcp-server
docker compose logs chroma
```

**Health check fails:**
```bash
curl -v http://localhost:8080/health
curl -v http://localhost:8001/api/v2
```

**Reset everything:**
```bash
docker compose down -v  # Warning: deletes all data
docker compose up -d
```

## Version

See `VERSION` file for current version.
