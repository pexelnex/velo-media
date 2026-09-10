import logging
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("velo")


def env_int(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


MAX_JOBS = env_int("VELO_MAX_CONCURRENT_JOBS", 1, 1)
MAX_QUEUE = env_int("VELO_MAX_QUEUE", 2, 0)
JOB_TTL = env_int("VELO_JOB_TTL_SECONDS", 1800, 300)
MAX_FILE_BYTES = env_int("VELO_MAX_FILE_BYTES", 1024**3, 50 * 1024**2)
INFO_LIMIT = env_int("VELO_INFO_LIMIT_PER_MINUTE", 10, 1)
DOWNLOAD_LIMIT = env_int("VELO_DOWNLOAD_LIMIT_PER_HOUR", 5, 1)

RATE_WINDOW = 60
DOWNLOAD_WINDOW = 3600

app = FastAPI(
    title="Velo Media API",
    version=APP_VERSION,
)

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
async def security_headers(request: Request, call_next):
    response = await call_next(request)

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=()"
    )
    response.headers["Cache-Control"] = "no-store"

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

QUALITY_VALUES = {
    "best",
    "1080",
    "720",
    "480",
    "360",
}


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

    host = (
        parsed.hostname.lower().rstrip(".")
        if parsed.hostname
        else ""
    )

    if (
        parsed.scheme not in {"http", "https"}
        or host not in YOUTUBE_HOSTS
    ):
        raise HTTPException(
            400,
            "Velo currently supports YouTube video URLs only.",
        )

    if host in {"youtu.be", "www.youtu.be"}:
        video_id = (
            parsed.path.strip("/").split("/")[0]
        )

    elif parsed.path == "/watch":
        video_id = parse_qs(
            parsed.query
        ).get("v", [""])[0]

    elif parsed.path.startswith("/shorts/"):
        parts = parsed.path.split("/")
        video_id = (
            parts[2]
            if len(parts) > 2
            else ""
        )

    elif parsed.path.startswith("/embed/"):
        parts = parsed.path.split("/")
        video_id = (
            parts[2]
            if len(parts) > 2
            else ""
        )

    else:
        video_id = ""

    if not re.fullmatch(
        r"[A-Za-z0-9_-]{6,20}",
        video_id or "",
    ):
        raise HTTPException(
            400,
            "That does not look like a valid YouTube video URL.",
        )

    return video_id


def client_key(request: Request) -> str:
    return (
        request.client.host
        if request.client
        else "unknown"
    )


def enforce_rate_limit(
    request: Request,
    bucket: str,
    limit: int,
    window: int,
) -> None:
    now = time.time()
    key = client_key(request)

    with lock:
        record = rate_limits.setdefault(
            key,
            {},
        )

        events = record.setdefault(
            bucket,
            [],
        )

        events[:] = [
            t
            for t in events
            if t > now - window
        ]

        if len(events) >= limit:
            raise HTTPException(
                429,
                "Rate limit reached. Please wait and try again later.",
            )

        events.append(now)


def clean_title(value: str | None) -> str:
    value = re.sub(
        r'[\\/:*?"<>|\x00-\x1f]+',
        "_",
        value or "video",
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    ).strip(" .")

    return value[:150] or "video"


def human_duration(
    seconds: Any,
) -> str | None:
    try:
        total = int(seconds)
    except (
        TypeError,
        ValueError,
    ):
        return None

    hours, remainder = divmod(
        total,
        3600,
    )

    minutes, seconds = divmod(
        remainder,
        60,
    )

    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"

    return f"{minutes}:{seconds:02d}"


def quality_format(
    quality: str,
) -> str:
    if quality in {
        "1080",
        "720",
        "480",
        "360",
    }:
        return (
            f"bestvideo[height<={quality}]"
            "+bestaudio/"
            f"best[height<={quality}]/best"
        )

    return "bestvideo+bestaudio/best"


def extractor_options(
    skip_download: bool = True,
) -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": skip_download,
        "socket_timeout": 25,
        "retries": 2,
        "fragment_retries": 2,
        "extractor_retries": 2,
        "impersonate": "chrome",
    }


def get_info(
    url: str,
) -> dict[str, Any]:
    logger.info(
        "Starting YouTube info extraction"
    )

    try:
        with yt_dlp.YoutubeDL(
            extractor_options()
        ) as ydl:
            result = ydl.extract_info(
                url,
                download=False,
            )

        logger.info(
            "YouTube info extraction succeeded"
        )

        return result

    except Exception as exc:
        logger.exception(
            "YouTube info extraction failed: "
            "type=%s repr=%r",
            type(exc).__name__,
            exc,
        )
        raise


def available_qualities(
    info: dict[str, Any],
) -> list[str]:
    heights = set()

    for fmt in info.get("formats") or []:
        try:
            height = int(
                fmt.get("height") or 0
            )
        except (
            TypeError,
            ValueError,
        ):
            continue

        if height:
            heights.add(height)

    result = [
        quality
        for quality in (
            "1080",
            "720",
            "480",
            "360",
        )
        if any(
            height >= int(quality)
            for height in heights
        )
    ]

    return result or ["360"]


def prune_jobs() -> None:
    now = time.time()

    with lock:
        for job_id, job in list(
            jobs.items()
        ):
            if (
                job.get("status")
                in {"complete", "error"}
                and now
                - job.get(
                    "updated_at",
                    now,
                )
                > JOB_TTL
            ):
                jobs.pop(
                    job_id,
                    None,
                )

                if job.get("file"):
                    shutil.rmtree(
                        Path(
                            job["file"]
                        ).parent,
                        ignore_errors=True,
                    )

       
