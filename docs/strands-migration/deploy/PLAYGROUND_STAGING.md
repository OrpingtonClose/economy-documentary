# Playground — staging deployment

This doc covers the **Component Playground** staging environment only.
It is a sealed workbench for the 15 atomic components (C01–C15), not
the documentary pipeline itself. No GPU. No TTS pool. No render
outputs.

## What gets deployed

Single Vast.ai CPU VM (ubuntu:22.04). Three processes, one public port:

```
                           +-------------------------+
  user browser  --->  :80 | nginx (reverse proxy)   |
                           +-------------------------+
                             │  everything  → :3100  (Next.js)
                             │  /playground/* → :8000 (FastAPI, for curl)
                             ▼
                 +--------------------------+      +-----------------------+
                 | frontend-playground      |  →   | server.server:app     |
                 | Next.js 14, port 3100    |      | FastAPI, port 8000    |
                 | (rewrites /playground/*  |      | includes              |
                 |  to backend via          |      |  /playground router   |
                 |  PLAYGROUND_API_URL)     |      |                       |
                 +--------------------------+      +-----------------------+
```

The Next.js app's own `next.config.js` already proxies `/playground/*`
to whatever `PLAYGROUND_API_URL` points at. We set
`PLAYGROUND_API_URL=http://127.0.0.1:8000` on the VM so the browser
never talks to the backend directly — it talks to nginx → Next.js →
localhost backend. Single origin, no CORS.

Supervisor owns both processes. Nginx is configured via
`/etc/nginx/sites-available/playground`. Logs:

- `/var/log/playground-backend.{out,err}.log`
- `/var/log/playground-frontend.{out,err}.log`
- `journalctl -u nginx`

## What is *not* deployed

- GPU workers (TTS, video). The playground is not a pipeline run — it
  runs each component in isolation, and components that depend on
  downstream workers either call them directly or fail closed.
- Backblaze B2 artifact uploads (optional; enabled only if
  `B2_KEY_ID` + `B2_APPLICATION_KEY` are forwarded).
- The production `frontend/` dashboard. Lives on port 3000 in the
  central-unit deployment. The playground uses `frontend-playground/`
  on port 3100, a separate app that never boots the full pipeline.

## Provisioning

```bash
# locally, with your VAST_API_KEY + LLM keys exported
export VAST_API_KEY=...
export GOOGLE_API_KEY=...
export OPENAI_API_KEY=...
# any other declared-model keys you want to smoke (KIMI, ANTHROPIC, …)

cd economy-documentary
python scripts/provision_playground_staging.py \
    --max-price 0.15 --disk 25 --branch main
```

The script:

1. Searches Vast.ai for the cheapest on-demand CPU offer that meets
   minimum CPU/RAM/disk (≥4 cores, ≥8 GB RAM, ≥25 GB disk).
2. Creates an `ubuntu:22.04` instance with the repo ports exposed
   (`80`, `3100`, `8000`) and your exported LLM keys forwarded via
   `--env`.
3. On first boot, the VM runs `scripts/playground_staging_bootstrap.sh`
   via `--onstart-cmd`, which:
   - installs Node 20, Python 3.12, supervisor, nginx,
   - clones the repo at `--branch`,
   - `pip install`s the runtime subset of `server/pyproject.toml`,
   - runs `npm ci && npm run build` in `frontend-playground/`,
   - writes `/etc/supervisor/conf.d/playground.conf` +
     `/etc/nginx/sites-available/playground`,
   - starts both processes and reloads nginx.
4. Prints the public URL and stores connection info in
   `~/.playground-staging-info.json`.

Bootstrap is idempotent — re-running it picks up a new commit on the
branch without rebuilding node_modules.

## Access

After the VM prints `running`, wait ~3–5 minutes for the bootstrap,
then open:

- UI:      `http://<public-ip>/components`
- Backend: `http://<public-ip>/playground/components` (JSON catalog)
- Health:  `http://<public-ip>/health`

## Redeploy from a different branch

```bash
# branch-at-provision
python scripts/provision_playground_staging.py --branch my/feature
# or, on an already-provisioned VM:
ssh -p <port> root@<host>
git -C /workspace/economy-documentary fetch --depth=1 origin my/feature
git -C /workspace/economy-documentary reset --hard origin/my/feature
bash /workspace/economy-documentary/scripts/playground_staging_bootstrap.sh
```

## Smoke test

```bash
IP=<public-ip>

# Catalog of all 15 components.
curl -fsS "http://${IP}/playground/components" | jq '.components | length'
# → 15

# Reachability for c01 (scenario writer).
curl -fsS "http://${IP}/playground/components/c01/reachability" | jq .

# Deterministic run (c04 style lock does no LLM call).
curl -fsS "http://${IP}/playground/components/c04/run" \
    -H 'content-type: application/json' \
    -d '{"case_name":"pass","input":{"scenes":[]}}' | jq .status
# → "OK"
```

## Tearing it down

```bash
vastai --api-key $VAST_API_KEY destroy instance <instance_id>
```

The VM is stateless besides the user-case sidecars under
`/workspace/playground-user-cases/`. If you want those to survive
teardown, rsync them off before destroying:

```bash
rsync -avz \
    -e "ssh -p <port>" \
    "root@<host>:/workspace/playground-user-cases/" \
    ./user-cases-backup/
```
