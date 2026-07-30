# ---- 构建阶段 ----
FROM python:3.12-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 安装最小化构建工具（某些 wheels 需要编译）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 创建虚拟环境
RUN python -m venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# 先复制 pyproject.toml 以充分利用 Docker 层缓存
COPY pyproject.toml README.md ./
COPY muika/ ./muika/

# 安装 muika 包（含 nonebot 额外依赖）
RUN pip install --upgrade pip setuptools wheel && \
    pip install ".[nonebot]"


# ---- 运行阶段 ----
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 从构建阶段复制已安装的虚拟环境
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# 复制 Bot 应用代码
COPY muika_bot/ ./muika_bot/
COPY deploy/docker-entrypoint.sh ./
COPY bot.py ./

# 创建运行时数据目录，赋予执行权限
RUN chmod +x docker-entrypoint.sh && mkdir -p /app/data

EXPOSE 8080

CMD ["sh", "docker-entrypoint.sh"]
