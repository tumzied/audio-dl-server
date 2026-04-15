FROM python:3.12-slim

# Install ffmpeg, curl, unzip (needed for Deno installer)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg curl unzip && \
    rm -rf /var/lib/apt/lists/*

# Install Deno — recommended JS runtime for yt-dlp EJS challenge solving (auto-detected by yt-dlp)
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    # Install yt-dlp-ejs Deno integration (registers Deno as the JS runtime for yt-dlp)
    pip install --no-cache-dir "yt-dlp[deno]"

COPY server.py auth.py ./

EXPOSE 8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
