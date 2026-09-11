# Run the harness API anywhere (Docker / Pi / VM). The runtime is dependency-free
# stdlib Python, so there is nothing to pip install.
#
#   docker build -t dotobot -f deploy/Dockerfile .
#   docker run --rm -p 8765:8765 -v harness-data:/data dotobot
#
# Then point the macOS app at http://<host>:8765
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg xdotool scrot xclip xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

# Shared volume (bots' memory, skills, messages, control, streams) lives here so
# it survives container restarts when mounted.
ENV HARNESS_HOME=/data
ENV PYTHONUNBUFFERED=1
VOLUME ["/data"]

EXPOSE 8765

# --up starts the roster's bots; bind to all interfaces so the Mac can connect.
# No docker daemon exists inside this container, so the bots run as processes
# here — the machines backend (per-bot sandboxed computers, the host default)
# needs to run on a host with a daemon.
CMD ["python", "-m", "harness", "--backend", "process", "serve", "--up", "--host", "0.0.0.0", "--port", "8765"]
