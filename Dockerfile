# syntax=docker/dockerfile:1
# Alexandria: one process, one container, one volume.

FROM python:3.14-slim AS build
ENV PIP_NO_CACHE_DIR=1 POETRY_VIRTUALENVS_IN_PROJECT=1 POETRY_NO_INTERACTION=1
RUN pip install "poetry==2.1.4"
WORKDIR /app
COPY pyproject.toml poetry.lock README.md ./
RUN poetry install --only main --no-root
COPY src ./src
RUN poetry install --only main

FROM python:3.14-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 alexandria \
    && mkdir /data && chown alexandria:alexandria /data
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    ALEXANDRIA_DATA_DIR=/data
USER alexandria
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"
ENTRYPOINT ["alexandria"]
CMD ["serve"]
