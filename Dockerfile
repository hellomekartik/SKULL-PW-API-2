FROM python:3.11-slim

WORKDIR /app

# curl_cffi pre-built wheels don't need extra system deps on slim
# but gcc helps if wheel not found for the platform
RUN apt-get update && apt-get install -y --no-install-recommends gcc && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 7860

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
