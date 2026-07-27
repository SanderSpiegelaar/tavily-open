# TrailSearch

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)

中文文档 | [English](README.md)

TrailSearch 是一个自托管的网页搜索、抓取与内容提取 API。它把低成本搜索路由、本地 SQLite FTS 复用、SearXNG、可选 Brave Search 兜底、Redis 缓存、Reader/HTTP/浏览器分阶段提取和异步 backfill 串在一起，让前台请求尽量快返回，难抓页面交给后台重试。

包名和命令行入口是 `trailsearch`；API 同时保留传统 `/search` 响应，并提供 Tavily-like 的 `/tavily/search` 和 `/tavily/extract`。

## 核心特性

- **低成本搜索路由**：优先查本地 SQLite 索引，再查 SearXNG；只有显式开启外部搜索时才调用 Brave Search。
- **Tavily-like API**：支持 `POST /tavily/search`、`POST /tavily/extract`、可选 answer、chunks、raw content、域名过滤和 search-only 模式。
- **基于 benchmark 的提取顺序**：支持 `reader_first`、`http_first`、`quality_gated` 三种提取策略。
- **本地内容复用**：成功提取的内容会写回 SQLite FTS 和 Redis，后续查询可直接复用。
- **异步 backfill**：前台爬取失败的 URL 会进入队列并按退避策略重试，不阻塞当前搜索。
- **分布式部署能力**：Redis 可作为共享 backfill 队列，etcd 可用于 API、crawler、Reader 节点注册与发现。
- **浏览器兜底**：支持 Obscura CLI、Browserless/CDP 和本地 Playwright，作为最后阶段处理必须渲染的页面。

## 系统架构

```text
client
  -> FastAPI app
      -> SearchRouter
          -> local SQLite FTS index
          -> SearXNG
          -> Brave Search, only when EXTERNAL_SEARCH_ENABLED=true
      -> local-index content reuse for known URLs
      -> Redis crawl/search cache
      -> extraction pipeline
          -> Reader, HTTP extractor, Obscura, Browserless/CDP, local Playwright
          -> order controlled by CRAWL_EXTRACTION_STRATEGY
      -> quality score, chunks, optional extractive answer
      -> Redis cache + SQLite local index writeback
      -> failed URLs queued for async backfill
```

核心模块：

| 模块 | 作用 |
|---|---|
| `src/trailsearch/main.py` | FastAPI 应用、请求模型、路由、Tavily-like 响应组装、生命周期初始化 |
| `src/trailsearch/search_providers.py` | Local、SearXNG、Brave 和 SearchRouter 搜索提供者 |
| `src/trailsearch/crawler.py` | 分阶段提取编排和缓存感知的爬取结果 |
| `src/trailsearch/extractor.py` | 轻量 HTTP/trafilatura 提取路径 |
| `src/trailsearch/reader.py` | Reader 服务客户端、多端点哈希分发和失败切换 |
| `src/trailsearch/browser.py` | Obscura、远程 Browserless/CDP、本地 Playwright 兜底 |
| `src/trailsearch/local_index.py` | SQLite 文档索引、FTS 搜索和本地 backfill 任务存储 |
| `src/trailsearch/backfill.py` | 后台重试失败爬取任务的 worker |
| `src/trailsearch/backfill_queue.py` | Redis 分布式 backfill 队列 |
| `src/trailsearch/service_registry.py` | etcd 服务注册与发现 |
| `src/trailsearch/cache.py` | Redis crawl/search 缓存 |
| `src/trailsearch/quality.py` | 内容质量评分、分词和 chunk |

更详细的架构说明见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 快速开始

### Docker

完整本地栈统一使用 Demo Compose：

```bash
cp .env.demo.example .env.demo
docker compose --env-file .env.demo -f docker-compose.demo.yml up -d --build
```

该命令会启动 API、Redis、SearXNG、Reader 和 Browserless，只向主机暴露 API。

| 服务 | 地址 |
|---|---|
| 主 API | `http://127.0.0.1:8000` |
| Swagger UI | `http://127.0.0.1:8000/docs` |
| ReDoc | `http://127.0.0.1:8000/redoc` |

配置 `CLOUDFLARE_TUNNEL_TOKEN` 后可启用 Cloudflare Tunnel：

```bash
docker compose --env-file .env.demo -f docker-compose.demo.yml --profile tunnel up -d --build
```

根目录 `docker-compose.yml` 仍保留给轻量开发 profile 使用，不要与 `docker-compose.demo.yml` 同时组合启动。

### 分布式 Compose

```bash
docker compose -f docker-compose.distributed.yml up -d --build
```

分布式栈会启动 Redis、etcd、SearXNG、两个 Reader 节点、一个公开 API 节点和两个 crawler worker。Redis 作为共享 backfill 队列，etcd 负责注册和发现 API/crawler/Reader 实例。

### 手动安装

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env
trailsearch
```

macOS/Linux 使用 `source .venv/bin/activate` 激活虚拟环境。命令行本地启动默认监听 `http://0.0.0.0:3000`。

如果手动本地运行时没有启动 Reader 服务，请先设置 `READER_ENABLED=false`，或者把 `READER_URL` / `READER_URLS` 指向可访问的 Reader 实例。

如果启用了本地浏览器兜底，先安装 Chromium：

```bash
python -m playwright install chromium
```

## API

### 传统搜索

```http
POST /search
Content-Type: application/json
```

```json
{
  "query": "crawler benchmark extraction",
  "limit": 5,
  "mode": "crawl",
  "provider": "router",
  "response_format": "legacy",
  "search_depth": "basic",
  "include_answer": false,
  "include_raw_content": false,
  "chunks_per_source": 0,
  "include_domains": [],
  "exclude_domains": [],
  "days": 7,
  "topic": "general"
}
```

**新增参数：**
- `days` (可选): 按时间范围（天数）过滤搜索结果。映射到 SearXNG 的 time_range：≤1→"day", ≤7→"week", ≤30→"month", >30→"year"
- `topic` (可选): 按主题分类搜索，路由到适当的搜索引擎。选项：`general`、`news`、`academic`、`code`、`images`、`videos`、`social`、`shopping`

设置 `"mode": "search"` 可以只返回搜索结果、不爬取页面。设置 `"response_format": "tavily"` 可以让 `/search` 直接返回 Tavily-like 结构。

### Tavily-like 搜索

```http
POST /tavily/search
Content-Type: application/json
```

```json
{
  "query": "reader benchmark extraction",
  "max_results": 5,
  "search_depth": "basic",
  "include_answer": true,
  "include_raw_content": false,
  "chunks_per_source": 2,
  "include_domains": [],
  "exclude_domains": [],
  "provider": "router",
  "days": 7,
  "topic": "general"
}
```

主要响应字段：

| 字段 | 含义 |
|---|---|
| `query` | 原始查询 |
| `answer` | 可选的非 LLM 抽取式答案，由高分来源句子拼接 |
| `results` | 排序后的提取结果，包含 `title`、`url`、`content`、`score`、`source_stage` |
| `chunks` | 当 `chunks_per_source > 0` 时返回每个结果的文本片段 |
| `raw_content` | 当 `include_raw_content=true` 时返回完整清洗正文 |
| `failed_results` | 前台提取失败的 URL |
| `backfill` | 失败 URL 进入异步队列后的状态 |
| `timings_ms` | 搜索、缓存、提取、文本处理和写回耗时 |

### 提取指定 URL

```http
POST /extract
POST /tavily/extract
```

```json
{
  "urls": ["https://example.com/article"],
  "query": "optional relevance query",
  "include_raw_content": true,
  "chunks_per_source": 2
}
```

成功提取的内容会写入本地 SQLite 索引，供后续搜索复用。

### 运维接口

| Endpoint | 用途 |
|---|---|
| `GET /cache/stats` | 查看 Redis 缓存状态 |
| `POST /cache/clear` | 清空 crawl/search 缓存 |
| `GET /backfill/stats` | 查看 backfill 队列状态 |
| `GET /backfill/jobs?limit=50` | 查看最近 backfill 任务 |
| `POST /backfill/enqueue` | 手动加入待回填 URL |
| `POST /backfill/run-once` | 立即处理一批到期 backfill 任务 |
| `GET /registry/services/{service_name}` | 查看 etcd 中发现的服务实例 |

## 配置

大部分配置都在 [.env.example](.env.example)。常用分组：

| 范围 | 变量 |
|---|---|
| API | `API_HOST`, `API_PORT`, `APP_PORT`, `APP_READER_PORT` |
| SearXNG | `SEARXNG_URL`, `SEARXNG_HOST`, `SEARXNG_PORT`, `SEARXNG_BASE_PATH`, `SEARCH_LANGUAGE` |
| 搜索路由 | `SEARCH_PROVIDER`, `SEARCH_ROUTE_PROVIDERS`, `EXTERNAL_SEARCH_ENABLED`, `EXTERNAL_SEARCH_FALLBACK_ONLY`, `BRAVE_SEARCH_API_KEY` |
| 本地索引 | `LOCAL_INDEX_ENABLED`, `LOCAL_INDEX_PATH`, `LOCAL_INDEX_MIN_RESULTS` |
| Redis 缓存 | `CACHE_ENABLED`, `REDIS_URL`, `CACHE_TTL_HOURS`, `SEARCH_CACHE_TTL_SECONDS` |
| 提取策略 | `CRAWL_EXTRACTION_STRATEGY`, `CRAWL_QUALITY_GATE_ENABLED`, `CRAWL_MIN_QUALITY_SCORE` |
| HTTP 阶段 | `HTTP_EXTRACTOR_ENABLED`, `HTTP_EXTRACTOR_TIMEOUT_SECONDS`, `HTTP_EXTRACTOR_MAX_CONCURRENCY`, `HTTP_EXTRACTOR_MIN_CONTENT_LENGTH` |
| Reader 阶段 | `READER_ENABLED`, `READER_URL`, `READER_URLS`, `READER_TIMEOUT_SECONDS`, `READER_MAX_CONCURRENCY`, `READER_MIN_CONTENT_LENGTH` |
| 浏览器兜底 | `BROWSER_BACKEND`, `BROWSERLESS_WS_URL`, `BROWSER_LOCAL_FALLBACK_ENABLED`, `OBSCURA_BINARY` |
| Backfill | `BACKFILL_ENABLED`, `BACKFILL_QUEUE_BACKEND`, `BACKFILL_BATCH_SIZE`, `BACKFILL_MAX_ATTEMPTS` |
| 服务发现 | `ETCD_ENABLED`, `ETCD_ENDPOINTS`, `ETCD_DISCOVER_READERS`, `ETCD_REGISTER_SELF`, `ETCD_REGISTER_READER_URLS` |
| 反爬设置 | `ANTI_CRAWL_ENABLED`, `ENABLE_USER_AGENT_ROTATION`, `ENABLE_REQUEST_DELAY`, `PROXY_LIST` |

提取策略：

| 策略 | 行为 |
|---|---|
| `reader_first` | 先 Reader，再 HTTP/browser；Reader 可用时这是基于 benchmark 的质量优先默认策略 |
| `http_first` | 先轻量 HTTP，再 Reader/browser；默认主 Docker app 使用该策略 |
| `quality_gated` | 先 HTTP，但低质量结果会升级到 Reader |

浏览器后端：

| 后端 | 行为 |
|---|---|
| `local` | 使用本地 Playwright 兜底 |
| `remote` | 使用 Browserless/CDP |
| `obscura` | 使用 Obscura CLI |
| `hybrid` | 依次尝试 Obscura、Browserless/CDP、本地 Playwright |

## Benchmark

当前稳定 benchmark 报告见 [benchmark-all-stable-report.md](benchmark-all-stable-report.md) 和 [benchmark-all-stable-report.html](benchmark-all-stable-report.html)。

已提交的稳定结果：

| Rank | Profile | Overall | Usable | Recall | JS | Boilerplate | Median ms | URLs/s |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | `reader_service` | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 15.93 | 376.65 |
| 2 | `http_extractor` | 52.6% | 66.7% | 66.7% | 0.0% | 100.0% | 463.03 | 12.96 |
| 3 | `scrapling_static` | 29.2% | 16.7% | 66.7% | 0.0% | 0.0% | 221.41 | 27.10 |
| 4 | `local_playwright` | 12.6% | 16.7% | 16.7% | 0.0% | 0.0% | 5341.88 | 1.12 |

运行上下文：

| 项目 | 值 |
|---|---|
| Rounds | `2` |
| URL count | `6` |
| Profiles | `http_extractor`, `reader_service`, `scrapling_static`, `local_playwright` |
| 综合分权重 | quality 45% + capability 25% + throughput 20% + latency 10% |

结论：

- Reader 在确定性 fixture benchmark 中质量和吞吐最好，所以 Reader 容量可用时优先使用 `reader_first`。
- HTTP extractor 对静态页面仍然很有价值，适合在未启动 Reader 的轻量部署中作为默认快速路径。
- 浏览器渲染成本最高，应该保持为最后兜底，只处理静态/Reader 都失败的页面。
- Benchmark 结果会受环境、可选依赖、浏览器安装状态和站点行为影响，适合作为提取顺序参考，不是绝对生产承诺。

本地运行 benchmark：

```powershell
$env:TRAILSEARCH_RUN_BENCHMARK = "1"
$env:TRAILSEARCH_BENCHMARK_PRESET = "fast"
$env:TRAILSEARCH_BENCHMARK_OUTPUT = "benchmark-results.json"
pytest tests/test_benchmark.py -m benchmark -s --no-cov
python scripts/render_benchmark_report.py benchmark-results.json --markdown benchmark-report.md --html benchmark-report.html
```

常用选项：

| 变量 | 用途 |
|---|---|
| `TRAILSEARCH_BENCHMARK_PRESET=fast` | HTTP、Reader、Scrapling static 和 Reader pipeline |
| `TRAILSEARCH_BENCHMARK_PRESET=quick` | 增加 local Playwright 对比 |
| `TRAILSEARCH_BENCHMARK_PRESET=browser` | 重点比较浏览器相关 profile |
| `TRAILSEARCH_BENCHMARK_PRESET=all` | 运行所有可用 profile |
| `TRAILSEARCH_BENCHMARK_ROUNDS=3` | 增加测量轮数 |
| `TRAILSEARCH_BENCHMARK_INSTALL_BROWSERS=1` | 允许 benchmark 自动安装缺失的 Playwright 浏览器 |
| `TRAILSEARCH_BENCHMARK_PROFILES=http_extractor,reader_service` | 指定 profile |

可选 Scrapling profile 需要：

```powershell
pip install "scrapling[fetchers]"
scrapling install
```

## 开发

```bash
pip install -e ".[dev]"
pytest
pytest --cov=trailsearch --cov-report=html
ruff check src/ tests/
black src/ tests/
mypy src/
```

Benchmark 测试默认跳过，只有设置 `TRAILSEARCH_RUN_BENCHMARK=1` 才会运行。

## 项目结构

```text
.
├── src/trailsearch/                  # Python package
├── tests/                          # 单元、集成和可选 benchmark 测试
├── scripts/                        # Benchmark 报告渲染/合并脚本
├── searxng/settings.yml            # 本地 SearXNG 配置
├── data/                           # 开发环境 SQLite 索引目录
├── docker-compose.yml              # 本地栈
├── docker-compose.distributed.yml  # Redis + etcd + 多节点栈
├── .env.example                    # 完整环境变量参考
├── CACHE_GUIDE.md                  # 缓存说明
├── DOCKER_PROFILES.md              # Compose profile 说明
├── ARCHITECTURE.md     # 架构细节
└── benchmark-all-stable-report.md  # 已提交的稳定 benchmark 报告
```

## 部署注意事项

SearXNG 必须开启 JSON 搜索结果。本地 [searxng/settings.yml](searxng/settings.yml) 已包含 JSON 支持。如果使用自维护 SearXNG，请确保：

```yaml
search:
  formats:
    - html
    - json
```

生产环境建议：

- 保持 `EXTERNAL_SEARCH_ENABLED=false`，除非明确需要 Brave API 兜底。
- 使用 Redis 作为共享 crawl/search 缓存和分布式 backfill 队列。
- Reader/crawler 节点需要动态发现时启用 etcd。
- 限制浏览器渲染并发，因为它是成本最高的阶段。
- 在 API 前放置反向代理，处理 TLS、鉴权、请求大小限制和限流。

## 许可证

本项目采用 [MIT License](LICENSE)。
