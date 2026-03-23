# Tube Archivist — Vast.ai Deployment

Deploy a self-hosted [Tube Archivist](https://github.com/tubearchivist/tubearchivist) instance on a cheap Vast.ai VM for YouTube video archiving.

## Prerequisites

- `VAST_API_KEY` environment variable set
- `pip install vastai` (Vast.ai CLI)
- SSH key configured for Vast.ai (`~/.ssh/vast_v3`)

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

```
┌────────────────────────────────────┐
│         Vast.ai VM ($0.05-0.20/hr) │
│                                    │
│  ┌──────────────┐  ┌────────────┐ │
│  │ TubeArchivist│  │  Redis     │ │
│  │  :8000       │  │  :6379     │ │
│  └──────┬───────┘  └────────────┘ │
│         │                          │
│  ┌──────┴───────┐                  │
│  │ElasticSearch │                  │
│  │  :9200       │                  │
│  └──────────────┘                  │
│                                    │
│  /data/ta/media  ← downloaded vids │
│  /data/ta/cache  ← TA cache       │
│  /data/ta/es     ← ES data        │
│  /data/ta/redis  ← Redis data     │
└────────────────────────────────────┘
         │
         │ REST API (port 8000)
         ▼
┌────────────────────────────────────┐
│  pipeline/youtube_downloader.py    │
│  (TubeArchivistClient)             │
│  - List archived videos            │
│  - Stream video files              │
│  - Fetch metadata + subtitles      │
└────────────────────────────────────┘
```

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

## Cost Estimates

| Resource | Cost |
|----------|------|
| Cheap CPU VM (interruptible) | $0.05-0.15/hr |
| Cheap CPU VM (on-demand) | $0.10-0.20/hr |
| Running 24/7 | $1.20-4.80/day |
| Storage (100GB disk) | included in VM |

Tip: destroy the VM when not downloading, redeploy when needed. TA data persists on the VM disk while it exists.

## Troubleshooting

**"No suitable instances found"**
- Relax search criteria — try during off-peak hours or increase max $/hr

**TA health check times out**
- SSH in and check: `docker compose logs -f`
- ElasticSearch needs 1-2 min to start: `docker logs archivist-es`
- Verify vm.max_map_count: `sysctl vm.max_map_count` (must be 262144)

**Cannot get API token**
- TA needs to fully initialize first (can take 2-3 min after health check passes)
- Run: `python3 scripts/tube-archivist/deploy.py --get-token`

**Docker not starting inside Vast.ai container**
- The base image `nvidia/cuda:12.4.0-devel-ubuntu22.04` supports Docker-in-Docker
- Check: `ssh ... dockerd &` then `docker ps`

**SSH connection refused**
- Instance may still be booting — wait 1-2 min
- Check instance status: `vastai show instance ID`
