# Docling service for PDFT - CPU only (no GPU, no CUDA). Build context is the repo root (see docker-compose.yml).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models/hf
# Do NOT set DOCLING_ARTIFACTS_PATH: it disables auto-download and fails with "Model ... not found in artifacts_path".

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

# CPU-only PyTorch first, so pip does not pull the multi-GB CUDA build as a docling dependency.
RUN pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
COPY docling/requirements.txt .
RUN pip install -r requirements.txt

# Same converter module the app uses (single source of truth); the service imports it as a plain module.
COPY backend/app/parse/docling_convert.py ./docling_convert.py
COPY docling/server.py ./server.py

VOLUME ["/models"]
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
  CMD curl -fsS http://127.0.0.1:8765/health || exit 1

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8765"]
