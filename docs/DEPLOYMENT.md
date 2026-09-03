# Production deployment (Tier 1)

Minimum settings for a stable **Render** (or similar PaaS) demo.

## Start command (required)

Use **one Gunicorn worker** — job state lives in process memory.

```bash
gunicorn web_server:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120
```

This repo includes:

- [`Procfile`](Procfile) — same command for Heroku/Render
- [`render.yaml`](render.yaml) — Blueprint with `workers 1` and `SIA_DEBUG_ENABLED=false`

**Do not** use `--workers 4` until jobs are stored in Redis/Postgres.

## Environment variables (Render dashboard)

Set these in **Environment** — not only via Settings UI (container disk is ephemeral).

| Variable | Required | Notes |
|----------|----------|--------|
| `AZURE_OPENAI_API_KEY` | Yes (Azure) | API key |
| `AZURE_OPENAI_ENDPOINT` | Yes | e.g. `https://….openai.azure.com` |
| `AZURE_OPENAI_API_VERSION` | Yes | e.g. `2025-04-01-preview` |
| `AZURE_OPENAI_DEPLOYMENT` | Yes | e.g. `gpt-5.3-chat` |
| `SIA_LLM_MODEL` | Recommended | e.g. `azure-gpt-5.3-chat` |
| `SIA_DEBUG_ENABLED` | Recommended | `false` on Render |
| `SIA_JOB_RETENTION_WARN` | Optional | Default `15` — UI warning threshold |

Settings → Save writes keys to `.env` inside the container; **redeploy wipes that file**. Dashboard env vars survive redeploys.

## Debug mode

| Environment | Default |
|-------------|---------|
| Local dev | `debug_enabled` from `user_config.json` (default **off**) |
| Render (`RENDER` set) | **Off** unless `SIA_DEBUG_ENABLED=true` |

Debug on creates per-tool CSV snapshots and large `llm_traces` — fine locally, risky on small Render instances.

## Job hygiene

- Delete finished jobs from the Upload page (**Delete** button) to free RAM.
- When **15+** jobs exist (configurable), the Upload page shows a retention warning.
- Deleting a job also removes its prefixed snapshots under `runtime/snapshots/{job_id}_*.csv`.

## Build & run locally (production-like)

```bash
pip install -r requirements.txt
export SIA_DEBUG_ENABLED=false
gunicorn web_server:app --bind 0.0.0.0:5000 --workers 1 --timeout 120
```

## What Tier 1 does not fix

- ~~HITL state lost on restart~~ — **optional fix:** set `SCHEMA_AGENT_METADATA_DB=runtime/metadata.db` (or `SCHEMA_AGENT_PERSIST_JOBS=true`) so job metadata and pending Review checkpoints reload from SQLite after restart. Use a persistent disk on Render for `/var/data/metadata.db`.
- No multi-user auth
- Large Excel files still scale with sheet size in RAM

See architecture notes in [`docs/README.md`](README.md).
