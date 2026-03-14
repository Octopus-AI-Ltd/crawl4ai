from typing import List, Optional, Dict
from enum import Enum
from pydantic import BaseModel, Field, HttpUrl
from utils import FilterType


class CrawlRequest(BaseModel):
    urls: List[str] = Field(min_length=1, max_length=100)
    browser_config: Optional[Dict] = Field(default_factory=dict)
    crawler_config: Optional[Dict] = Field(default_factory=dict)


class HookConfig(BaseModel):
    """Configuration for user-provided hooks"""
    code: Dict[str, str] = Field(
        default_factory=dict,
        description="Map of hook points to Python code strings"
    )
    timeout: int = Field(
        default=30,
        ge=1,
        le=120,
        description="Timeout in seconds for each hook execution"
    )
    
    class Config:
        schema_extra = {
            "example": {
                "code": {
                    "on_page_context_created": """
async def hook(page, context, **kwargs):
    # Block images to speed up crawling
    await context.route("**/*.{png,jpg,jpeg,gif}", lambda route: route.abort())
    return page
""",
                    "before_retrieve_html": """
async def hook(page, context, **kwargs):
    # Scroll to load lazy content
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_timeout(2000)
    return page
"""
                },
                "timeout": 30
            }
        }


class CrawlRequestWithHooks(CrawlRequest):
    """Extended crawl request with hooks support"""
    hooks: Optional[HookConfig] = Field(
        default=None,
        description="Optional user-provided hook functions"
    )

class MarkdownRequest(BaseModel):
    """Request body for the /md endpoint."""
    url: str                    = Field(...,  description="Absolute http/https URL to fetch")
    f:   FilterType             = Field(FilterType.FIT, description="Content‑filter strategy: fit, raw, bm25, or llm")
    q:   Optional[str] = Field(None,  description="Query string used by BM25/LLM filters")
    c:   Optional[str] = Field("0",   description="Cache‑bust / revision counter")
    provider: Optional[str] = Field(None, description="LLM provider override (e.g., 'anthropic/claude-3-opus')")
    temperature: Optional[float] = Field(None, description="LLM temperature override (0.0-2.0)")
    base_url: Optional[str] = Field(None, description="LLM API base URL override")


class RawCode(BaseModel):
    code: str

class HTMLRequest(BaseModel):
    url: str
    
class ScreenshotRequest(BaseModel):
    url: str
    screenshot_wait_for: Optional[float] = 2
    output_path: Optional[str] = None

class PDFRequest(BaseModel):
    url: str
    output_path: Optional[str] = None


class JSEndpointRequest(BaseModel):
    url: str
    scripts: List[str] = Field(
        ...,
        description="List of separated JavaScript snippets to execute"
    )


class SeedRequest(BaseModel):
    """Request body for the /seed endpoint — URL discovery via sitemap/Common Crawl."""
    urls: List[str] = Field(
        min_length=1, max_length=50,
        description="Base URL(s) to scope discovery (e.g. 'https://example.com/blogs')")
    source: str = Field(
        "sitemap+cc",
        description="URL source: 'sitemap', 'cc', or 'sitemap+cc'")
    pattern: Optional[str] = Field(
        "*",
        description="Additional glob URL filter (e.g. '*.html')")
    max_depth: int = Field(
        -1, ge=-1,
        description="Max path segments beyond base URL (-1 = unlimited)")
    extract_head: bool = Field(
        False,
        description="Extract <head> metadata (title, meta, og tags, jsonld)")
    live_check: bool = Field(
        False,
        description="HEAD-verify each URL is accessible")
    max_urls: int = Field(
        -1,
        description="Max URLs to return per base URL (-1 = unlimited)")
    concurrency: int = Field(
        1000, ge=1,
        description="Parallel workers")
    hits_per_sec: int = Field(
        5, ge=1,
        description="Rate limit (requests/sec)")
    force: bool = Field(
        False,
        description="Bypass cache, fetch fresh data")
    query: Optional[str] = Field(
        None,
        description="BM25 search query for relevance scoring")
    scoring_method: Optional[str] = Field(
        "bm25",
        description="Scoring method (currently 'bm25')")
    score_threshold: Optional[float] = Field(
        None, ge=0.0, le=1.0,
        description="Min relevance score to include URL")
    filter_nonsense_urls: bool = Field(
        True,
        description="Filter utility URLs (robots.txt, etc.)")
    cache_ttl_hours: int = Field(
        24, ge=0,
        description="Hours before sitemap cache expires")
    validate_sitemap_lastmod: bool = Field(
        True,
        description="Refetch if sitemap lastmod is newer")
    verbose: Optional[bool] = Field(
        None,
        description="Show detailed progress")


class WebhookConfig(BaseModel):
    """Configuration for webhook notifications."""
    webhook_url: HttpUrl
    webhook_data_in_payload: bool = False
    webhook_headers: Optional[Dict[str, str]] = None


class WebhookPayload(BaseModel):
    """Payload sent to webhook endpoints."""
    task_id: str
    task_type: str  # "crawl", "llm_extraction", etc.
    status: str  # "completed" or "failed"
    timestamp: str  # ISO 8601 format
    urls: List[str]
    error: Optional[str] = None
    data: Optional[Dict] = None  # Included only if webhook_data_in_payload=True