# Образ Playwright уже содержит Chromium и все системные библиотеки для него
FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Moscow

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
RUN mkdir -p /app/data/media && chown -R pwuser:pwuser /app/data
USER pwuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:8000' + os.getenv('BASE_PATH', '/publisher') + '/health', timeout=4)"

# Ровно один процесс: в нём живёт планировщик автопарсинга (несколько воркеров запустили бы его несколько раз)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
