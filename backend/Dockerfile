# Stage 1: build the new UI (web/) into static files. Node is only used here; the running
# Core image has no Node and the browser loads nothing from outside the appliance.
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# Stage 2: Core.
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends iputils-ping iproute2 \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY backend/app /app/app
COPY agents /agents
COPY frontend /frontend
COPY --from=web /web/dist /web-dist

ENV PYTHONPATH=/app:/agents
ENV FRONTEND_DIR=/frontend
ENV WEB_DIST=/web-dist

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
