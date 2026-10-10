FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY pyproject.toml README.md alembic.ini ./
COPY muika/ ./muika/
RUN pip install --no-cache-dir '.[standard]'
RUN mkdir -p /app/data /app/configs /app/plugins
CMD ["python", "-m", "muika.ipc.bootstrap"]
