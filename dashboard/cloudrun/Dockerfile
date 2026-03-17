FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# D-00: Single source of truth — frontend/index.html is the canonical dashboard.
# Built during deploy, so cloudrun/static/index.html is a generated artifact.
# The deploy script (deploy_cloudrun.py) uses --source <DASHBOARD_DIR>, so the
# build context is dashboard/cloudrun/. We need to reference the sibling dir.
# Cloud Build will COPY the whole repo context, so use relative paths.
COPY static/ ./static/

# Serve on Cloud Run's expected PORT (default 8080)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
