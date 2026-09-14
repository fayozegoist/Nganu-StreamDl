FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data/uploads

EXPOSE 8000

CMD ["sh", "-c", "echo STARTING with PORT=$PORT && (gunicorn --worker-class gthread --threads 4 --timeout 300 --access-logfile - --error-logfile - -b 0.0.0.0:${PORT:-8000} app:app &) && exec python -u wrapper.py"]