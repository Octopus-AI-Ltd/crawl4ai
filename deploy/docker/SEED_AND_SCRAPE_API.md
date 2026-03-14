# Seed & Scrape API Reference

Production base URL: `https://crawl4ai-production-fbed.up.railway.app`

---

## Overview

Two-step workflow: **seed** discovers URLs under a base path, **crawl** scrapes them.

```
POST /seed   → discover URLs from sitemap/Common Crawl
POST /crawl  → scrape the discovered URLs and return content
```

---

## Step 1: Seed — `POST /seed`

Discovers URLs scoped to a base URL path using sitemap and/or Common Crawl index.

### Request Body

```json
{
  "urls": ["https://roomspace.com/blog"],
  "source": "sitemap",
  "max_depth": 2,
  "max_urls": 50,
  "extract_head": false
}
```

### All Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `urls` | `string[]` | **required** | Base URL(s) to scope discovery. Only URLs under this path are returned. |
| `source` | `string` | `"sitemap+cc"` | `"sitemap"`, `"cc"` (Common Crawl), or `"sitemap+cc"` (both) |
| `max_depth` | `int` | `-1` | Max path segments beyond base URL. `-1` = unlimited. `1` = one level deep. |
| `max_urls` | `int` | `-1` | Max URLs to return per base URL. `-1` = unlimited. Applied after path filtering. |
| `extract_head` | `bool` | `false` | Fetch `<head>` metadata (title, meta, OG tags, JSON-LD). **Slow** — adds HTTP request per URL. |
| `live_check` | `bool` | `false` | HEAD-verify each URL is accessible. Sets `status` to `"valid"` or `"not_valid"`. |
| `pattern` | `string` | `"*"` | Additional glob filter (e.g. `"*.html"`, `"*/product/*"`) |
| `query` | `string` | `null` | BM25 search query for relevance scoring |
| `scoring_method` | `string` | `"bm25"` | Scoring method (only `"bm25"` supported) |
| `score_threshold` | `float` | `null` | Min relevance score (0.0–1.0) to include a URL |
| `concurrency` | `int` | `1000` | Parallel workers for discovery |
| `hits_per_sec` | `int` | `5` | Rate limit (requests/second) |
| `force` | `bool` | `false` | Bypass cache, re-fetch from source |
| `filter_nonsense_urls` | `bool` | `true` | Filter utility URLs (robots.txt, sitemap.xml, .js, .css, etc.) |
| `cache_ttl_hours` | `int` | `24` | Hours before sitemap cache expires |
| `validate_sitemap_lastmod` | `bool` | `true` | Re-fetch if sitemap's `<lastmod>` is newer than cache |
| `verbose` | `bool` | `null` | Show detailed progress in server logs |

### Response (adaptive — fields depend on request params)

**Minimal** (`extract_head=false`, no `query`):

```json
{
  "success": true,
  "results": {
    "https://roomspace.com/blog": [
      {"url": "https://roomspace.com/blog/winter-in-lisbon/", "status": "unknown"},
      {"url": "https://roomspace.com/blog/sports/", "status": "unknown"}
    ]
  },
  "total_urls": 2,
  "server_processing_time_s": 5.07,
  "server_memory_delta_mb": 0.37
}
```

**With `extract_head=true`** — adds `head_data`:

```json
{
  "url": "https://roomspace.com/blog/6-reasons-why-travel-agents-should-book-with-roomspace/",
  "status": "valid",
  "head_data": {
    "title": "6 Reasons Why Travel Agents Should Book With Roomspace",
    "charset": "utf-8",
    "meta": {
      "description": "Why should travel agents book with Roomspace?...",
      "og:title": "6 Reasons Why Travel Agents Should Book With Roomspace",
      "og:image": "https://roomspace.com/app/uploads/2021/01/home-why-roomspace-clean.jpg",
      "og:type": "article",
      "article:published_time": "2022-04-27T12:30:24+00:00",
      "author": "Jose",
      "twitter:card": "summary_large_image"
    },
    "link": {
      "canonical": [{"href": "https://roomspace.com/blog/6-reasons.../"}],
      "alternate": [{"href": "...", "hreflang": "en"}]
    }
  }
}
```

**With `query`** — adds `relevance_score`:

```json
{"url": "...", "status": "unknown", "relevance_score": 0.85}
```

### `status` field values

| Value | When |
|-------|------|
| `"unknown"` | `live_check=false` (default) |
| `"valid"` | `live_check=true` and URL responded to HEAD |
| `"not_valid"` | `live_check=true` and URL did not respond |

---

## Step 2: Crawl — `POST /crawl`

Scrapes URLs and returns full content (HTML, markdown, links, media, etc.).

### Config Serialization Pattern

All config objects use `{"type": "ClassName", "params": {...}}`. Enums use string values.
Dicts inside params use `{"type": "dict", "value": {...}}`. Primitives are passed directly.

### Request Body (markdown + cleaned content only)

```json
{
  "urls": [
    "https://roomspace.com/blog/winter-in-lisbon/",
    "https://roomspace.com/blog/sports/"
  ],
  "browser_config": {
    "type": "BrowserConfig",
    "params": {
      "headless": true
    }
  },
  "crawler_config": {
    "type": "CrawlerRunConfig",
    "params": {
      "word_count_threshold": 200,
      "only_text": true,
      "excluded_tags": ["nav", "footer", "header", "aside"],
      "cache_mode": "bypass"
    }
  }
}
```

### All `crawler_config.params`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `word_count_threshold` | `int` | `200` | Min word count to keep a page |
| `only_text` | `bool` | `false` | Strip all HTML, return text-only content |
| `css_selector` | `string` | `null` | CSS selector to extract specific elements (e.g. `"article"`, `".main-content"`) |
| `target_elements` | `string[]` | `[]` | HTML elements to target for extraction |
| `excluded_tags` | `string[]` | `[]` | HTML tags to exclude (e.g. `["nav", "footer", "aside"]`) |
| `excluded_selector` | `string` | `""` | CSS selector to exclude (e.g. `".sidebar, .ads, .cookie-banner"`) |
| `keep_data_attributes` | `bool` | `false` | Preserve `data-*` attributes in cleaned HTML |
| `cache_mode` | `string` | `"bypass"` | `"bypass"`, `"enabled"`, `"write_only"`, `"read_only"`, `"disabled"` |
| `screenshot` | `bool` | `false` | Capture full-page screenshot (base64 PNG) |
| `screenshot_wait_for` | `float` | `null` | Seconds to wait before screenshot capture |
| `pdf` | `bool` | `false` | Generate PDF of page |
| `stream` | `bool` | `false` | If `true`, use `/crawl/stream` for NDJSON streaming |
| `js_code` | `string[]` | `null` | JavaScript snippets to execute before extraction |
| `wait_for` | `string` | `null` | CSS selector to wait for before extraction |
| `simulate_user` | `bool` | `true` | Simulate real user behavior (mouse moves, etc.) |
| `magic` | `bool` | `false` | Auto-detect and handle anti-bot measures |
| `scan_full_page` | `bool` | `false` | Scroll through entire page before extraction |
| `process_iframes` | `bool` | `false` | Extract content from iframes |
| `remove_overlay_elements` | `bool` | `false` | Remove popups/overlays before extraction |
| `delay_before_return_html` | `float` | `null` | Seconds to wait before capturing HTML |

### All `browser_config.params`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `headless` | `bool` | `true` | Run browser in headless mode |
| `verbose` | `bool` | `false` | Enable verbose logging |
| `viewport` | `object` | `null` | `{"type": "dict", "value": {"width": 1920, "height": 1080}}` |
| `user_agent` | `string` | `null` | Custom user agent string |
| `proxy` | `string` | `null` | Proxy URL (e.g. `"http://user:pass@proxy:8080"`) |
| `text_mode` | `bool` | `false` | Disable images/CSS for faster loading |

### Extraction Strategies (advanced)

For structured extraction, pass as nested `type`/`params`:

```json
{
  "crawler_config": {
    "type": "CrawlerRunConfig",
    "params": {
      "extraction_strategy": {
        "type": "JsonCssExtractionStrategy",
        "params": {
          "schema": {
            "type": "dict",
            "value": {
              "baseSelector": "article.post",
              "fields": [
                {"name": "title", "selector": "h1", "type": "text"},
                {"name": "content", "selector": ".content", "type": "html"}
              ]
            }
          }
        }
      }
    }
  }
}
```

### Response

```json
{
  "success": true,
  "results": [
    {
      "url": "https://roomspace.com/blog/winter-in-lisbon/",
      "success": true,
      "html": "<html>...</html>",
      "cleaned_html": "<div>...</div>",
      "markdown": {
        "raw_markdown": "# Winter in Lisbon\n\n...",
        "fit_markdown": "# Winter in Lisbon (pruned)...",
        "markdown_with_citations": "...",
        "references_markdown": "..."
      },
      "links": {"internal": [...], "external": [...]},
      "media": {"images": [...], "videos": [...]},
      "metadata": {"title": "...", "description": "..."},
      "extracted_content": null,
      "error_message": null
    }
  ],
  "server_processing_time_s": 3.2,
  "server_memory_delta_mb": 12.5,
  "server_peak_memory_mb": 256.3
}
```

### Key response fields

| Field | Description |
|-------|-------------|
| `results[].markdown.raw_markdown` | Full markdown of the page |
| `results[].markdown.fit_markdown` | Pruned/condensed markdown (best for LLM context) |
| `results[].markdown.markdown_with_citations` | Markdown with source citations |
| `results[].cleaned_html` | Sanitized HTML |
| `results[].links` | Internal + external links found |
| `results[].media` | Images, videos, audio found |
| `results[].metadata` | Page title, description, etc. |
| `results[].extracted_content` | Structured data (if extraction_strategy used) |

---

## Full Seed + Scrape Example (Python)

```python
import httpx

BASE = "https://crawl4ai-production-fbed.up.railway.app"

# 1. Seed: discover blog URLs
seed_resp = httpx.post(f"{BASE}/seed", json={
    "urls": ["https://roomspace.com/blog"],
    "source": "sitemap",
    "max_depth": 1,
    "max_urls": 10,
}, timeout=120).json()

discovered_urls = [
    item["url"]
    for item in seed_resp["results"]["https://roomspace.com/blog"]
]
print(f"Discovered {len(discovered_urls)} URLs")

# 2. Crawl: scrape the discovered URLs (markdown + clean text only)
crawl_resp = httpx.post(f"{BASE}/crawl", json={
    "urls": discovered_urls,
    "browser_config": {
        "type": "BrowserConfig",
        "params": {"headless": True, "text_mode": True}
    },
    "crawler_config": {
        "type": "CrawlerRunConfig",
        "params": {
            "word_count_threshold": 200,
            "only_text": True,
            "excluded_tags": ["nav", "footer", "header", "aside"],
            "cache_mode": "bypass",
        }
    }
}, timeout=300).json()

for result in crawl_resp["results"]:
    if result["success"]:
        md = result["markdown"]["raw_markdown"]
        fit = result["markdown"]["fit_markdown"]  # pruned version for LLM
        print(f"\n--- {result['url']} ---")
        print(md[:300])
```

---

## Full Seed + Scrape Example (cURL)

```bash
BASE="https://crawl4ai-production-fbed.up.railway.app"

# 1. Seed
curl -s -X POST "$BASE/seed" \
  -H "Content-Type: application/json" \
  -d '{
    "urls": ["https://roomspace.com/blog"],
    "source": "sitemap",
    "max_depth": 1,
    "max_urls": 10
  }'

# 2. Crawl (paste discovered URLs from step 1)
curl -s -X POST "$BASE/crawl" \
  -H "Content-Type: application/json" \
  -d '{
    "urls": [
      "https://roomspace.com/blog/winter-in-lisbon/",
      "https://roomspace.com/blog/sports/"
    ],
    "browser_config": {
      "type": "BrowserConfig",
      "params": {"headless": true, "text_mode": true}
    },
    "crawler_config": {
      "type": "CrawlerRunConfig",
      "params": {
        "word_count_threshold": 200,
        "only_text": true,
        "excluded_tags": ["nav", "footer", "header", "aside"],
        "cache_mode": "bypass"
      }
    }
  }'
```

---

## Background Jobs (for long-running operations)

For `extract_head=true` or large domains, use the job endpoints:

```bash
# Submit seed job
curl -X POST "$BASE/seed/job" \
  -H "Content-Type: application/json" \
  -d '{
    "urls": ["https://roomspace.com/"],
    "source": "sitemap+cc",
    "extract_head": true,
    "max_urls": 100
  }'
# Returns: {"task_id": "seed_a1b2c3d4"}

# Poll for result
curl "$BASE/seed/job/seed_a1b2c3d4"
# Returns: {"status": "processing", ...} or {"status": "completed", "result": {...}}

# Similarly for crawl jobs:
curl -X POST "$BASE/crawl/job" \
  -H "Content-Type: application/json" \
  -d '{"urls": ["https://roomspace.com/blog/winter-in-lisbon/"]}'
```

---

## Depth Scoping Examples

Given base URL `https://example.com/blog`:

| `max_depth` | Includes | Excludes |
|-------------|----------|----------|
| `0` | `/blog` (only the page itself) | `/blog/post-1` |
| `1` | `/blog/post-1`, `/blog/news` | `/blog/category/post-1` |
| `2` | `/blog/category/post-1` | `/blog/a/b/c` |
| `-1` | Everything under `/blog` | Nothing |

---

## Performance Notes

| Operation | Speed | Notes |
|-----------|-------|-------|
| Seed (sitemap, no extras) | ~5–10s | Fastest. Uses cached sitemap. |
| Seed + `live_check` | ~60–70s | HEAD request per URL across whole domain |
| Seed + `extract_head` | ~2–5min | Fetches `<head>` of every URL. Use `/seed/job` for this. |
| Seed + `force=true` | +5–10s | Bypasses cache, re-fetches sitemap/CC |
| Crawl (per URL) | ~2–5s | Full page render + markdown extraction |
