# NetAgent: образ без зависимостей — только стандартная библиотека Python.
# Сборка:  docker build -t netagent .
# Запуск:   docker run -d -p 8000:8000 -v netagent-data:/app/data netagent
FROM python:3.12-slim

WORKDIR /app

COPY agent ./agent
COPY web ./web
COPY run_agent.py ./

ENV DB_PATH=/app/data/agent.db \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8000 \
    SCAN_INTERVAL_MINUTES=15

VOLUME ["/app/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/api/health')"

CMD ["python", "-m", "web.server"]
