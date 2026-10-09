# One image, two jobs. The command decides which:
#   worker (default):  python -m claimlens worker
#   API:               python -m claimlens serve --host 0.0.0.0 --port 8080
# Pin the base by digest in CI for reproducible builds (docker buildx imagetools inspect python:3.12-slim-bookworm).
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Tesseract for the local OCR path (the worker's cheap first read before escalating to Textract).
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY claimlens ./claimlens

# Run as an unprivileged user. The root filesystem is mounted read-only in ECS; only /tmp is writable.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER 10001

# Healthy = the worker touched its heartbeat file recently OR the API answers /healthz.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-m", "claimlens", "healthcheck"]

ENTRYPOINT ["python", "-m", "claimlens"]
CMD ["worker"]
