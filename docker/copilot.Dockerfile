# syntax=docker/dockerfile:1
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /workspace

# 1. Create an isolated virtual environment outside /workspace
RUN python -m venv $VIRTUAL_ENV

# 2. Install base dependencies first (cached unless pyproject.toml changes)
COPY pyproject.toml README.md ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip hatchling && \
    python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" > /tmp/reqs.txt && \
    pip install -r /tmp/reqs.txt

COPY src ./src
RUN pip install --no-deps --no-build-isolation .

#COPY pyproject.toml README.md /workspace/
#COPY src /workspace/src
#RUN --mount=type=cache,target=/root/.cache/pip \
#	python -m pip install .

EXPOSE 8501
CMD ["python", "-m", "streamlit", "run", "src/kaya_kopi/copilot/streamlit_app.py", "--server.address=0.0.0.0", "--server.port=8501", "--browser.gatherUsageStats=false"]
