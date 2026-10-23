FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TDL_DATA_DIR=/data \
    TDL_CONTAINER=1 \
    HOME=/tmp/tdl-home
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps . \
    && mkdir -p /data /tmp/tdl-home \
    && chmod 1777 /tmp/tdl-home
USER 1000:1000
VOLUME ["/data"]
ENTRYPOINT ["papa-tdl"]
CMD []
