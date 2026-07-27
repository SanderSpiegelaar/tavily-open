# Tavily-like Framework

This project now has a low-cost Tavily-like pipeline that avoids depending on paid
external search APIs as the primary path.

## Component Placement

The benchmark results showed that the Reader stage has the strongest extraction
quality, while browser rendering is expensive and should be a late fallback.

Default flow:

```text
query
  -> SearchRouter
      -> local SQLite FTS index
      -> SearXNG
      -> Brave Search, only when EXTERNAL_SEARCH_ENABLED=true
  -> local-index content reuse for local hits
  -> Reader-first extraction for missing URLs
  -> HTTP extractor fallback
  -> Obscura / remote browser / local Playwright fallback
  -> quality score, chunks, optional extractive answer
  -> failed URLs are queued for async backfill with retry backoff
  -> Redis cache + SQLite local index writeback
```

## Endpoints

- `POST /search`: existing endpoint, legacy response by default.
- `POST /search` with `"response_format": "tavily"`: Tavily-like response.
- `POST /tavily/search`: Tavily-like search response by default.
- `POST /extract`: extract known URLs and write successful content into the local index.
- `POST /tavily/extract`: alias for `/extract`.

Example:

```bash
curl -X POST http://localhost:8000/tavily/search \
  -H "Content-Type: application/json" \
  -d '{
    "query": "reader benchmark extraction",
    "max_results": 5,
    "include_answer": true,
    "include_raw_content": false,
    "chunks_per_source": 2
  }'
```

## Cost Controls

External search is disabled by default:

```env
SEARCH_PROVIDER=router
SEARCH_ROUTE_PROVIDERS=local,searxng,brave
EXTERNAL_SEARCH_ENABLED=false
LOCAL_INDEX_ENABLED=true
```

Turn on Brave only when you want paid fallback:

```env
EXTERNAL_SEARCH_ENABLED=true
BRAVE_SEARCH_API_KEY=...
```

## Async Backfill

Foreground search should not keep retrying pages that are blocked or flaky. Failed URLs
are persisted to the local SQLite queue and retried in the background. Successful
backfill writes content into the local index, so the next search can reuse it.

```env
BACKFILL_ENABLED=true
BACKFILL_BATCH_SIZE=3
BACKFILL_WORKER_INTERVAL_SECONDS=30
BACKFILL_MAX_ATTEMPTS=5
BACKFILL_BASE_DELAY_SECONDS=300
BACKFILL_MAX_DELAY_SECONDS=86400
```

Observability and control:

- `GET /backfill/stats`
- `GET /backfill/jobs`
- `POST /backfill/enqueue`
- `POST /backfill/run-once`
- `GET /registry/services/{service_name}`

Jobs stop at `abandoned` after `BACKFILL_MAX_ATTEMPTS`, so anti-crawl pages do not
loop forever.

## Distributed Deployment

For multiple crawler/API nodes, use Redis as the shared backfill queue:

```env
BACKFILL_QUEUE_BACKEND=redis
BACKFILL_REDIS_KEY_PREFIX=trailsearch:backfill
BACKFILL_CLAIM_TTL_SECONDS=900
```

Each node atomically claims due jobs from Redis. A URL that is already `queued`,
`running`, or retry-delayed is skipped by foreground requests, so multiple nodes do
not keep connecting to the same anti-crawl page.

Use etcd for service registration/discovery:

```env
ETCD_ENABLED=true
ETCD_ENDPOINTS=http://etcd:2379
ETCD_DISCOVER_READERS=true
ETCD_REGISTER_SELF=true
ETCD_SELF_SERVICES=api,crawler
ETCD_NODE_ID=app-1
ETCD_NODE_ENDPOINT=http://app-1:3000
```

Reader discovery is etcd-first and static-config fallback. This means a crawler node
will use registered `reader` instances when etcd has them, and fall back to
`READER_URLS` if etcd is unavailable or empty.

For multiple Reader nodes, configure:

```env
READER_URLS=http://reader-1:3000,http://reader-2:3000,http://reader-3:3000
ETCD_REGISTER_READER_URLS=true
```

Requests are spread by stable target-URL hashing. If one Reader endpoint fails, the
client tries the next endpoint in the ordered list. If your Reader image can
self-register, prefer that. If it cannot, `ETCD_REGISTER_READER_URLS=true` registers
the configured Reader endpoints as a practical compose/Kubernetes bridge.

Compose example:

```bash
docker compose -f docker-compose.distributed.yml up -d
```

If Docker Hub mirrors are unreliable on the host, override image sources:

```env
PYTHON_IMAGE=mcr.microsoft.com/devcontainers/python:1-3.11-bookworm
REDIS_IMAGE=public.ecr.aws/docker/library/redis:7-alpine
SEARXNG_IMAGE=ghcr.io/searxng/searxng:latest
READER_IMAGE=ghcr.io/intergalacticalvariable/reader:latest
```

Notes:

- Redis cache is shared across crawler nodes.
- etcd stores ephemeral node presence only; it is not the crawl job queue.
- The SQLite local index is per node in the compose example. Successful backfill also
  writes through the crawler cache, so other nodes can avoid re-fetching once the
  same query/URL pair is cached.
- For a larger production cluster, replace local SQLite search with a shared index
  such as Postgres FTS, Meilisearch, Typesense, or Elasticsearch.

## Extraction Strategy

Default:

```env
CRAWL_EXTRACTION_STRATEGY=reader_first
CRAWL_QUALITY_GATE_ENABLED=true
```

Available strategies:

- `reader_first`: benchmark-informed default. Use Reader before cheaper local HTTP extraction.
- `http_first`: use local HTTP extraction first to reduce Reader usage.
- `quality_gated`: try HTTP first, but escalate low-quality content to Reader.

## Docker

Default local stack:

```bash
docker compose up -d --build
```

This starts Redis, SearXNG, and the API app. Reader and Browserless are optional
because they pull larger external images.

Reader-enabled local stack:

```bash
docker compose --profile reader up -d --build
```

This keeps the default API on `http://localhost:8000` and exposes the
reader-enabled API on `http://localhost:8001`.

Full local stack:

```bash
docker compose --profile full up -d --build
```

The local SQLite index is persisted in:

```text
./data/trailsearch.sqlite3
```
