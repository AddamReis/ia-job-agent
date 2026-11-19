FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=America/Sao_Paulo

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata git libatomic1 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src
COPY config ./config
COPY scripts ./scripts

RUN pip install --no-cache-dir . \
    && python -m playwright install --with-deps chromium

VOLUME ["/app/data"]

CMD ["python", "-m", "src.cli", "discovery-serve"]
