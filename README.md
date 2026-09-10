# Velo — production-hardened YouTube downloader

Velo is a small, mobile-first web application for downloading YouTube videos that the operator has permission to download. It uses a static frontend and a FastAPI/yt-dlp/FFmpeg backend.

## Architecture

- `frontend/` — static HTML application, deployable to Netlify or any static host.
- `backend/` — FastAPI API running yt-dlp + yt-dlp-ejs + Deno + FFmpeg.
- `backend/Dockerfile` — Render-compatible container.
- `backend/render.yaml` — Render deployment configuration.
- `netlify.toml` — static-site build and security headers.

## Production hardening included

- YouTube-only URL validation.
- No playlist downloads.
- Bounded concurrent downloads and a small queue.
- Per-client rate limits for metadata and downloads.
- Maximum generated-file size.
- Strict job ID validation.
- Temporary job storage with expiry.
- Files are deleted after successful delivery.
- CORS is configurable.
- Security response headers.
- No persistent user data or credentials.
- FFmpeg and a JavaScript runtime required by current yt-dlp YouTube extraction are included in the container.
- Non-root Docker process.
- Health endpoint for deployment checks.

## Important free-tier limitation

The default deployment is intentionally constrained for free hosting. It is suitable for a small number of authorized users, not a high-volume public downloader. A free Render web service can sleep when idle and has limited CPU/RAM/storage. For a genuinely high-volume production service, move the backend to a paid worker/service with persistent object storage and a distributed job queue.

## Environment variables

| Variable | Default | Purpose |
|---|---:|---|
| `CORS_ORIGINS` | `*` | Comma-separated allowed frontend origins |
| `VELO_MAX_CONCURRENT_JOBS` | `1` | Simultaneous media jobs |
| `VELO_MAX_QUEUE` | `2` | Waiting jobs |
| `VELO_JOB_TTL_SECONDS` | `1800` | Completed/failed job retention |
| `VELO_MAX_FILE_BYTES` | `1073741824` | Maximum final media size |
| `VELO_INFO_LIMIT_PER_MINUTE` | `10` | Metadata requests per client/minute |
| `VELO_DOWNLOAD_LIMIT_PER_HOUR` | `5` | Download jobs per client/hour |

For a public deployment, replace `CORS_ORIGINS=*` with the exact frontend origin once the frontend hostname is known.

## Local backend

```bash
cd backend
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

The production container is preferred because it includes FFmpeg and Deno.

## API

- `GET /health`
- `POST /api/info` with `{ "url": "https://www.youtube.com/watch?v=..." }`
- `POST /api/download` with `{ "url": "...", "quality": "best|1080|720|480|360" }`
- `GET /api/download/{job_id}`
- `GET /api/download/{job_id}/file`

## Deployment

### Backend

Deploy the `backend/` directory as a Docker web service on Render. The container listens on the platform-provided `PORT` and exposes `/health` for health checks.

### Frontend

Deploy the repository root as a static site with `netlify.toml`. The published directory is `frontend/`.

After deployment, open **Settings → Backend URL** in Velo and set it to the backend's public HTTPS URL if it differs from the default.

## Verification checklist

Before calling the installation production-ready, verify all of these against the deployed service:

1. `GET /health` returns HTTP 200.
2. The frontend loads over HTTPS.
3. A valid YouTube URL analyzes successfully.
4. An unavailable quality is disabled in the UI.
5. A download enters the queue.
6. Progress reaches 100%.
7. The returned file is a playable MP4 when MP4 was produced.
8. `/api/download/{job_id}/file` returns 404 after cleanup/expiry.
9. Invalid/non-YouTube URLs return HTTP 400.
10. Excessive requests return HTTP 429.
11. CORS only permits the intended frontend origin in the deployed environment.
12. The service remains within the hosting provider's resource limits.

## Usage rights

Velo does not grant permission to download copyrighted material. Use it only for media you are authorized to download and process.
