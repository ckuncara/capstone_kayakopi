FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       default-mysql-client libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
COPY pyproject.toml README.md /workspace/
COPY src /workspace/src
RUN python -m pip install --no-cache-dir ".[ocr]"
