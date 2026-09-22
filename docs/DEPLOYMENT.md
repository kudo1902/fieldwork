# fieldwork — Deployment

How the web tier actually gets served, and how a release reaches the box.

Companion to [`ARCHITECTURE.md`](./ARCHITECTURE.md). Corresponds to tasks B1 and D5 in the
[implementation plan](./IMPLEMENTATION_PLAN.md).

---

## 1. Topology

```
                     browser
                        │
                   :80  :443
                        ▼
                 ┌─────────────┐
                 │    Caddy    │   TLS · static files · reverse proxy
                 └──┬───┬───┬──┘
          /         │   │   │        api.* ──► Flask   :8080   (JSON only)
      (static SPA) ─┘   │   └─────── s3.*  ──► MinIO   :9000   (image bytes)
                        │
                 baked into the
                 Caddy image
                        
   private network (no published ports): api · worker ×N · vllm · postgres · redis · minio
```

**Caddy is the only container with published ports.** Everything else — including vLLM — is
reachable only on the internal Docker network.

---

## 2. How the frontend is served

| Option | When it's right | Cost |
| --- | --- | --- |
| **Static files served by Caddy** ✅ | This product, today | None — no JS toolchain in production at all |
| A framework's static build | You adopt Preact/Svelte and want a real bundle | A Node build stage in CI |
| Flask serves the SPA (today) | Phase 1 only | Couples deploys, Python serving static, no cache headers |

`web/` is already plain HTML, CSS and JavaScript with no build step, so the production image
is three lines:

```dockerfile
# docker/web.Dockerfile
FROM caddy:2-alpine
COPY web/ /srv/web
COPY docker/Caddyfile /etc/caddy/Caddyfile
```

No `npm ci`, no lockfile, no Node runtime to patch, no supply chain to audit. Deploying the
frontend *is* deploying Caddy: atomic, versioned, one-command rollback, and no shared volume
to fall out of sync.

If the Stage C review UI outgrows vanilla, the smallest step that preserves all of this is
Preact loaded as an ESM import — still no build step, still three lines. Add a Node build
stage only when you genuinely have a bundle to build, and have it emit into `/srv/web` so
nothing else in this document changes.

---

## 3. Caddyfile

```caddyfile
{
	email ops@example.com
}

fieldwork.example.com {
	encode zstd gzip

	# JSON API. Uploads do NOT pass through here.
	handle /v1/* {
		request_body {
			max_size 2MB
		}
		reverse_proxy api:8080 {
			flush_interval -1          # SSE: never buffer
			transport http {
				read_timeout 0         # SSE: no idle timeout
			}
		}
	}

	handle {
		root * /srv/web
		try_files {path} /index.html
		file_server
	}

	header {
		Strict-Transport-Security "max-age=31536000; includeSubDomains"
		X-Content-Type-Options nosniff
		Referrer-Policy no-referrer
		-Server
	}
}

# Object storage on its own hostname -- see the gotcha in section 4.
s3.fieldwork.example.com {
	request_body {
		max_size 25MB
	}
	reverse_proxy minio:9000
}
```

`flush_interval -1` is not optional. Without it Caddy buffers the SSE stream and every
progress event arrives at once, at the end. This costs an afternoon to diagnose every time.

---

## 4. Two gotchas that will bite

**Presigned URLs must be signed for the hostname the browser uses.** An S3 signature covers
the host and path. If the API signs a URL for `minio:9000` but the browser resolves
`s3.fieldwork.example.com`, the signature fails with an opaque 403. Give MinIO its own
subdomain and tell it what it is:

```yaml
minio:
  environment:
    MINIO_SERVER_URL: https://s3.fieldwork.example.com
```

Do not proxy MinIO under a path prefix like `/s3/*`. The signature covers the path, the
prefix rewrite changes it, and you will spend a day on it.

**MinIO needs CORS for browser PUTs.** Without it the upload fails before it starts:

```bash
mc admin config set local api cors_allow_origin="https://fieldwork.example.com"
```

---

## 5. TLS — pick the one that matches your box

**A. Public DNS record, ports 80/443 reachable.** Nothing to do. Caddy obtains and renews
Let's Encrypt certificates on first start. This is the whole reason to use Caddy.

**B. Box is not publicly reachable** (home LAN, office, CGNAT) — the common case for a GPU
box. Three ways, best first:

1. **Tailscale.** Put the box on your tailnet, use MagicDNS and Tailscale's certs. Nothing is
   exposed to the internet at all, which matches the product's premise that nothing leaves
   the box. Best choice for an internal tool.
2. **DNS-01 challenge.** Real certificates without inbound 80/443. Needs a Caddy build with
   your DNS provider's plugin (`caddy-dns/cloudflare` and friends).
3. **`tls internal`.** Caddy's own CA. Free and instant, but every browser warns until you
   distribute the root certificate to every machine.

---

## 6. Compose

```yaml
# docker/compose.yml
name: fieldwork

services:
  caddy:
    image: ghcr.io/you/fieldwork-web:${TAG}
    ports: ["80:80", "443:443"]
    volumes:
      - caddy_data:/data
      - caddy_config:/config
    restart: unless-stopped
    depends_on: [api]

  migrate:
    image: ghcr.io/you/fieldwork-api:${TAG}
    command: alembic upgrade head
    env_file: .env
    depends_on:
      postgres: {condition: service_healthy}
    restart: "no"

  api:
    image: ghcr.io/you/fieldwork-api:${TAG}
    command: gunicorn -k gevent -w 4 -b 0.0.0.0:8080 fieldwork.api:app
    env_file: .env
    depends_on:
      migrate: {condition: service_completed_successfully}
      redis: {condition: service_healthy}
    restart: unless-stopped

  worker:
    image: ghcr.io/you/fieldwork-api:${TAG}
    command: rq worker --url redis://redis:6379 extractions
    env_file: .env
    deploy:
      replicas: 4                 # must exceed 1, or vLLM has nothing to batch
    stop_grace_period: 120s       # let in-flight extractions finish
    depends_on:
      redis: {condition: service_healthy}
      vllm: {condition: service_healthy}
    restart: unless-stopped

  vllm:
    image: vllm/vllm-openai:v0.x.y          # pin it
    command: >
      --model /models/gemma-4-12B-it
      --served-model-name gemma-4-12B-it
      --max-model-len 32768
      --gpu-memory-utilization 0.90
      --enable-prefix-caching
    volumes:
      - /srv/models:/models:ro              # weights on a volume, never in the image
    deploy:
      resources:
        reservations:
          devices: [{driver: nvidia, count: all, capabilities: [gpu]}]
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      start_period: 600s                    # model load is slow; do not shorten this
      interval: 15s
    restart: unless-stopped

  postgres:
    image: postgres:16-alpine
    env_file: .env
    volumes: [pg_data:/var/lib/postgresql/data]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER}"]
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    command: redis-server --appendonly yes
    volumes: [redis_data:/data]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
    restart: unless-stopped

  minio:
    image: minio/minio
    command: server /data
    env_file: .env
    environment:
      MINIO_SERVER_URL: https://s3.fieldwork.example.com
    volumes: [minio_data:/data]
    restart: unless-stopped

volumes: {caddy_data: , caddy_config: , pg_data: , redis_data: , minio_data: }
```

Note there is no `ports:` on anything but Caddy. vLLM in particular must never be published.

---

## 7. Releasing

CI builds two images per tag (`fieldwork-api`, `fieldwork-web`) and pushes to GHCR. On the box:

```bash
export TAG=v1.4.0
docker compose pull api worker caddy
docker compose run --rm migrate
docker compose up -d --no-deps api worker caddy
curl -fsS https://fieldwork.example.com/v1/healthz
```

**`--no-deps` is the important flag.** It stops Compose from restarting vLLM, whose model load
takes minutes. Application deploys must not touch the inference tier — that decoupling is
most of the value of running the model as a separate service.

**Downtime is about two seconds** and no work is lost: the API restart drops no jobs because
they live in Redis, and workers get `stop_grace_period` to finish what they hold. This is a
direct payoff of Stage B; the synchronous Phase 1 endpoint would drop every in-flight request.

Rollback is the same commands with the previous `TAG`. Migrations are the exception — write
them additively (add columns, don't drop) so an old image can run against a new schema.

---

## 8. First-run checklist

1. NVIDIA driver + Container Toolkit installed; `docker run --rm --gpus all nvidia-smi` works.
2. Weights downloaded to `/srv/models/gemma-4-12B-it`.
3. `.env` written on the box (never committed). Consider SOPS/age once more than one person
   has access.
4. DNS: `fieldwork` and `s3` both resolve to the box — or the tailnet equivalent.
5. `docker compose up -d` and wait for vLLM's healthcheck. First start is slow.
6. Create the MinIO bucket, set the lifecycle rule for retention, set CORS.
7. Verify `curl -fsS https://fieldwork.example.com/v1/healthz` reports the model as available.
8. Confirm from **outside** the box that `:8000`, `:5432`, `:6379`, `:9000` are all refused.
9. `pg_dump` cron + a restore drill. An untested backup is not a backup.
