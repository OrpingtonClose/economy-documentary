# Tube Archivist — Vast.ai Deployment

Deploy a self-hosted [Tube Archivist](https://github.com/tubearchivist/tubearchivist) instance on a cheap Vast.ai VM for YouTube video archiving.

## Prerequisites

- `VAST_API_KEY` environment variable set
- `pip install vastai` (Vast.ai CLI)
- SSH key configured for Vast.ai

## Quick Start

```bash
# 1. Deploy TA on Vast.ai (~5 min)
export VAST_API_KEY=your_key
python3 scripts/tube-archivist/deploy.py

# 2. Subscribe to channels
python3 scripts/tube-archivist/setup_channels.py --channels channels.txt --start-download

# 3. Verify
python3 scripts/tube-archivist/deploy.py --status
```

## Architecture

Tube Archivist runs as a **native install** on Vast.ai (not Docker — Vast.ai containers
lack the `SYS_ADMIN` capability required for Docker-in-Docker).

```
┌──────────────────────────────────────────┐
│         Vast.ai VM ($0.04-0.10/hr)       │
│   image: nvidia/cuda:12.4-ubuntu22.04    │
│                                          │
│  Python 3.12 venv: /opt/ta-venv          │
│                                          │
│  ┌──────────────┐  ┌──────────────────┐  │
│  │ TubeArchivist│  │  Redis 6.x       │  │
│  │  :8000       │  │  :6379           │  │
│  │  (uvicorn)   │  │  (apt install)   │  │
│  └──────┬───────┘  └──────────────────┘  │
│         │                                │
│  ┌──────┴──────────┐  ┌───────────────┐  │
│  │ElasticSearch 8.x│  │ Celery worker │  │
│  │  :9200          │  │ + beat        │  │
│  │  (apt install)  │  │ (ta-venv)     │  │
│  └─────────────────┘  └───────────────┘  │
│                                          │
│  /data/ta/media  ← downloaded videos     │
│  /data/ta/cache  ← TA cache             │
│  /data/ta/es     ← ES data              │
│  /data/ta/redis  ← Redis data           │
│  /opt/tubearchivist ← TA source clone    │
└──────────────────────────────────────────┘
         │
         │ SSH tunnel → REST API (port 8000)
         ▼
┌──────────────────────────────────────────┐
│  pipeline/youtube_downloader.py          │
│  (TubeArchivistClient)                   │
│  - List archived videos                  │
│  - Stream video files                    │
│  - Fetch metadata + subtitles            │
└──────────────────────────────────────────┘
```

### Access via SSH Tunnel

TA port 8000 is not directly exposed. Connect via SSH tunnel:

```bash
ssh -o StrictHostKeyChecking=no -L 8000:localhost:8000 -p <SSH_PORT> root@<SSH_HOST> -N
# Then access TA at http://localhost:8000
```

The `youtube_downloader.py` handles tunnel setup automatically when using TA backend.

## CLI Reference

### deploy.py

| Command | Description |
|---------|-------------|
| `deploy.py` | Deploy new TA instance (finds cheapest VM) |
| `deploy.py --instance-id ID` | Deploy on an existing Vast.ai instance |
| `deploy.py --status` | Check TA health and VM status |
| `deploy.py --destroy` | Destroy VM and remove connection file |
| `deploy.py --ssh` | Print SSH command for the VM |
| `deploy.py --get-token` | Fetch TA API token from running instance |

### setup_channels.py

| Command | Description |
|---------|-------------|
| `setup_channels.py --channels FILE` | Subscribe to channels listed in FILE |
| `setup_channels.py --channels FILE --start-download` | Subscribe + trigger download |
| `setup_channels.py --ta-url URL --ta-token TOKEN --channels FILE` | Use explicit connection |

### Channel file format

One channel ID or URL per line. Comments start with `#`:

```
# Financial channels
UCvnFGEzKkS_IvhPjkRaGTmg
https://youtube.com/channel/UC4sS8q...
@BenjaminCowen
```

## Native Install Details

Since Vast.ai containers cannot run Docker-in-Docker, TA is installed natively:

1. **Python 3.12** installed via `deadsnakes` PPA (TA requires Django 6.0+)
2. **Virtual env** at `/opt/ta-venv` with all TA Python dependencies
3. **Elasticsearch 8.x** installed via official apt repo, runs as `elasticsearch` user
4. **Redis 6.x** installed via apt
5. **TA source** cloned from GitHub to `/opt/tubearchivist`
6. **Uvicorn** serves TA on port 8000 with 2 workers
7. **Celery** worker + beat for background tasks (channel scanning, downloads)

### Boot Script

`/opt/boot_ta.sh` starts all services in order: Redis → ES → Celery → Uvicorn.
Copy this to `/root/onstart.sh` for auto-start on instance reboot.

### Important Notes

- YouTube rate-limits datacenter IPs. Consider importing browser cookies
  (`cookie_import` in TA settings) or using residential proxies
- The pipeline's Apify and Bright Data backends serve as fallbacks when TA
  encounters rate limits

## Cost Estimates

| Resource | Cost |
|----------|------|
| Cheap GPU VM (needed for CUDA image) | $0.04-0.10/hr |
| Running 24/7 | $0.96-2.40/day |
| Storage (100GB disk) | included in VM |

Tip: stop the VM when not downloading, restart when needed. TA data persists
on the VM disk while the instance exists (even when stopped).

## Troubleshooting

**"No suitable instances found"**
- Relax search criteria — try during off-peak hours or increase max $/hr

**TA health check times out**
- SSH in and check: `tail -f /tmp/ta_startup.log`
- ElasticSearch needs 1-2 min to start: `tail -f /var/log/elasticsearch/tubearchivist.log`

**Cannot get API token**
- TA needs to fully initialize first (can take 2-3 min after health check passes)
- Run: `python3 scripts/tube-archivist/deploy.py --get-token`

**Docker-in-Docker not supported**
- Vast.ai containers lack `CAP_SYS_ADMIN` — Docker images cannot be pulled
- Use the native install approach (this is what deploy.py does)
- For KVM VM support, use Vast.ai's KVM VM templates (more expensive)

**SSH connection refused**
- Instance may still be booting — wait 1-2 min
- Check instance status: `vastai show instance ID`

**"Failed to get metadata" on channel subscription**
- YouTube blocks datacenter IPs; import cookies via TA settings
- Or use `pipeline/youtube_downloader.py` which falls back to Apify/Bright Data
