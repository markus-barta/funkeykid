FROM python:3.12-slim

WORKDIR /app

# System deps for evdev build + audio + BT diagnostics + ffmpeg (loudnorm for FKID-1)
RUN apt-get update && apt-get install -y --no-install-recommends \
    pulseaudio-utils \
    bluez \
    dbus \
    ffmpeg \
    gcc \
    linux-libc-dev \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY pyproject.toml .
RUN pip install --no-cache-dir evdev paho-mqtt requests aiohttp Pillow

# App code
COPY keyboard.py display.py server.py version.py versioning.py versioning_bundle.py version.json ./
COPY static/ ./static/
COPY settings.json /app/defaults/settings.json

# INSPR CalVer3 (FUNK-60): every build verifies the vendored presentation bundle
# against its pins and the authoritative version record; any mismatch fails the build.
RUN python3 versioning_bundle.py && python3 -c "import version; print('version', version.VERSION, version.VERSION_SCHEME)"

# Data dirs (overridden by volume mounts)
RUN mkdir -p /data/sounds /data/images

ENV FUNKEYKID_DATA=/data
ENV FUNKEYKID_PORT=8081
ARG BUILD_SHA=dev
ARG BUILD_TIME=unknown
ENV FUNKEYKID_BUILD=$BUILD_SHA
RUN echo "$BUILD_TIME" > /app/.build_time

EXPOSE 8081

CMD ["python3", "server.py"]
