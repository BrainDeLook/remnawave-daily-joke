FROM ollama/ollama:0.40.15 AS ollama
FROM python:3.13-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libgomp1 tzdata \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ollama /bin/ollama /bin/ollama
COPY --from=ollama /usr/lib/ollama /usr/lib/ollama
WORKDIR /app
COPY app.py /app/app.py
ENV PYTHONUNBUFFERED=1
CMD ["python", "/app/app.py"]
