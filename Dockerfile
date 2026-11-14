FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends \
        cups-client poppler-utils fonts-dejavu-core curl \
    && rm -rf /var/lib/apt/lists/*
# fonts-ibm-plex is not in Debian trixie (2026-11-14): take IBM Plex Sans Bold from IBM's repo.
RUN mkdir -p /usr/share/fonts/truetype/ibm-plex \
    && curl -fsSL -o /usr/share/fonts/truetype/ibm-plex/IBMPlexSans-Bold.ttf \
       https://github.com/IBM/plex/raw/master/packages/plex-sans/fonts/complete/ttf/IBMPlexSans-Bold.ttf
RUN uv pip install --system --no-cache fastapi "uvicorn[standard]" pillow resvg-py pyyaml python-multipart
WORKDIR /app
COPY app/ /app/
ENV HUB_DATA=/data HUB_PORT=9190 PYTHONUNBUFFERED=1
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${HUB_PORT}"]
