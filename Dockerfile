ARG PYTHON_IMAGE=python:3.12-slim

FROM ${PYTHON_IMAGE} AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

FROM base AS deps
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM base AS runtime
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 appuser
COPY --from=deps /install /usr/local
COPY src ./src
COPY sql ./sql
COPY frontend ./frontend
RUN mkdir -p /app/data/warehouse /app/data/raw && chown -R appuser:appuser /app
ARG APP_VERSION=1.0.0
ENV APP_VERSION=${APP_VERSION}
LABEL org.opencontainers.image.title="settlement-api" org.opencontainers.image.version="${APP_VERSION}"
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
