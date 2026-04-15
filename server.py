"""
YouTube Audio Streaming Server
================================
Endpoints:
  GET /search?q=...&limit=5   — search YouTube, return video suggestions
  GET /info?url=...           — video metadata (no download)
  GET /stream?url=...&fmt=... — download audio, convert to fmt via ffmpeg (requires ffmpeg)
  GET /stream/raw?url=...     — stream native audio format, no ffmpeg required

Requirements:
  pip install fastapi uvicorn yt-dlp
  ffmpeg in PATH  (only needed for /stream with format conversion)

Run:
  uvicorn server:app --host 0.0.0.0 --port 8000 --reload
"""

import os
import tempfile
from pathlib import Path
from typing import Annotated

import yt_dlp
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.security import OAuth2PasswordRequestForm

from auth import USERS, create_access_token, require_auth, verify_password

app = FastAPI(title="YouTube Audio Streamer")

MIME_TYPES = {
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "opus": "audio/ogg",
    "wav": "audio/wav",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fetch_info(url: str) -> dict:
    opts = {"quiet": True, "no_warnings": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _safe_filename(title: str) -> str:
    return "".join(c for c in title if c not in r'\/:*?"<>|').strip()


def _stream_chunks(path: str, chunk_size: int = 65536):
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            yield chunk


# Shorthand for the auth dependency used on every protected endpoint
Auth = Annotated[dict, Depends(require_auth)]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/auth/token", tags=["auth"])
def login(form_data: Annotated[OAuth2PasswordRequestForm, Depends()]):
    """
    Obtain a JWT access token.
    Submit `username` + `password` as form data (application/x-www-form-urlencoded).
    Use the returned token as `Authorization: Bearer <token>` on all other endpoints.
    """
    hashed = USERS.get(form_data.username)
    if not hashed or not verify_password(form_data.password, hashed):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = create_access_token(subject=form_data.username)
    return {"access_token": token, "token_type": "bearer"}


@app.get("/search")
def search_videos(
    auth: Auth,
    q: str = Query(..., description="Search query"),
    limit: int = Query(5, ge=1, le=25, description="Number of results (1–25)"),
):
    """
    Search YouTube and return video suggestions.
    Results include title, url, duration, uploader, view count, and thumbnail.
    """
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,   # don't fetch full info for each result
        "skip_download": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            results = ydl.extract_info(f"ytsearch{limit}:{q}", download=False)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    entries = results.get("entries") or []
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
    try:
        info = _fetch_info(url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

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

    try:
        info = _fetch_info(url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not fetch video info: {e}")

    title = _safe_filename(info.get("title", "audio"))
    filename = f"{title}.{fmt}"
    mime = MIME_TYPES[fmt]

    def generate():
        with tempfile.TemporaryDirectory() as tmpdir:
            output_template = os.path.join(tmpdir, "audio.%(ext)s")
            ydl_opts = {
                "format": "bestaudio/best",
                "outtmpl": output_template,
                "quiet": True,
                "no_warnings": True,
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
                print(f"[stream error] download failed: {e}")
                return

            files = list(Path(tmpdir).iterdir())
            if not files:
                print("[stream error] yt-dlp produced no output file")
                return

            yield from _stream_chunks(str(files[0]))

    return StreamingResponse(
        generate(),
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/stream/raw")
def stream_audio_raw(
    auth: Auth,
    url: str = Query(..., description="YouTube video URL"),
):
    """
    Stream the best available audio in its native format (no ffmpeg needed).
    The file extension and MIME type are determined by what YouTube provides
    (typically .webm/opus or .m4a).
    """
    try:
        info = _fetch_info(url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not fetch video info: {e}")

    title = _safe_filename(info.get("title", "audio"))

    def generate():
        with tempfile.TemporaryDirectory() as tmpdir:
            output_template = os.path.join(tmpdir, "audio.%(ext)s")
            ydl_opts = {
                "format": "bestaudio/best",
                "outtmpl": output_template,
                "quiet": True,
                "no_warnings": True,
            }
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
            except Exception as e:
                print(f"[stream/raw error] download failed: {e}")
                return

            files = list(Path(tmpdir).iterdir())
            if not files:
                print("[stream/raw error] yt-dlp produced no output file")
                return

            ext = files[0].suffix.lstrip(".")
            mime = MIME_TYPES.get(ext, "application/octet-stream")
            # We can't set response headers inside a generator, so we set
            # filename/mime before yielding — see the outer StreamingResponse.
            # Store for the closure:
            generate._ext = ext
            generate._mime = mime
            generate._filename = f"{title}.{ext}"

            yield from _stream_chunks(str(files[0]))

    # Run the generator once to get ext/mime (first call just initialises state)
    # Instead, we probe the format from info directly:
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

    return StreamingResponse(
        generate(),
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
