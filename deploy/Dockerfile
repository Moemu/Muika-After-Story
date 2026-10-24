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

# 先复制元数据以充分利用 Docker 层缓存
COPY pyproject.toml uv.lock README.md alembic.ini ./
COPY muika/ ./muika/

# 按 uv.lock 安装 muika 包（Core 所需的 standard 与 Bot 所需的 nonebot 额外依赖）
RUN /usr/local/bin/pip install uv && \
    uv export --frozen --extra standard --extra nonebot --no-dev --no-hashes --no-emit-project -o /tmp/requirements.txt && \
    pip install --upgrade pip setuptools wheel && \
    pip install -r /tmp/requirements.txt ".[standard,nonebot]"

# 个人适配器等额外依赖，用锁文件约束版本，避免覆盖默认依赖
ARG BOT_EXTRA_DEPENDENCIES=""
RUN if [ -n "$BOT_EXTRA_DEPENDENCIES" ]; then \
    pip install -c /tmp/requirements.txt ${BOT_EXTRA_DEPENDENCIES}; fi


# ---- 运行阶段 ----
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 从构建阶段复制已安装的虚拟环境
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# 复制 Bot 应用代码（入口在容器内，Core/Gateway 由 Compose 覆盖命令）
COPY muika_bot/ ./muika_bot/
COPY bot.py ./

# 创建运行时数据目录
RUN mkdir -p /app/data /app/configs /app/plugins

EXPOSE 8080

CMD ["python", "bot.py"]
