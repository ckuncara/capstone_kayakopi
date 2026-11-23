FROM python:3.12-slim

WORKDIR /workspace
COPY pyproject.toml README.md /workspace/
COPY src /workspace/src
RUN python -m pip install --no-cache-dir ".[dev]"

CMD ["python", "-m", "pytest"]
