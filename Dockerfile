FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install playwright \
    && playwright install --with-deps chromium
COPY . .
ENV PORT=8080 \
    THREADS_USE_NO_SANDBOX=1 \
    THREADS_BROWSER_ARGS="--headless=new --no-sandbox --disable-dev-shm-usage"
CMD ["sh", "-c", "gunicorn -w 2 -k sync -b 0.0.0.0:${PORT} app:app & python bot.py"]
