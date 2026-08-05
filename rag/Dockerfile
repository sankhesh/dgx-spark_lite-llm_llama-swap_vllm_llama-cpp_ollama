# rag-proxy: OpenAI-compatible RAG endpoint (see serve.py).
# Built locally by docker-compose (no registry push needed).
FROM python:3.12-slim

RUN pip install --no-cache-dir numpy requests

WORKDIR /app
COPY serve.py /app/serve.py

# The index is mounted at /index (read-only) at runtime, not baked in, so it can
# be rebuilt with index.py without rebuilding this image.
EXPOSE 8080
CMD ["python", "/app/serve.py"]
