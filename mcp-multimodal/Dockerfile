FROM python:3.12-slim AS builder

WORKDIR /build

RUN pip install --no-cache-dir uv

ENV UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

COPY pyproject.toml ./
RUN uv pip install --system --no-cache .

FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \
        libcups2 libdrm2 libxkbcommon0 libatspi2.0-0 libxcomposite1 \
        libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
        libcairo2 libasound2 libx11-xcb1 && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN playwright install chromium && \
    chmod -R o+rX /opt/ms-playwright

WORKDIR /app

COPY . .

RUN groupadd -r mcp && useradd -r -g mcp -d /app -s /sbin/nologin mcp && \
    mkdir -p /home/mcp && chown -R mcp:mcp /home/mcp /app

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

EXPOSE 9100

HEALTHCHECK --interval=15s --timeout=5s --retries=3 --start-period=20s \
    CMD curl -f http://127.0.0.1:9100/health || exit 1

USER mcp

CMD ["python", "-m", "mcp_multimodal.server"]
