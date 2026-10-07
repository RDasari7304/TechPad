# TechPad — production image. Playwright's base image ships Chromium + all system deps.
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && playwright install chromium

COPY . .
RUN mkdir -p /app/data

ENV LARPCHECK_HOST=0.0.0.0 \
    LARPCHECK_PORT=8000 \
    LARPCHECK_DB=/app/data/techpad.db \
    LARPCHECK_UPLOADS=/app/data/uploads \
    PYTHONUNBUFFERED=1

EXPOSE 8000
# "all" = web + API + X bot (bot only starts if X keys are set)
CMD ["python", "-m", "larpcheck", "all"]
