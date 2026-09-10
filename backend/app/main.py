import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, HttpUrl, field_validator
from starlette.background import BackgroundTask

APP_VERSION = "2.1.0"
TMP_ROOT = Path(os.getenv("VELO_TMP_DIR", "/tmp/velo"))
TMP_ROOT.mkdir(parents=True, exist_ok=True)


def env_int(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


MAX_JOBS = env_int("VELO_MAX_CONCURRENT_JOBS", 1, 1)
MAX_QUEUE = env_int("VELO_MAX_QUEUE", 2, 0)
JOB_TTL = env_int("VELO_JOB_TTL_SECONDS", 1800, 300)
MAX_FILE_BYTES = env_int("VELO_MAX_FILE_BYTES", 1024 * 1024 * 1024, 50 * 1024 * 1024)
INFO_LIMIT = env_int("VELO_INFO_LIMIT_PER_MINUTE", 10, 1)
DOWNLOAD_LIMIT = env_int("VELO_DOWNLOAD_LIMIT_PER_HOUR", 5, 1)
RATE_WINDOW = 60
DOWNLOAD_WINDOW = 3600

app = FastAPI(title="Velo Media API", version=APP_VERSION)

origins = [
    x.strip()
    for x in os.getenv("CORS_ORIGINS", "*").split(",")
    if x.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next) -> Response:
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=()",
    )
    response.headers.setdefault("Cache-Control", "no-store")
    return response


jobs: dict[str, dict[str, Any]] = {}
rate_limits: dict[str, dict[str, list[float]]] = {}
lock = threading.RLock()
job_slots = threading.Semaphore(MAX_JOBS)

YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}

QUALITY_VALUES = {"best", "1080", "720", "480", "360"}


class InfoRequest(BaseModel):
    url: HttpUrl


class DownloadRequest(BaseModel):
    url: HttpUrl
    quality: str = "best"

    @field_validator("quality")
    @classmethod
    def validate_quality(cls, value: str) -> str:
        return value if value in QUALITY_VALUES else "best"


def validate_youtube_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname.lower().rstrip(".") if parsed.hostname else ""

    if parsed.scheme not in {"http", "https"} or host not in YOUTUBE_HOSTS:
        raise HTTPException(
            400,
            "Velo currently supports YouTube video URLs only.",
        )

    if host in {"youtu.be", "www.youtu.be"}:
        video_id = parsed.path.strip("/").split("/")[0]
    elif parsed.path == "/watch":
        video_id = parse_qs(parsed.query).get("v", [""])[0]
    elif parsed.path.startswith("/shorts/"):
        parts = parsed.path.split("/")
        video_id = parts[2] if len(parts) > 2 else ""
    elif parsed.path.startswith("/embed/"):
        parts = parsed.path.split("/")
        video_id = parts[2] if len(parts) > 2 else ""
    else:
        video_id = ""

    if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id or ""):
        raise HTTPException(
            400,
            "That does not look like a valid YouTube video URL.",
        )

    return video_id


def client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def enforce_rate_limit(
    request: Request,
    bucket: str,
    limit: int,
    window: int,
) -> None:
    now = time.time()
    key = client_key(request)

    with lock:
        record = rate_limits.setdefault(key, {})
        events = record.setdefault(bucket, [])
        cutoff = now - window
        events[:] = [stamp for stamp in events if stamp > cutoff]

        if len(events) >= limit:
            raise HTTPException(
                429,
                "Rate limit reached. Please wait and try again later.",
            )

        events.append(now)

        if len(record) == 0:
            rate_limits.pop(key, None)


def clean_title(value: str) -> str:
    value = re.sub(
        r'[\\/:*?"<>|\x00-\x1f]+',
        "_",
        value or "video",
    )
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:150] or "video"


def human_duration(seconds: Any) -> str | None:
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return None

    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)

    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def quality_format(quality: str) -> str:
    if quality == "1080":
        return "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"

    if quality == "720":
        return "bestvideo[height<=720]+bestaudio/best[height<=720]/best"

    if quality == "480":
        return "bestvideo[height<=480]+bestaudio/best[height<=480]/best"

    if quality == "360":
        return "bestvideo[height<=360]+bestaudio/best[height<=360]/best"

    return "bestvideo+bestaudio/best"


def extractor_options(skip_download: bool = True) -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": skip_download,
        "socket_timeout": 25,
        "retries": 2,
        "fragment_retries": 2,
        "extractor_retries": 2,
    }


def get_info(url: str) -> dict[str, Any]:
    with yt_dlp.YoutubeDL(extractor_options()) as ydl:
        return ydl.extract_info(url, download=False)


def available_qualities(info: dict[str, Any]) -> list[str]:
    heights = set()

    for fmt in info.get("formats") or []:
        try:
            height = int(fmt.get("height") or 0)
        except (TypeError, ValueError):
            continue

        if height:
            heights.add(height)

    result = [
        q
        for q in ("1080", "720", "480", "360")
        if any(h >= int(q) for h in heights)
    ]

    return result or ["360"]


def prune_jobs() -> None:
    now = time.time()

    with lock:
        stale = [
            job_id
            for job_id, job in jobs.items()
            if job.get("status") in {"complete", "error"}
            and now - job.get("updated_at", now) > JOB_TTL
        ]

        for job_id in stale:
            job = jobs.pop(job_id, None)

            if job and job.get("file"):
                shutil.rmtree(
                    Path(job["file"]).parent,
                    ignore_errors=True,
                )

        for key in list(rate_limits):
            record = rate_limits[key]

            for bucket, events in list(record.items()):
                cutoff = now - (
                    DOWNLOAD_WINDOW
                    if bucket == "download"
                    else RATE_WINDOW
                )

                events[:] = [
                    stamp for stamp in events if stamp > cutoff
                ]

                if not events:
                    record.pop(bucket, None)

            if not record:
                rate_limits.pop(key, None)


def update_job(job_id: str, **changes: Any) -> None:
    with lock:
        if job_id in jobs:
            jobs[job_id].update(changes)
            jobs[job_id]["updated_at"] = time.time()


def queued_count() -> int:
    return sum(
        1
        for job in jobs.values()
        if job.get("status") == "queued"
    )


def run_job(job_id: str, url: str, quality: str) -> None:
    job_dir = TMP_ROOT / job_id
    acquired = False

    try:
        update_job(
            job_id,
            message="Waiting for an available worker…",
        )

        job_slots.acquire()
        acquired = True

        job_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        def hook(data: dict[str, Any]) -> None:
            status = data.get("status")

            if status == "downloading":
                total = (
                    data.get("total_bytes")
                    or data.get("total_bytes_estimate")
                    or 0
                )

                done = data.get("downloaded_bytes") or 0
                progress = (
                    done / total * 100
                    if total
                    else 0
                )

                update_job(
                    job_id,
                    status="downloading",
                    progress=min(91, round(progress, 1)),
                    message="Downloading…",
                    speed=data.get("speed") or 0,
                    eta=data.get("eta"),
                )

            elif status == "finished":
                update_job(
                    job_id,
                    progress=93,
                    message="Merging audio and video…",
                )

        update_job(
            job_id,
            status="downloading",
            progress=1,
            message="Preparing media…",
        )

        outtmpl = str(
            job_dir / "%(title).150B.%(ext)s"
        )

        opts = extractor_options(
            skip_download=False
        )

        opts.update(
            {
                "format": quality_format(quality),
                "merge_output_format": "mp4",
                "outtmpl": outtmpl,
                "progress_hooks": [hook],
                "restrictfilenames": True,
                "max_filesize": MAX_FILE_BYTES,
            }
        )

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                url,
                download=True,
            )

            prepared = Path(
                ydl.prepare_filename(info)
            )

            candidates = [
                prepared.with_suffix(".mp4"),
                prepared,
            ]

            media_files = [
                p
                for p in job_dir.iterdir()
                if p.is_file()
                and p.suffix.lower()
                in {
                    ".mp4",
                    ".mkv",
                    ".webm",
                    ".mov",
                    ".m4v",
                }
            ]

            final = next(
                (
                    p
                    for p in candidates
                    if p.exists()
                ),
                None,
            )

            if final is None and media_files:
                final = max(
                    media_files,
                    key=lambda p: p.stat().st_mtime,
                )

            if final is None or not final.exists():
                raise RuntimeError(
                    "The media file was not produced."
                )

            if final.stat().st_size > MAX_FILE_BYTES:
                raise RuntimeError(
                    "The generated file is larger than the free-server limit."
                )

        extension = (
            final.suffix.lower()
            or ".mp4"
        )

        filename = (
            clean_title(info.get("title"))
            + extension
        )

        update_job(
            job_id,
            status="complete",
            progress=100,
            message="Download ready",
            filename=filename,
            file=str(final),
            title=info.get("title") or "Video",
        )

    except Exception as exc:
        shutil.rmtree(
            job_dir,
            ignore_errors=True,
        )

        message = str(exc).replace(
            "\n",
            " ",
        ).strip()

        lowered = message.lower()

        if (
            "sign in to confirm" in lowered
            or "bot" in lowered
        ):
            message = (
                "YouTube rejected the server request. "
                "Try another video or try again later."
            )

        update_job(
            job_id,
            status="error",
            progress=0,
            message=(
                message[:600]
                or "Download failed."
            ),
        )

    finally:
        if acquired:
            job_slots.release()


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "name": "Velo Media API",
        "version": APP_VERSION,
        "status": "ok",
    }


@app.get("/health")
def health() -> dict[str, Any]:
    prune_jobs()

    with lock:
        active = sum(
            1
            for job in jobs.values()
            if job.get("status") == "downloading"
        )

        queued = queued_count()

    return {
        "status": "ok",
        "version": APP_VERSION,
        "active_jobs": active,
        "queued_jobs": queued,
        "max_concurrent_jobs": MAX_JOBS,
        "queue_limit": MAX_QUEUE,
    }


@app.post("/api/info")
def info(
    req: InfoRequest,
    request: Request,
) -> dict[str, Any]:
    prune_jobs()

    enforce_rate_limit(
        request,
        "info",
        INFO_LIMIT,
        RATE_WINDOW,
    )

    url = str(req.url)
    validate_youtube_url(url)

    try:
        data = get_info(url)

        return {
            "id": data.get("id"),
            "title": data.get("title"),
            "thumbnail": data.get("thumbnail"),
            "duration": data.get("duration"),
            "duration_text": human_duration(
                data.get("duration")
            ),
            "webpage_url": (
                data.get("webpage_url")
                or url
            ),
            "uploader": data.get("uploader"),
            "channel": data.get("channel"),
            "view_count": data.get("view_count"),
            "available_qualities": available_qualities(
                data
            ),
        }

    except HTTPException:
        raise

    except Exception as exc:
        raise HTTPException(
            400,
            "Unable to analyze this video: "
            + str(exc)[:450],
        ) from exc


@app.post("/api/download")
def download(
    req: DownloadRequest,
    request: Request,
) -> dict[str, Any]:
    prune_jobs()

    enforce_rate_limit(
        request,
        "download",
        DOWNLOAD_LIMIT,
        DOWNLOAD_WINDOW,
    )

    url = str(req.url)
    validate_youtube_url(url)

    with lock:
        queued = queued_count()

        if queued >= MAX_QUEUE:
            raise HTTPException(
                429,
                "The free server queue is full. "
                "Please wait for a download to finish.",
            )

        job_id = uuid.uuid4().hex

        jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "progress": 0,
            "message": "Queued…",
            "filename": None,
            "file": None,
            "created_at": time.time(),
            "updated_at": time.time(),
        }

    thread = threading.Thread(
        target=run_job,
        args=(job_id, url, req.quality),
        daemon=True,
        name=f"velo-{job_id[:8]}",
    )

    thread.start()

    return {
        "job_id": job_id,
        "status": "queued",
    }


@app.get("/api/download/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    if not re.fullmatch(
        r"[a-f0-9]{32}",
        job_id,
    ):
        raise HTTPException(
            400,
            "Invalid job ID.",
        )

    prune_jobs()

    with lock:
        job = jobs.get(job_id)

        if not job:
            raise HTTPException(
                404,
                "Job not found or expired.",
            )

        return {
            k: v
            for k, v in job.items()
            if k != "file"
        }


@app.get("/api/download/{job_id}/file")
def job_file(job_id: str) -> FileResponse:
    if not re.fullmatch(
        r"[a-f0-9]{32}",
        job_id,
    ):
        raise HTTPException(
            400,
            "Invalid job ID.",
        )

    prune_jobs()

    with lock:
        job = jobs.get(job_id)

        if not job:
            raise HTTPException(
                404,
                "Job not found or expired.",
            )

        if (
            job.get("status") != "complete"
            or not job.get("file")
        ):
            raise HTTPException(
                409,
                "The download is not ready.",
            )

        path = Path(job["file"])
        filename = (
            job.get("filename")
            or path.name
        )

    if (
        not path.exists()
        or not path.is_file()
        or path.parent.parent != TMP_ROOT
    ):
        raise HTTPException(
            404,
            "The generated file has expired. "
            "Start the download again.",
        )

    def cleanup() -> None:
        shutil.rmtree(
            path.parent,
            ignore_errors=True,
        )

        with lock:
            jobs.pop(job_id, None)

    media_type = (
        "video/mp4"
        if path.suffix.lower() == ".mp4"
        else "application/octet-stream"
    )

    return FileResponse(
        path,
        media_type=media_type,
        filename=filename,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
        background=BackgroundTask(cleanup),
)
