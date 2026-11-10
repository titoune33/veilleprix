FROM python:3.12-slim

LABEL org.opencontainers.image.title="veilleprix"
LABEL org.opencontainers.image.description="Veille tarifaire auto-hébergée : relève, historise, alerte."
LABEL org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    VEILLEPRIX_DB=/data/veilleprix.db

WORKDIR /app

COPY pyproject.toml README.md LICENSE veilleprix.py ./
RUN pip install --no-cache-dir ".[extract]" && \
    mkdir -p /data

VOLUME ["/data"]

# Par défaut : relevé toutes les 15 minutes, bordereau à 8 h.
CMD ["sh", "-c", "\
  python veilleprix.py init && \
  while true; do \
    python veilleprix.py check; \
    hour=$(date +%H); \
    if [ \"$hour\" = \"08\" ]; then python veilleprix.py digest; fi; \
    sleep 900; \
  done"]
