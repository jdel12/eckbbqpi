FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        bluez \
        libglib2.0-0 \
        dbus && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir bleak requests

COPY python/ python/

CMD ["python", "python/pybbq.py"]
