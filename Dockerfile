# syntax=docker/dockerfile:1
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip wheel --no-deps --wheel-dir /wheels .

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_ENV=production \
    DATABASE_PATH=/app/data/profilabych.sqlite3
WORKDIR /app
COPY requirements.txt ./requirements.txt
COPY --from=builder /wheels /wheels
RUN python -m pip install --require-hashes -r requirements.txt \
    && python -m pip install --no-deps /wheels/*.whl \
    && rm -rf /wheels \
    && groupadd --gid 10001 profilabych \
    && useradd --uid 10001 --gid profilabych --no-create-home profilabych \
    && mkdir -p /app/data \
    && chown profilabych:profilabych /app/data

USER profilabych
CMD ["python", "-m", "profilabych"]
