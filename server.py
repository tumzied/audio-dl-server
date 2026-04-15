"""
YouTube Audio Streaming Server
================================
Endpoints:
  POST /auth/token              — get a JWT access token
  GET  /health                  — server + YouTube auth health check (public)
  POST /cookies                 — upload a Netscape cookies.txt (admin)
  GET  /cookies/status          — cookies file info + age warning (admin)
  GET  /search?q=...&limit=5    — search YouTube
  GET  /info?url=...            — video metadata (no download)
  GET  /stream?url=...&fmt=...  — download + convert audio (requires ffmpeg)
  GET  /stream/raw?url=...      — stream native audio, no conversion

Requirements:
  pip install fastapi uvicorn yt-dlp
  ffmpeg in PATH  (only needed for /stream with format conversion)

Run:
  uvicorn server:app --host 0.0.0.0 --port 8000 --reload
"""

import logging
import os
import time
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

import yt_dlp
from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.security import OAuth2PasswordRequestForm

from auth import USERS, create_access_token, require_auth, verify_password

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("audio-server")

app = FastAPI(title="YouTube Audio Streamer")

MIME_TYPES = {
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "opus": "audio/ogg",
    "wav": "audio/wav",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
}

# Path where cookies are stored. Override via COOKIES_FILE env var.
# Defaults to the named Docker volume so cookies survive container rebuilds.
COOKIES_FILE = os.getenv("COOKIES_FILE", "/app/cookies_store/cookies.txt")

# A well-known short public video used for health checks
_HEALTH_CHECK_VIDEO = "https://www.youtube.com/watch?v=jNQXAC9IVRw"  # "Me at the zoo" — first YouTube video, stable forever

# Warn in /cookies/status if cookies file is older than this many days
_COOKIE_WARN_AGE_DAYS = int(os.getenv("COOKIE_WARN_AGE_DAYS", "14"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ydl_opts_base() -> dict:
    """Base yt-dlp options, injecting cookies if the file exists."""
    opts: dict = {"quiet": True, "no_warnings": True}
    if Path(COOKIES_FILE).is_file():
        opts["cookiefile"] = COOKIES_FILE
    return opts


def _is_bot_detection_error(error: Exception) -> bool:
    """Return True when YouTube is blocking us due to missing/expired cookies."""
    msg = str(error).lower()
    return "sign in to confirm" in msg or "bot" in msg or "cookies" in msg


# Format selector used for all audio extraction.
# Fallback chain: best audio-only m4a → any audio-only → best combined → absolute best.
# This handles restricted videos, older uploads, and region-locked content gracefully.
_AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio/best"


def _fetch_info(url: str) -> dict:
    opts = {**_ydl_opts_base(), "format": _AUDIO_FORMAT}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _safe_filename(title: str) -> str:
    return "".join(c for c in title if c not in r'\/:*?"<>|').strip()


def _content_disposition(filename: str) -> str:
    """
    Build a Content-Disposition header that handles non-ASCII filenames (RFC 6266).
    Provides an ASCII fallback for old clients and a UTF-8 percent-encoded name for modern ones.
    """
    ascii_fallback = filename.encode("ascii", errors="ignore").decode("ascii").strip() or "audio"
    utf8_encoded = quote(filename, safe="")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{utf8_encoded}"


def _stream_chunks(path: str, chunk_size: int = 65536):
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            yield chunk


def _raise_for_yt_error(exc: Exception, context: str = "") -> None:
    """
    Convert yt-dlp exceptions into appropriate HTTP responses.
    Bot detection → 503 (service issue, not client's fault).
    Everything else → 400.
    """
    prefix = f"{context}: " if context else ""
    if _is_bot_detection_error(exc):
        raise HTTPException(
            status_code=503,
            detail=(
                f"{prefix}YouTube is requiring authentication. "
                "The server's cookies may be missing or expired. "
                "Please contact the administrator."
            ),
        )
    raise HTTPException(status_code=400, detail=f"{prefix}{exc}")


# Shorthand for the auth dependency
Auth = Annotated[dict, Depends(require_auth)]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/auth/token", tags=["auth"])
def login(form_data: Annotated[OAuth2PasswordRequestForm, Depends()]):
    """
    Obtain a JWT access token.
    Submit username + password as form data.
    """
    hashed = USERS.get(form_data.username)
    if not hashed or not verify_password(form_data.password, hashed):
        log.warning("Failed login attempt for user '%s'", form_data.username)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = create_access_token(subject=form_data.username)
    log.info("Token issued for user '%s'", form_data.username)
    return {"access_token": token, "token_type": "bearer"}


@app.get("/health", tags=["admin"])
def health_check():
    """
    Public endpoint — checks server status and YouTube reachability.
    The mobile app can call this on startup to detect expired cookies early.

    youtube_status values:
      "ok"       — authenticated and working
      "degraded" — bot detection / cookies missing or expired
      "error"    — unexpected failure
    """
    cookies_loaded = Path(COOKIES_FILE).is_file()

    # List every file in the cookies store directory
    cookies_dir = Path(COOKIES_FILE).parent
    cookies_files = []
    if cookies_dir.is_dir():
        for f in sorted(cookies_dir.iterdir()):
            if f.is_file():
                stat = f.stat()
                age_days = round((time.time() - stat.st_mtime) / 86400, 1)
                cookies_files.append({
                    "name": f.name,
                    "size_bytes": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                    "age_days": age_days,
                    "active": str(f) == COOKIES_FILE,
                })

    log.info("Health check — cookies_loaded=%s, files=%s", cookies_loaded, [f["name"] for f in cookies_files])

    try:
        opts = {**_ydl_opts_base(), "extract_flat": True, "skip_download": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(_HEALTH_CHECK_VIDEO, download=False)
        youtube_status = "ok"
        youtube_detail = None
        log.info("Health check — youtube_status=ok")
    except Exception as exc:
        if _is_bot_detection_error(exc):
            youtube_status = "degraded"
            youtube_detail = "YouTube authentication required — cookies missing or expired"
            log.warning("Health check — youtube_status=degraded: %s", exc)
        else:
            youtube_status = "error"
            youtube_detail = str(exc)
            log.error("Health check — youtube_status=error: %s", exc)

    return {
        "status": "ok" if youtube_status == "ok" else "degraded",
        "cookies_loaded": cookies_loaded,
        "cookies_store": {
            "path": str(cookies_dir),
            "active_file": COOKIES_FILE,
            "files": cookies_files,
        },
        "youtube_status": youtube_status,
        "youtube_detail": youtube_detail,
    }


@app.post("/cookies", tags=["admin"])
async def upload_cookies(auth: Auth, file: UploadFile = File(...)):
    """
    Upload a Netscape-format cookies.txt (admin only).
    Export from your browser with the 'Get cookies.txt LOCALLY' extension.
    No container restart needed — takes effect immediately.
    """
    content = await file.read()
    if not content.strip().startswith(b"# "):
        log.warning("Cookie upload rejected — file does not look like Netscape cookies.txt")
        raise HTTPException(
            status_code=400,
            detail="File does not look like a Netscape cookies.txt (expected '# Netscape HTTP Cookie File' header)",
        )
    Path(COOKIES_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(COOKIES_FILE).write_bytes(content)
    log.info("Cookies uploaded — %d bytes saved to %s", len(content), COOKIES_FILE)
    return {
        "detail": "Cookies saved. All subsequent requests will use them.",
        "size_bytes": len(content),
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/cookies/status", tags=["admin"])
def cookies_status(auth: Auth):
    """
    Returns cookies file info including age.
    Warns when the file is older than COOKIE_WARN_AGE_DAYS (default 14 days).
    YouTube cookies typically last weeks to a few months before expiring.
    """
    path = Path(COOKIES_FILE)
    if not path.is_file():
        return {
            "loaded": False,
            "warning": "No cookies file found. YouTube bot-detection may block requests.",
        }

    stat = path.stat()
    age_days = (time.time() - stat.st_mtime) / 86400
    uploaded_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()

    result = {
        "loaded": True,
        "size_bytes": stat.st_size,
        "uploaded_at": uploaded_at,
        "age_days": round(age_days, 1),
    }

    if age_days > _COOKIE_WARN_AGE_DAYS:
        result["warning"] = (
            f"Cookies are {round(age_days)} days old. "
            f"Consider re-uploading via POST /cookies to avoid expiry issues."
        )

    return result


@app.get("/search")
def search_videos(
    auth: Auth,
    q: str = Query(..., description="Search query"),
    limit: int = Query(5, ge=1, le=25, description="Number of results (1–25)"),
):
    """Search YouTube and return video suggestions."""
    log.info("Search — query=%r limit=%d", q, limit)
    opts = {
        **_ydl_opts_base(),
        "extract_flat": True,
        "skip_download": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            results = ydl.extract_info(f"ytsearch{limit}:{q}", download=False)
    except Exception as e:
        log.error("Search failed — query=%r error=%s", q, e)
        _raise_for_yt_error(e, "Search failed")

    entries = results.get("entries") or []
    log.info("Search — query=%r returned %d results", q, len(entries))
    return {
        "query": q,
        "results": [
            {
                "title": e.get("title"),
                "url": f"https://www.youtube.com/watch?v={e['id']}",
                "video_id": e.get("id"),
                "duration": e.get("duration"),
                "uploader": e.get("uploader") or e.get("channel"),
                "view_count": e.get("view_count"),
                "thumbnail": e.get("thumbnail"),
            }
            for e in entries
            if e.get("id")
        ],
    }


@app.get("/info")
def video_info(
    auth: Auth,
    url: str = Query(..., description="YouTube video URL"),
):
    """Return metadata for a YouTube video without downloading."""
    log.info("Info — url=%s", url)
    try:
        info = _fetch_info(url)
    except Exception as e:
        log.error("Info failed — url=%s error=%s", url, e)
        _raise_for_yt_error(e)
    log.info("Info — title=%r", info.get("title"))

    return {
        "title": info.get("title"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader"),
        "thumbnail": info.get("thumbnail"),
        "audio_formats": [
            {
                "format_id": f.get("format_id"),
                "ext": f.get("ext"),
                "abr": f.get("abr"),
                "acodec": f.get("acodec"),
                "filesize": f.get("filesize"),
            }
            for f in info.get("formats", [])
            if f.get("acodec") not in (None, "none")
        ],
    }


@app.get("/stream")
def stream_audio(
    auth: Auth,
    url: str = Query(..., description="YouTube video URL"),
    fmt: str = Query("mp3", description="Output format: mp3, m4a, opus, wav, flac, ogg"),
):
    """
    Download and stream audio converted to the requested format.
    Requires ffmpeg in PATH for format conversion.
    """
    if fmt not in MIME_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported format '{fmt}'. Choose from: {', '.join(MIME_TYPES)}",
        )

    log.info("Stream — url=%s fmt=%s", url, fmt)
    try:
        info = _fetch_info(url)
    except Exception as e:
        log.error("Stream fetch-info failed — url=%s error=%s", url, e)
        _raise_for_yt_error(e, "Could not fetch video info")

    title = _safe_filename(info.get("title", "audio"))
    filename = f"{title}.{fmt}"
    mime = MIME_TYPES[fmt]
    log.info("Stream — title=%r filename=%r", title, filename)

    def generate():
        t0 = time.monotonic()
        with tempfile.TemporaryDirectory() as tmpdir:
            output_template = os.path.join(tmpdir, "audio.%(ext)s")
            ydl_opts = {
                **_ydl_opts_base(),
                "format": _AUDIO_FORMAT,
                "outtmpl": output_template,
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": fmt,
                        "preferredquality": "192",
                    }
                ],
            }
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
            except Exception as e:
                log.error("Stream download failed — url=%s error=%s", url, e)
                return

            files = list(Path(tmpdir).iterdir())
            if not files:
                log.error("Stream — yt-dlp produced no output file for url=%s", url)
                return

            size = files[0].stat().st_size
            log.info("Stream — starting transfer title=%r size=%d bytes", title, size)
            yield from _stream_chunks(str(files[0]))
            log.info("Stream — done title=%r elapsed=%.1fs", title, time.monotonic() - t0)

    return StreamingResponse(
        generate(),
        media_type=mime,
        headers={"Content-Disposition": _content_disposition(filename)},
    )


@app.get("/stream/raw")
def stream_audio_raw(
    auth: Auth,
    url: str = Query(..., description="YouTube video URL"),
):
    """
    Stream the best available audio in its native format (no ffmpeg needed).
    Typically returns .webm/opus or .m4a depending on the video.
    """
    log.info("Stream/raw — url=%s", url)
    try:
        info = _fetch_info(url)
    except Exception as e:
        log.error("Stream/raw fetch-info failed — url=%s error=%s", url, e)
        _raise_for_yt_error(e, "Could not fetch video info")

    title = _safe_filename(info.get("title", "audio"))

    best_audio = next(
        (
            f for f in reversed(info.get("formats", []))
            if f.get("acodec") not in (None, "none")
            and f.get("vcodec") in (None, "none")
        ),
        None,
    )
    ext = best_audio.get("ext", "m4a") if best_audio else "m4a"
    mime = MIME_TYPES.get(ext, "application/octet-stream")
    filename = f"{title}.{ext}"
    log.info("Stream/raw — title=%r ext=%s", title, ext)

    def generate():
        t0 = time.monotonic()
        with tempfile.TemporaryDirectory() as tmpdir:
            output_template = os.path.join(tmpdir, "audio.%(ext)s")
            ydl_opts = {
                **_ydl_opts_base(),
                "format": _AUDIO_FORMAT,
                "outtmpl": output_template,
            }
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
            except Exception as e:
                log.error("Stream/raw download failed — url=%s error=%s", url, e)
                return

            files = list(Path(tmpdir).iterdir())
            if not files:
                log.error("Stream/raw — yt-dlp produced no output file for url=%s", url)
                return

            size = files[0].stat().st_size
            log.info("Stream/raw — starting transfer title=%r size=%d bytes", title, size)
            yield from _stream_chunks(str(files[0]))
            log.info("Stream/raw — done title=%r elapsed=%.1fs", title, time.monotonic() - t0)

    return StreamingResponse(
        generate(),
        media_type=mime,
        headers={"Content-Disposition": _content_disposition(filename)},
    )
