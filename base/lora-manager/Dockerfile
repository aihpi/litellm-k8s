FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Runs as root so it can write to the adapters PVC, which inherits root
# ownership from the vLLM container that originally populates it. Acceptable
# because the service is ClusterIP-only and reachable only through LiteLLM's
# pass-through (already authenticated). Matches the existing aihpi tool-* image
# convention (kisz-auth-wrapper, tool-litellm).

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
