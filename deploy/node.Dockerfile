FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 UV_LINK_MODE=copy
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock README.md ./
RUN pip install uv==0.7.11 && uv sync --frozen --extra standard --no-dev --no-install-project
COPY muika/ ./muika/
RUN uv sync --frozen --extra standard --no-dev
ENV PATH="/app/.venv/bin:$PATH"
WORKDIR /workspace
ENTRYPOINT ["python", "-m", "muika.node"]
CMD ["serve", "/data/server.json"]
