FROM python:3.12-slim

WORKDIR /app

# System deps for evdev build + audio
RUN apt-get update && apt-get install -y --no-install-recommends \
    pulseaudio-utils \
    gcc \
    linux-libc-dev \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY pyproject.toml .
RUN pip install --no-cache-dir evdev paho-mqtt requests aiohttp

# App code
COPY keyboard.py display.py server.py ./
COPY static/ ./static/
COPY settings.json /app/defaults/settings.json

# Data dirs (overridden by volume mounts)
RUN mkdir -p /data/sounds /data/images

ENV FUNKEYKID_DATA=/data
ENV FUNKEYKID_PORT=8081

EXPOSE 8081

CMD ["python3", "server.py"]
