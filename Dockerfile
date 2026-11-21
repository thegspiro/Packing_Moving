FROM python:3.13-slim

LABEL org.opencontainers.image.title="Packing Tracker" \
      org.opencontainers.image.description="Track moving boxes and print labels with QR codes" \
      org.opencontainers.image.source="https://github.com/thegspiro/Packing_Moving"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=5639 \
    BIND_ADDRESS=0.0.0.0 \
    DATA_DIR=/data \
    LOG_LEVEL=INFO

WORKDIR /app
COPY app/ /app/

# The server runs as Unraid's standard nobody:users (99:100). Started as root
# (as an Unraid template does), it gives /data to PUID:PGID and drops to that
# user before doing anything else. Started with --user / compose `user:`, it
# just runs as that user.
ENV PUID=99 \
    PGID=100
RUN mkdir -p /data && chown 99:100 /data

VOLUME ["/data"]
EXPOSE 5639

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python3", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT', '5639'), timeout=4)"]

CMD ["python3", "/app/server.py"]
