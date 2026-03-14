# Deploy Crawl4AI on Railway — CLI Playbook

Tested against Railway CLI **v4.12.0**, project **octopus-ai**, environment **production**.

Deploys from GitHub repo `Octopus-AI-Ltd/crawl4ai` so that the `REDIS_URL` override
in `utils.py load_config()` is included in the build.

---

## Step 1 — Connect your existing service to the GitHub repo

The CLI does not support attaching a repo to an existing service.
Do this once in the Railway dashboard:

1. Open your project at [railway.app](https://railway.app)
2. Click on your crawl4ai service
3. Go to **Settings** → **Source**
4. Click **Connect Repo** → select `Octopus-AI-Ltd/crawl4ai`
5. Railway auto-deploys from the repo's `Dockerfile`

After this, every push to the default branch triggers a new deploy automatically.

---

## Step 2 — Link your terminal to the service

```bash
railway service link
```

Select your crawl4ai service when prompted.

Confirm:

```bash
railway status
```

---

## Step 3 — Verify environment variables are set

You already set these. Confirm they're still there:

```bash
railway variables --kv
```

Expected (at minimum):

```
REDIS_URL=${{Redis.REDIS_URL}}
SECRET_KEY=...
OPENAI_API_KEY=sk-proj-...
```

If anything is missing, add it:

```bash
railway variables --set 'REDIS_URL=${{Redis.REDIS_URL}}'
railway variables --set SECRET_KEY=$(openssl rand -hex 32)
railway variables --set OPENAI_API_KEY=sk-proj-your-actual-key
```

**How `REDIS_URL` flows through the code:**

```
${{Redis.REDIS_URL}}
  → Railway resolves → redis://default:***@redis.railway.internal:6379
  → load_config() in utils.py reads os.environ["REDIS_URL"]
  → sets config["redis"]["uri"]
  → server.py:207 → aioredis.from_url(config["redis"]["uri"])
  → connects to your existing Railway Redis
```

---

## Step 4 — Expose public domain on port 11235

If not already done:

```bash
railway domain --port 11235
```

---

## Step 5 — Verify deployment

```bash
railway logs
```

Once logs show the app is up:

```bash
curl https://YOUR_GENERATED_DOMAIN.up.railway.app/health
curl https://YOUR_GENERATED_DOMAIN.up.railway.app/metrics
```

Open the playground:

```
https://YOUR_GENERATED_DOMAIN.up.railway.app/playground
```

---

## Redeploying after code changes

Push to GitHub and Railway auto-deploys. Or trigger manually:

```bash
railway redeploy
```

---

## Environment variable reference

### Overrides config.yml via `load_config()` in `utils.py`

| Env Var | What it overrides | Default if unset |
|---|---|---|
| `REDIS_URL` | `config["redis"]["uri"]` → `aioredis.from_url()` | `redis://localhost` (bundled) |
| `LLM_PROVIDER` | `config["llm"]["provider"]` | `openai/gpt-4o-mini` |
| `LLM_API_KEY` | `config["llm"]["api_key"]` | (none) |
| `LLM_TEMPERATURE` | Global temperature for all LLM calls | (provider default) |
| `LLM_BASE_URL` | Custom API base URL | (provider default) |

### Read directly from environment (not in config.yml)

| Env Var | Used in | Default if unset |
|---|---|---|
| `SECRET_KEY` | `auth.py` — JWT signing key | `"mysecret"` |
| `CRAWL4AI_HOOKS_ENABLED` | `server.py` — enables hook execution | `"false"` |
| `OPENAI_API_KEY` | litellm runtime | (none) |
| `ANTHROPIC_API_KEY` | litellm runtime | (none) |
| `DEEPSEEK_API_KEY` | litellm runtime | (none) |
| `GROQ_API_KEY` | litellm runtime | (none) |
| `TOGETHER_API_KEY` | litellm runtime | (none) |
| `MISTRAL_API_KEY` | litellm runtime | (none) |
| `GEMINI_API_TOKEN` | litellm runtime | (none) |

### Baked into config.yml (no env override — must edit config.yml to change)

| Setting | Value |
|---|---|
| `app.port` | `11235` |
| `rate_limiting.storage_uri` | `memory://` |
| `security.jwt_enabled` | `false` |
| `observability.prometheus.enabled` | `true` |
| `observability.prometheus.endpoint` | `/metrics` |
| `crawler.pool.max_pages` | `40` |

---

## What runs inside the container

Supervisord starts one process:

| Process | Bind | Notes |
|---|---|---|
| `gunicorn` | `:11235` | FastAPI app via Uvicorn worker, 1 worker, 4 threads, 1800s timeout |

Redis is external — provided by the Railway Redis service.

---

## Existing services in octopus-ai project

| Service | Status |
|---|---|
| octopus-crawler-srv/api | SUCCESS |
| octopus-api | SUCCESS |
| pgvector | SUCCESS |
| octopus-chat | SLEEPING |
| octopus-ai | SUCCESS |
| octopus-ingestion | SUCCESS |
| octopus-web | SLEEPING |
| Redis | SUCCESS |
| octopus-ws | SUCCESS |
| octopus-rag | SUCCESS |
