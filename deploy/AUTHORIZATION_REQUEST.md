# MCP Shared Memory Server - Authorization Request

## Summary

Request to deploy a shared memory coordination system for Claude Code AI assistants. This tool improves developer productivity by allowing AI agents to share knowledge, avoid duplicate work, and coordinate file edits across team members.

## What This System Does

### Purpose
When developers use Claude Code (Anthropic's AI coding assistant), each session starts fresh with no memory of previous work. This system provides persistent shared memory so that:

- AI assistants remember architectural decisions and patterns
- Knowledge learned by one developer's AI is available to others
- Multiple AI sessions don't accidentally edit the same files simultaneously
- Tasks and backlog items persist across sessions

### How It Works
1. Developer's Claude Code connects to the shared server via MCP (Model Context Protocol - an open standard by Anthropic)
2. AI registers its session and checks what others are working on
3. AI can query existing knowledge before implementing something new
4. AI stores learnings and decisions for future sessions
5. AI ends session with a summary for continuity

### What It Does NOT Do
- Does not execute code
- Does not access external networks (beyond Chroma database)
- Does not store credentials or secrets
- Does not modify developer machines
- Does not intercept or log AI conversations
- Does not require Anthropic API keys (clients use their own)

## Technical Components

| Component | Description | Port |
|-----------|-------------|------|
| MCP Server | Python application handling AI requests | 8080 |
| ChromaDB | Open-source vector database for storage | 8001 |

Both run as Docker containers on a single server.

### Data Stored
- Architecture documentation and decisions
- Code patterns and learnings
- Function references (name, location, purpose)
- Task backlog items
- Session metadata (which AI is working on what)

### Data NOT Stored
- Source code (only function signatures/descriptions)
- Credentials or secrets
- Personal information
- AI conversation content

## Network & Security

### Ports Required
- **8080** (inbound): MCP protocol endpoint for Claude Code clients
- **8001** (internal): ChromaDB, can be restricted to localhost

### Authentication
- No built-in authentication (relies on network-level security)
- Recommend: Restrict access via AWS Security Groups to developer IPs/VPN

### Data at Rest
- Stored in Docker volume on server
- ChromaDB uses SQLite + Parquet files
- No encryption at rest (recommend encrypted EBS if sensitive)

### Data in Transit
- HTTP (not HTTPS) by default
- Can add TLS via reverse proxy (nginx) if required

## Optional: Librarian Service

An optional enhancement that provides deeper code analysis.

### What It Does
- Analyzes registered functions to extract signatures, parameters, complexity
- Generates rich descriptions for better search
- Uses Claude API (Haiku model) for analysis

### File Access Requirement
- **Requires read access to source code files**
- Runs on host (not Docker) to access local filesystem
- Only reads files to analyze function signatures
- Does not modify any files

### If Not Installed
- System works without it
- Functions are registered with basic info only
- Developers can manually include code snippets when registering

## Resource Requirements

| Resource | Minimum | Recommended |
|----------|---------|-------------|
| CPU | 1 core | 2 cores |
| RAM | 1 GB | 2 GB |
| Disk | 5 GB | 20 GB |
| Instance | t3.small | t3.medium |

## Licensing

**This software is the personal property of Thomas Lemmons.**

- Created on personal time using personal resources
- Not a work-for-hire; not company property
- Licensed under MIT with Personal Ownership Clause

### Grant to Company
- Perpetual, irrevocable right to use and modify
- License survives any employment changes
- Company modifications belong to company
- No obligation for author to provide updates or support

### License Requirements
- Must retain copyright notice in source files
- Must include LICENSE file with any distribution

Full license text included in deployment package.

## Risk Assessment

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Data exposure | Low | Network restriction, no secrets stored |
| Service disruption | Low | Auto-restart, health checks, stateless design |
| Resource exhaustion | Low | Lightweight (< 500MB typical), bounded storage |
| Supply chain | Low | Minimal dependencies, all open source |
| Malicious input | Low | AI clients only, no user-facing interface |

## Deployment Process

1. Copy deployment package to server
2. Run `./install.sh` (checks prerequisites, builds containers)
3. Configure security groups to allow port 8080 from developer networks
4. Developers add server URL to their Claude Code config

Upgrade process preserves data and creates automatic backups.

## Support & Maintenance

- Self-contained system with health endpoint (`/health`)
- Standard Docker management (logs, restart, etc.)
- No external dependencies beyond Docker runtime
- Author available for questions (not contractual support)

## Approval Requested

- [ ] Security team approval
- [ ] Infrastructure/DevOps approval
- [ ] Tech lead approval
- [ ] Budget approval (if dedicated instance required)

---

**Contact:** Thomas Lemmons
**Date:** January 2026
**Version:** 1.0.0
