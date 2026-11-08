FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PDFT_DATA_DIR=/data \
    PDFT_CONFIG_DIR=/config

# Static fallback fonts (Korean + Latin). ttc/otc collections are intentionally not used.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates fonts-nanum fonts-liberation2 fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt .
RUN pip install -r requirements.txt

# Default fonts: Noto *variable* fonts, so the weight slider is continuous (100-900).
# A failed download is not fatal - the apt fonts above stay available.
RUN mkdir -p /app/fonts && cd /app/fonts \
 && base="https://github.com/google/fonts/raw/main/ofl" \
 && for item in \
      "notosanskr/NotoSansKR%5Bwght%5D.ttf NotoSansKR-VF.ttf" \
      "notoserifkr/NotoSerifKR%5Bwght%5D.ttf NotoSerifKR-VF.ttf" \
      "notosansjp/NotoSansJP%5Bwght%5D.ttf NotoSansJP-VF.ttf" \
      "notosanssc/NotoSansSC%5Bwght%5D.ttf NotoSansSC-VF.ttf" \
      "notosans/NotoSans%5Bwdth%2Cwght%5D.ttf NotoSans-VF.ttf" ; do \
      set -- $item; \
      curl -fsSL --retry 3 -o "$2" "$base/$1" && [ "$(wc -c < "$2")" -gt 500000 ] \
        || { echo "WARN: could not download $1"; rm -f "$2"; }; \
    done \
 && ls -la /app/fonts

COPY backend/app ./app
COPY frontend ./frontend
COPY backend/models.example.json ./models.example.json

VOLUME ["/data", "/config"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"

# One worker on purpose: translation jobs run as in-process tasks.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
