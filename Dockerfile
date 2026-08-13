FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app app \
    && chown app:app /app

COPY --chown=app:app bot.py app.py ./
COPY --chown=app:app discord_bot ./discord_bot

USER app

CMD ["sh", "-c", "gunicorn -w 2 -k sync -b 0.0.0.0:${PORT} app:app & python bot.py"]
