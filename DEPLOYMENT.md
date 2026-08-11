# Demo Environment Deployment

## Recommended Approach

This service is not a purely stateless API. The complete search pipeline includes FastAPI, SearXNG, Redis, persistent SQLite data, and optionally a browser.

For a demo environment, the recommended approach is to deploy `docker-compose.demo.yml` to a Linux cloud server and choose the access method based on the target region:

* Users in China: use Alibaba Cloud ECS or a Simple Application Server, point the domain directly to the server, and configure HTTPS.
* Overseas users or temporary demos: run Cloudflare Tunnel on the same server and let Cloudflare provide HTTPS, without exposing the API’s inbound port.

The recommended minimum server configuration is 4 CPU cores and 8 GB of RAM, because the demo stack runs both Reader and Browserless.

The server must have Docker Engine, Docker Compose v2, and Git installed.

## Starting the Services

```bash
git clone <repository-url> trailsearch
cd trailsearch
cp .env.demo.example .env.demo
```

Edit `.env.demo` and, at minimum, replace `SEARXNG_SECRET` with a long randomly generated string.

Then start the services:

```bash
docker compose --env-file .env.demo -f docker-compose.demo.yml up -d --build
docker compose --env-file .env.demo -f docker-compose.demo.yml ps
curl http://127.0.0.1:8000/healthz
```

By default, the API only listens on the server’s `127.0.0.1:8000`, so the unauthenticated API is not exposed directly to the public internet.

SQLite, Reader, and Redis data are stored in the named volumes `app_data`, `reader_data`, and `redis_data`, respectively.

## Cloudflare Tunnel

Create a Tunnel in the Cloudflare Zero Trust dashboard and set the Public Hostname’s Origin Service to:

```text
http://app:3000
```

Add the Tunnel token to `.env.demo`:

```dotenv
CLOUDFLARE_TUNNEL_TOKEN=replace-with-dashboard-token
```

Start the stack with the tunnel profile:

```bash
docker compose --env-file .env.demo -f docker-compose.demo.yml --profile tunnel up -d --build
```

The API does not include built-in user authentication. Before exposing a public demo, you should add a Cloudflare Access policy for the hostname, such as an email one-time passcode or a policy restricted to specific accounts.

Swagger UI is available at `/docs`.

If you do not have a domain and only need a short-term test, you can run a Quick Tunnel on the server:

```bash
cloudflared tunnel --url http://127.0.0.1:8000
```

The Quick Tunnel URL is not persistent and is only suitable for temporary demos. Both the server and the `cloudflared` command must remain running.

## Alibaba Cloud Function Compute

Function Compute can only run the main API using a custom container. SearXNG, Redis, and persistent storage would need to be moved to external services.

This project also uses long-running requests, background backfill tasks, SQLite, and an optional browser process. Cold starts and function instance recycling can therefore affect application behavior.

Unless you retain only a stateless `search-only` endpoint, Function Compute is not recommended for hosting the demo environment.

If Function Compute must be used, you will need to:

1. Push the container image to Alibaba Cloud Container Registry (ACR).
2. Use a custom-container function and configure the container port as `3000`.
3. Host SearXNG and Redis externally, and disable `LOCAL_INDEX_ENABLED`, `BACKFILL_ENABLED`, and the local browser.
4. Increase the function timeout enough to cover search and crawling duration, and configure public network access and authentication.

## Cloudflare Containers and Other Platforms

Cloudflare Workers and Pages cannot directly run the current Python and browser dependencies.

Cloudflare Containers could theoretically run the main container image, but the service is currently in beta, its disk storage is ephemeral, and SearXNG and Redis would still need to be deployed separately. It is therefore not the preferred platform for this demo stack.

Container platforms such as Railway, Render, and Fly.io can also host the application by splitting it into three separate services, but networking, persistent volumes, and health checks would need to be configured individually.

For the current project, Docker Compose on a single cloud server is simpler and also makes it easier to troubleshoot networking and anti-bot issues related to the search engine.

## Verification

```bash
curl https://your-demo-host/healthz

curl -X POST https://your-demo-host/tavily/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"OpenAI Codex","max_results":3,"search_depth":"basic"}'
```

View the service logs with:

```bash
docker compose --env-file .env.demo -f docker-compose.demo.yml logs -f app searxng
```
