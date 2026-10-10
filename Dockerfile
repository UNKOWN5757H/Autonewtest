# ================================
#   PYROGRAM TELEGRAM BOT IMAGE
#   Optimized for Speed & Stability
# ================================

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive \
    TZ=UTC

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    wget \
    ffmpeg \
    git \
    tzdata \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s CMD curl -f http://localhost:8080/ || exit 1

# Start the bot natively (bot.py handles both Telegram and the Web Healthcheck)
CMD ["python3", "bot.py"]
