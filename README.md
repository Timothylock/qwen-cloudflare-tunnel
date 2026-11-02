# Qwen via the NAS Cloudflare Tunnel

Exposes the OpenAI-compatible Qwen server at `100.110.158.5` (which cannot
run cloudflared itself) through the existing "Tim NAS" Cloudflare Tunnel,
with a keepalive shim in front so long generations survive Cloudflare's
~125s silence limit (error 524, not configurable on Free/Pro).

```
client -> https://qwen.timlock.dev
       -> Cloudflare -> Tim NAS tunnel (pokebinder-cloudflared-1)
       -> qwen-shim:8080        (this project, on tim-nas)
       -> http://100.110.158.5:8001   (Qwen server, port in .env)
```

## Verified facts (checked from the NAS, 2026-11-02)

- Upstream is **http://100.110.158.5:8001** with the API under `/v1`. The
  NAS itself is 100.110.158.36/26 on the same subnet (100.110.158.0/26),
  route to .5 is direct, port 8001 open.
- The upstream requires its **own API key**: `GET /v1/models` without one
  returns 401. Use that same key as `SHIM_API_KEY` (see Deploy step 1).
- Hostname **qwen.timlock.dev** is still an assumption. Pick another in the
  Cloudflare step if you prefer; nothing in this project depends on it.
- `/health` on the shim answers even while the upstream is down, and
  `/health/upstream` tells you which side is at fault.

## Releases

Same flow as muse-calling: git tag `vX.Y.Z` -> GitHub Actions builds the
image and pushes `ghcr.io/timothylock/qwen-cloudflare-tunnel:vX.Y.Z`.
The NAS only ever pulls. No source code lives on the NAS.
If the GHCR package is private, either set it public in the repo's
Packages settings (the source is public anyway) or `docker login ghcr.io`
on the NAS once.

## Deploy on tim-nas

Only two files go on the NAS: `docker-compose.yml` and `.env`, in
`/volume2/docker/qwen-tunnel`.

1. Configure secrets (they stay on the NAS):

   ```
   cd /volume2/docker/qwen-tunnel
   cp .env.example .env
   # edit .env: set SHIM_API_KEY to a long random value (compose refuses
   # to start without it; proxy.py alone treats it as optional for tests),
   # and IMAGE_TAG to the release to run.
   # If the Qwen server itself runs with an --api-key, use the SAME value
   # there: the shim checks the client's key, then forwards the header
   # upstream untouched.
   ```

2. Start it (UGOS Docker project import also works):

   ```
   docker compose up -d
   docker logs -f qwen-shim     # expect "qwen-shim listening on :8080"
   ```

   Updating later = bump `IMAGE_TAG` in `.env`, `docker compose up -d`.

   Requires the `pokebinder_default` docker network to exist (it does while
   the tunnel connector container is up). Check: `docker network ls`.

4. Smoke test from the NAS, works even before the Qwen box is back:

   ```
   curl localhost:18090/health
   # {"status": "ok"}
   ```

   Once the Qwen box is back:

   ```
   curl localhost:18090/v1/models -H "Authorization: Bearer <SHIM_API_KEY>"
   curl localhost:18090/health/upstream
   # 200 {"status": "reachable", "upstream_status": 200}
   # 502 {"status": "unreachable"}   (details in `docker logs qwen-shim`)
   ```

## The one Cloudflare step (Timothy, in the dashboard)

Zero Trust -> Networks -> Connectors -> the existing **Tim NAS** tunnel ->
Published application routes -> Add:

- Hostname: `qwen.timlock.dev`
- Service: `http://qwen-shim:8080`
- No path restriction.

This works because qwen-shim joins `pokebinder_default`, the connector's
own network (service-name origins only resolve there; that was the
muse-calling lesson).

Fallback if that origin 502s: change the service to the NAS host port,
`http://100.110.158.1:18090`. If that also 502s, the shim cannot egress to
the LAN from its bridge network; tell Muse and we switch the shim to
host networking.

## Client setup

- Base URL: `https://qwen.timlock.dev/v1`
- API key: the `SHIM_API_KEY` value, sent as `Authorization: Bearer <key>`
  (the only accepted form)
- Request bodies need a plain `Content-Length`, max `MAX_BODY_BYTES`
  (32 MiB, else 413). Chunked uploads (`Transfer-Encoding`) get 400.
- Prefer `stream: true`. Non-streaming works too (the shim heartbeats it),
  it just cannot show progress and wastes the whole wait if the client
  times out on its own side first. Also raise the client's own request
  timeout; Cloudflare is no longer the binding constraint, the client is.

## How the timeout is beaten

- Streaming, slow first token: shim commits a 200 SSE response after 10s
  and sends `: keepalive` comments every 15s until tokens flow, then relays
  tokens and keepalives any idle gap over 15s.
- Non-streaming, slow generation: shim commits a 200 chunked response after
  45s and sends one space every 15s. Leading spaces are valid JSON
  whitespace, so the client parses the final body normally.
- Fast responses pass through with their real status and headers.
- Trade-off: after an early commit the real upstream status can no longer
  be sent. A late upstream error arrives as a JSON error body under 200
  (a non-JSON error page is replaced by `{"error": ...}`).
  Preferable to a guaranteed 524 at 125s.
- Hard cap: `MAX_REQUEST_SECONDS` (default 1800) per request.

All timers are env vars in docker-compose.yml / .env.

## Tests

Local, no NAS or Qwen needed (mock upstream with artificial delays, shim
timers compressed to seconds):

```
python3 tests/run_tests.py    # 10 cases: auth, fast/slow, stream/non-stream, errors, smuggling
```

## Files

- `proxy.py` - the keepalive proxy, the whole app (Python stdlib only)
- `Dockerfile` - python:3.13-alpine
- `docker-compose.yml` - NAS deployment (pulls the GHCR image)
- `.env.example` - upstream + API key + timers + IMAGE_TAG
- `.github/workflows/publish-image.yml` - tag -> GHCR build
- `tests/` - mock upstream + test runner
