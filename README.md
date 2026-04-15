# audio-server

A FastAPI server that searches YouTube, extracts audio via [yt-dlp](https://github.com/yt-dlp/yt-dlp), and streams it to the client.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/auth/token` | Get a JWT access token |
| `GET` | `/search` | Search YouTube, returns video suggestions |
| `GET` | `/info` | Video metadata (no download) |
| `GET` | `/stream` | Download + convert audio (requires ffmpeg) |
| `GET` | `/stream/raw` | Stream native audio, no conversion needed |

All endpoints except `/auth/token` require authentication.

Interactive docs available at `http://localhost:8002/docs`.

---

## Quick Start (Docker)

**1. Configure environment**

```bash
cp .env.example .env
# Edit .env — set SECRET_KEY, API_KEYS, ADMIN_PASSWORD
```

**2. Build and run**

```bash
docker compose up --build -d
```

Server is available at `http://localhost:8002`.

---

## Local Development

**Requirements:** Python 3.12+, ffmpeg in PATH

```bash
pip install -r requirements.txt
cp .env.example .env

uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

---

## Authentication

Two schemes are supported — either one grants access.

### API Key

Pass a key from `API_KEYS` in the `X-API-Key` header:

```bash
curl -H "X-API-Key: my-secret-key-1" "http://localhost:8002/search?q=lofi"
```

### JWT Bearer

```bash
# 1. Get a token
curl -X POST http://localhost:8002/auth/token \
  -d "username=admin&password=changeme"

# 2. Use the token
curl -H "Authorization: Bearer <token>" "http://localhost:8002/search?q=lofi"
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SECRET_KEY` | *(weak default)* | JWT signing secret — **change in production** |
| `API_KEYS` | empty | Comma-separated static API keys |
| `ADMIN_USERNAME` | `admin` | Username for JWT login |
| `ADMIN_PASSWORD` | empty | Password for JWT login (disabled if empty) |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `60` | JWT token lifetime |

---

## Usage Examples

### Search

```bash
curl -H "X-API-Key: my-secret-key-1" \
  "http://localhost:8002/search?q=lofi+hip+hop&limit=5"
```

### Video Info

```bash
curl -H "X-API-Key: my-secret-key-1" \
  "http://localhost:8002/info?url=https://www.youtube.com/watch?v=VIDEO_ID"
```

### Stream (native format, no ffmpeg)

```bash
curl -OJ -H "X-API-Key: my-secret-key-1" \
  "http://localhost:8002/stream/raw?url=https://www.youtube.com/watch?v=VIDEO_ID"
```

### Stream as MP3 (requires ffmpeg)

```bash
curl -OJ -H "X-API-Key: my-secret-key-1" \
  "http://localhost:8002/stream?url=https://www.youtube.com/watch?v=VIDEO_ID&fmt=mp3"
```

Supported formats: `mp3`, `m4a`, `opus`, `wav`, `flac`, `ogg`.

---

## Project Structure

```
audio-server/
├── server.py          # FastAPI app and route handlers
├── auth.py            # Authentication (API key + JWT)
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example       # Environment variable template
└── .gitignore
```
