# Deploy Crawl4AI on Railway — CLI Playbook

Complete copy-paste CLI commands to deploy Crawl4AI with Redis and Prometheus on Railway.
No placeholders — every value is derived from the actual codebase config.

## Prerequisites

```bash
brew install railway
railway login
```

---

## Step 1 — Create a Railway project

```bash
railway init --name crawl4ai
```

---

## Step 2 — Add a Redis database

```bash
railway add --database redis
```

Railway provisions a Redis instance and auto-generates these variables on the Redis service:
`REDIS_URL`, `REDIS_HOST`, `REDIS_PORT`, `REDIS_USER`, `REDIS_PASSWORD`.

---

## Step 3 — Add the Crawl4AI service (from Docker Hub image)

```bash
railway add --image unclecode/crawl4ai:latest
```

The service is automatically linked. Rename it if you like:

```bash
railway service link
```

(Select the image service when prompted.)

---

## Step 4 — Set environment variables on the Crawl4AI service

The app reads `config.yml` at startup, but `load_config()` in `utils.py` overrides
specific keys from environment variables. Here is the complete mapping:

| Env Var | What it overrides | Required |
|---|---|---|
| `REDIS_URL` | `config["redis"]["uri"]` → used by `aioredis.from_url()` in `server.py:207` | Yes (for external Redis) |
| `SECRET_KEY` | JWT signing key in `auth.py:14` (default: `"mysecret"`) | Yes (production) |
| `OPENAI_API_KEY` | Read by litellm at runtime for `openai/*` providers | If using OpenAI |
| `ANTHROPIC_API_KEY` | Read by litellm at runtime for `anthropic/*` providers | If using Anthropic |
| `LLM_PROVIDER` | Overrides `config["llm"]["provider"]` (default: `openai/gpt-4o-mini`) | No |
| `LLM_API_KEY` | Overrides `config["llm"]["api_key"]` if not already set in config | No |
| `LLM_TEMPERATURE` | Global temperature for all LLM calls | No |
| `LLM_BASE_URL` | Global custom API base URL for all providers | No |
| `CRAWL4AI_HOOKS_ENABLED` | Enables hooks execution (default: `"false"`) — RCE risk if true | No |

### Wire Redis to the app

Railway reference variables (`${{service.VAR}}`) let one service read another's variables.
The Redis service is named `Redis` by default.

```bash
railway variable set \
  'REDIS_URL=${{Redis.REDIS_URL}}' \
  --service crawl4ai
```

Railway resolves `${{Redis.REDIS_URL}}` at deploy time to the actual internal connection string
(e.g. `redis://default:abc123@redis.railway.internal:6379`).

**How it flows:**

```
${{Redis.REDIS_URL}}
    → Railway resolves to redis://default:pw@redis.railway.internal:6379
    → load_config() in utils.py reads os.environ["REDIS_URL"]
    → sets config["redis"]["uri"] = that URL
    → server.py line 207: aioredis.from_url(config["redis"].get("uri", ...))
    → connects to Railway's Redis service
```

### Set the remaining variables

```bash
railway variable set \
  SECRET_KEY=$(openssl rand -hex 32) \
  OPENAI_API_KEY=sk-proj-your-actual-key \
  --service crawl4ai
```

> Replace `sk-proj-your-actual-key` with your real OpenAI key.
> Add other provider keys as needed (ANTHROPIC_API_KEY, DEEPSEEK_API_KEY, etc).

---

## Step 5 — Generate a public domain

The app listens on port **11235** (Gunicorn bind in `supervisord.conf`).

```bash
railway domain --port 11235 --service crawl4ai
```

This creates a `*.up.railway.app` URL. Railway routes HTTPS traffic on port 443
to your container's port 11235.

---

## Step 6 — Deploy

If deploying from the Docker Hub image (Step 3), the service is already deployed.
If deploying from your local repo instead:

```bash
railway up --service crawl4ai --detach
```

---

## Step 7 — Verify

```bash
# Stream logs
railway logs --service crawl4ai

# Check health (replace with your actual domain)
curl https://crawl4ai-production.up.railway.app/health

# Check Prometheus metrics
curl https://crawl4ai-production.up.railway.app/metrics
```

Expected health response:

```json
{"status":"ok"}
```

Expected metrics: Prometheus text format with `http_request_duration_seconds`, etc.

---

## Step 8 — (Optional) Add Prometheus scraper service

```bash
railway add --image prom/prometheus:latest
```

Prometheus needs a config file. Create a minimal repo with a `prometheus.yml`:

```yaml
global:
  scrape_interval: 15s

scrape_configs:
  - job_name: crawl4ai
    metrics_path: /metrics
    static_configs:
      - targets: ['crawl4ai.railway.internal:11235']
```

> `crawl4ai.railway.internal` is the private DNS name Railway assigns.
> Use the actual service name if you renamed it — check with `railway status`.

Then set the config and expose the dashboard:

```bash
railway domain --port 9090 --service Prometheus
```

---

## Step 9 — (Optional) Add Grafana

```bash
railway add --image grafana/grafana:latest
railway domain --port 3000 --service Grafana
```

After deploying, open Grafana at its domain and add a Prometheus data source:

```
URL: http://Prometheus.railway.internal:9090
```

---

## What runs inside the container

Supervisord manages two processes (see `supervisord.conf`):

| Program | Command | Port | Notes |
|---|---|---|---|
| `redis` | `/usr/bin/redis-server --loglevel notice` | 6379 | Bundled Redis (still runs but unused when REDIS_URL points externally) |
| `gunicorn` | `gunicorn --bind 0.0.0.0:11235 --workers 1 --threads 4 --timeout 1800 ... server:app` | 11235 | FastAPI via Uvicorn worker |

The bundled Redis starts automatically but is harmless — the app connects to whichever
URL `config["redis"]["uri"]` resolves to. When `REDIS_URL` env var is set, the app
uses Railway's managed Redis instead.

---

## Config reference: what the app actually reads

### From `config.yml` (baked into the Docker image)

| Section | Key values | Override via env var? |
|---|---|---|
| `app.port` | `11235` | No (hardcoded in supervisord) |
| `llm.provider` | `openai/gpt-4o-mini` | Yes → `LLM_PROVIDER` |
| `redis.host/port` | `localhost:6379` | Yes → `REDIS_URL` overrides `redis.uri` |
| `rate_limiting.storage_uri` | `memory://` | No (change config.yml for Redis-backed rate limiting) |
| `security.jwt_enabled` | `false` | No (change config.yml) |
| `observability.prometheus.enabled` | `true` | No (always on by default) |
| `observability.prometheus.endpoint` | `/metrics` | No |
| `crawler.pool.max_pages` | `40` | No (change config.yml) |

### From environment variables only (not in config.yml)

| Env Var | Used in | Default |
|---|---|---|
| `SECRET_KEY` | `auth.py:14` | `"mysecret"` |
| `CRAWL4AI_HOOKS_ENABLED` | `server.py:84` | `"false"` |
| `OPENAI_API_KEY` | litellm runtime | (none) |
| `ANTHROPIC_API_KEY` | litellm runtime | (none) |
| `DEEPSEEK_API_KEY` | litellm runtime | (none) |
| `GROQ_API_KEY` | litellm runtime | (none) |
| `TOGETHER_API_KEY` | litellm runtime | (none) |
| `MISTRAL_API_KEY` | litellm runtime | (none) |
| `GEMINI_API_TOKEN` | litellm runtime | (none) |

---

## Full CLI sequence (copy-paste)

```bash
# 1. Install and authenticate
brew install railway
railway login

# 2. Create project
railway init --name crawl4ai

# 3. Add Redis database
railway add --database redis

# 4. Add Crawl4AI from Docker Hub
railway add --image unclecode/crawl4ai:latest

# 5. Link to the crawl4ai service
railway service link
# → Select the unclecode/crawl4ai service when prompted

# 6. Wire Redis URL (Railway resolves the reference at deploy time)
railway variable set 'REDIS_URL=${{Redis.REDIS_URL}}'

# 7. Set secrets
railway variable set SECRET_KEY=$(openssl rand -hex 32)
railway variable set OPENAI_API_KEY=sk-proj-your-actual-key

# 8. Expose public domain on port 11235
railway domain --port 11235

# 9. Stream logs to verify
railway logs
```

---

## Teardown

```bash
railway down          # Remove latest deployment
railway delete        # Delete entire project (interactive confirmation)
```
