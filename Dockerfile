# ---- build the React UI -----------------------------------------------------------------
FROM node:22-alpine AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend ./
RUN npm run build

# ---- runtime: FastAPI serves the API and the built UI from one container -------------------
FROM python:3.12-slim AS app
ARG GIT_SHA=dev
ARG BUILD_TIME=unknown
LABEL org.opencontainers.image.title="clinic-voice-agent" \
      org.opencontainers.image.description="Rules-based, bilingual (English/Urdu) clinic voice assistant" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.created="${BUILD_TIME}"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 APP_ENV=prod USE_MIGRATIONS=true PORT=8000 \
    GIT_SHA=${GIT_SHA} BUILD_TIME=${BUILD_TIME}
WORKDIR /app
RUN useradd --system --uid 10001 --no-create-home app
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
COPY backend/config ./config
COPY backend/scenarios ./scenarios
COPY backend/alembic.ini ./alembic.ini
COPY backend/migrations ./migrations
COPY --from=ui /ui/dist ./static
RUN mkdir -p /app/data && chown -R app:app /app/data
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8000'), timeout=2)"
# Client addresses come from X-Forwarded-For only as far as TRUSTED_PROXY_COUNT says (see app/api/deps.py),
# so uvicorn's own proxy-header rewriting is deliberately off. WEB_CONCURRENCY sets the worker count.
CMD ["sh", "-c", "exec uvicorn app.asgi:app --host 0.0.0.0 --port ${PORT}"]
