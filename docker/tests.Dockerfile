# syntax=docker/dockerfile:1
FROM python:3.12-slim

WORKDIR /workspace
COPY pyproject.toml README.md /workspace/
COPY src /workspace/src
RUN --mount=type=cache,target=/root/.cache/pip \
	python -m pip install ".[dev]"

CMD ["python", "-m", "pytest"]
