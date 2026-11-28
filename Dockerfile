# Base pinned by immutable index digest (FUNK-60 release provenance). Refresh
# deliberately: resolve the new digest, update it here, release.
ARG BASE_IMAGE=python:3.12-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b
FROM ${BASE_IMAGE}
ARG BASE_IMAGE

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

# Python deps: exact versions + hashes from the lock (pip-compile --generate-hashes
# from requirements.in); any unlisted or altered distribution fails the build.
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes --no-deps -r requirements.lock

# Build provenance for the release-set manifest: base image, Python, the exact
# pip and dpkg package sets of this platform's image.
RUN python3 -c "import json,os,subprocess,sys; \
pip=subprocess.check_output([sys.executable,'-m','pip','freeze','--all'],text=True).split(); \
dpkg=subprocess.check_output(['dpkg-query','-W','-f=\${Package}=\${Version}\n'],text=True).split(); \
json.dump({'base_image':os.environ.get('BASE_IMAGE'),'python':sys.version.split()[0],'pip':pip,'dpkg':dpkg}, \
open('/app/build-provenance.json','w'),indent=1)"

# App code
COPY atomicwrite.py keyboard.py keymap.py profiles.py progress.py quiz.py setbundle.py display.py server.py version.py versioning.py versioning_bundle.py releasehistory.py version.json ./
COPY release-history/ ./release-history/
COPY static/ ./static/
COPY settings.json /app/defaults/settings.json
# Source evidence from the build gate (python3 scripts/source_manifest.py). Mandatory:
# without it the build fails; with it, the bundle must equal the git-tracked set.
COPY .source-manifest.json ./

# INSPR CalVer3 (FUNK-60): every build verifies the vendored presentation bundle
# against its pins and the source manifest, and the authoritative version record.
RUN python3 versioning_bundle.py --require-source-manifest \
    && python3 -c "import version; print('version', version.VERSION, version.VERSION_SCHEME)"

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
