FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
# Exported from uv.lock; runtime installs the same pinned dependency versions.
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir --no-deps . \
    && useradd --create-home --uid 10001 app \
    && mkdir /data && chown app:app /data
USER app
ENV ACADEMIC_DB=/data/academic.sqlite3
EXPOSE 8000
CMD ["python", "-m", "academic_mcp.server", "--transport", "streamable-http", "--host", "0.0.0.0"]
