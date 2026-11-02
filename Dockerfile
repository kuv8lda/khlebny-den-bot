FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
RUN groupadd --gid 10001 bot \
    && useradd --uid 10001 --gid bot --no-create-home bot \
    && mkdir /app/data \
    && chown bot:bot /app/data

COPY pyproject.toml README.md LICENSE ./
COPY khlebny_bot/ ./khlebny_bot/
RUN chmod -R a+rX /app/khlebny_bot && pip install .

USER 10001:10001
VOLUME ["/app/data"]
STOPSIGNAL SIGTERM
CMD ["python", "-m", "khlebny_bot.main"]
