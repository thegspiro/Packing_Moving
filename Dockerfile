FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=5639 \
    BIND_ADDRESS=0.0.0.0 \
    DATA_DIR=/data \
    LOG_LEVEL=INFO

WORKDIR /app
COPY app/ /app/

# Unraid's standard nobody:users (99:100). Override with `user:` in compose or --user.
RUN mkdir -p /data && chown 99:100 /data
USER 99:100

VOLUME ["/data"]
EXPOSE 5639

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python3", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT', '5639'), timeout=4)"]

CMD ["python3", "/app/server.py"]
