FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y curl

RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y nodejs

RUN pip install uv

ENV UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

COPY pyproject.toml uv.lock ./
RUN uv pip install --system --no-cache .

COPY . .

RUN chmod +x run.sh

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["./run.sh"]
