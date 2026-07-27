# 演示环境部署

## 推荐方案

这套服务不是纯无状态 API。完整搜索链路包含 FastAPI、SearXNG、Redis、SQLite
持久数据，并可选调用浏览器。因此，最适合演示环境的方式是把
`docker-compose.demo.yml` 部署到一台 Linux 云服务器，再按访问区域选择入口：

- 国内用户：阿里云 ECS 或轻量应用服务器，域名直接解析到服务器并配置 HTTPS。
- 海外或临时演示：同一台服务器运行 Cloudflare Tunnel，由 Cloudflare 提供 HTTPS，
  无需开放 API 入站端口。

建议最低配置为 4 核 8 GB，因为演示栈同时运行 Reader 和 Browserless。服务器需要
安装 Docker Engine、Compose v2 和 Git。

## 启动服务

```bash
git clone <repository-url> trailsearch
cd trailsearch
cp .env.demo.example .env.demo
```

编辑 `.env.demo`，至少把 `SEARXNG_SECRET` 换成随机长字符串。随后启动：

```bash
docker compose --env-file .env.demo -f docker-compose.demo.yml up -d --build
docker compose --env-file .env.demo -f docker-compose.demo.yml ps
curl http://127.0.0.1:8000/healthz
```

默认只监听服务器的 `127.0.0.1:8000`，不会把未鉴权 API 直接暴露到公网。SQLite、
Reader 和 Redis 数据分别保存在命名卷 `app_data`、`reader_data`、`redis_data` 中。

## Cloudflare Tunnel

在 Cloudflare Zero Trust 控制台创建 Tunnel，并把 Public Hostname 的 Origin Service
设置为 `http://app:3000`。将 Tunnel token 写入 `.env.demo`：

```dotenv
CLOUDFLARE_TUNNEL_TOKEN=replace-with-dashboard-token
```

带 tunnel profile 启动：

```bash
docker compose --env-file .env.demo -f docker-compose.demo.yml --profile tunnel up -d --build
```

API 没有内置用户鉴权。公开演示前，应在 Cloudflare Access 中为该 hostname 添加邮箱
一次性验证码或指定账号策略。Swagger UI 位于 `/docs`。

如果没有域名，只做短时间测试，可以在服务器上运行 Quick Tunnel：

```bash
cloudflared tunnel --url http://127.0.0.1:8000
```

Quick Tunnel 地址不固定，只适合临时演示，服务器和命令都必须持续运行。

## 阿里云函数计算

函数计算只能使用自定义容器运行主 API，并把 SearXNG、Redis 和持久存储拆到外部服务。
本项目还有长请求、后台 backfill、SQLite 和可选浏览器进程，冷启动与函数实例回收都会
影响行为。除非只保留无状态的 `search-only` 接口，否则不建议用函数计算承载演示环境。

若必须使用函数计算，需要同时完成：

1. 把镜像推送到阿里云容器镜像服务 ACR。
2. 使用自定义容器函数，容器端口设为 `3000`。
3. 外置 SearXNG 和 Redis，关闭 `LOCAL_INDEX_ENABLED`、`BACKFILL_ENABLED` 与本地浏览器。
4. 把函数超时提高到能够覆盖搜索和抓取时长，并配置公网访问与认证。

## Cloudflare Containers 与其他平台

Cloudflare Workers/Pages 无法直接运行当前 Python 和浏览器依赖。Cloudflare Containers
理论上能运行主镜像，但目前属于 beta，磁盘是临时的，而且 SearXNG/Redis 仍需单独部署；
它不适合作为这套演示栈的首选。

Railway、Render、Fly.io 等容器平台也能拆成三个服务部署，但需要分别配置网络、持久卷和
健康检查。对当前项目而言，一台云服务器上的 Compose 更简单，也更容易排查搜索引擎的
网络和反爬问题。

## 验证

```bash
curl https://your-demo-host/healthz

curl -X POST https://your-demo-host/tavily/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"OpenAI Codex","max_results":3,"search_depth":"basic"}'
```

查看运行日志：

```bash
docker compose --env-file .env.demo -f docker-compose.demo.yml logs -f app searxng
```
