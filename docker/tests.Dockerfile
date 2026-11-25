# syntax=docker/dockerfile:1
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /workspace
# COPY pyproject.toml README.md /workspace/
# COPY src /workspace/src
# RUN --mount=type=cache,target=/root/.cache/pip \
# 	python -m pip install ".[dev]"

RUN python -m venv $VIRTUAL_ENV

COPY pyproject.toml README.md ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip hatchling && \
    python -c "import tomllib; d = tomllib.load(open('pyproject.toml', 'rb'))['project']; print('\n'.join(d['dependencies'] + d['optional-dependencies']['dev']))" > /tmp/reqs.txt && \
    pip install -r /tmp/reqs.txt

COPY src ./src
COPY tests ./tests
RUN pip install --no-deps --no-build-isolation .

# CMD ["python", "-m", "pytest"]
CMD ["python", "-m", "pytest", "-p", "no:cacheprovider"]
