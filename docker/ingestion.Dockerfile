# syntax=docker/dockerfile:1
FROM python:3.13-slim

# RUN apt-get update \
#     && apt-get install -y --no-install-recommends \
#        default-mysql-client libgl1 libglib2.0-0 libgomp1 \
#     && rm -rf /var/lib/apt/lists/*

# Install MySQL client for database interactions and required dependencies, excluding unnecessary packages
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       default-mysql-client \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /workspace
# COPY pyproject.toml README.md /workspace/
# COPY src /workspace/src
# RUN --mount=type=cache,target=/root/.cache/pip \
#     python -m pip install ".[ocr]"

RUN python -m venv $VIRTUAL_ENV

COPY pyproject.toml README.md ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip hatchling && \
    python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" > /tmp/reqs.txt && \
    pip install -r /tmp/reqs.txt

COPY src ./src
RUN pip install --no-deps --no-build-isolation .