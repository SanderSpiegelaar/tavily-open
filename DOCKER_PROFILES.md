# TrailSearch Docker Compose 指南

TrailSearch 提供三个 Compose 入口，分别用于完整 Demo、轻量开发和分布式试验。对外 Demo 或自用时，优先使用 `docker-compose.demo.yml`。

## 完整栈

完整栈包含 FastAPI、Redis、SearXNG、Reader 和 Browserless：

```bash
cp .env.demo.example .env.demo
docker compose --env-file .env.demo -f docker-compose.demo.yml up -d --build
```

运行状态：

```bash
docker compose --env-file .env.demo -f docker-compose.demo.yml ps
```

查看关键日志：

```bash
docker compose --env-file .env.demo -f docker-compose.demo.yml \
  logs -f app searxng reader browserless
```

停止服务但保留数据卷：

```bash
docker compose --env-file .env.demo -f docker-compose.demo.yml down
```

启用 Cloudflare Named Tunnel：

```bash
docker compose --env-file .env.demo -f docker-compose.demo.yml \
  --profile tunnel up -d --build
```

启动 Tunnel 前必须在 `.env.demo` 中配置 `CLOUDFLARE_TUNNEL_TOKEN`。

## 轻量开发栈

根目录的 `docker-compose.yml` 主要用于分阶段开发：

```bash
# Redis + SearXNG + 轻量 API
docker compose up -d --build

# 增加 Reader 和 Reader 版 API
docker compose --profile reader up -d --build

# 增加 Browserless
docker compose --profile browserless up -d --build

# 启动所有开发 profile，包含两套 API
docker compose --profile full up -d --build
```

`full` profile 会同时启动 `app` 和 `app-reader`，不是推荐的 Demo 入口。需要单一、完整 API 时使用上面的 `docker-compose.demo.yml`。

## 分布式栈

```bash
docker compose -f docker-compose.distributed.yml up -d --build
```

该栈会启动 Redis、etcd、SearXNG、两个 Reader、一个 API 和两个 crawler worker，仅用于分布式行为验证。

## 地址

| 服务 | 地址 |
|---|---|
| TrailSearch API | `http://127.0.0.1:8000` |
| Swagger UI | `http://127.0.0.1:8000/docs` |
| ReDoc | `http://127.0.0.1:8000/redoc` |

Demo Compose 不会向主机公开 Redis、SearXNG、Reader 和 Browserless 端口。
