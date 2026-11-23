# syntax=docker/dockerfile:1
FROM python:3.12-slim

WORKDIR /workspace
COPY pyproject.toml README.md /workspace/
COPY src /workspace/src
RUN --mount=type=cache,target=/root/.cache/pip \
	python -m pip install .

EXPOSE 8501
CMD ["python", "-m", "streamlit", "run", "src/kaya_kopi/copilot/streamlit_app.py", "--server.address=0.0.0.0", "--server.port=8501"]
