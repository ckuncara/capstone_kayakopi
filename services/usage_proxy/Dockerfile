FROM python:3.12-slim

WORKDIR /app
COPY services/usage_proxy/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/usage_proxy/ .

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
