FROM kbmcontainerregistry.azurecr.io/base/kbm-ledsas-base-production:0.2.2

# Base image already provides: python:3.11-alpine3.20, kbm_ledsas_sdk 0.2.2
# (production-mode build), WORKDIR /app, and a non-root kbmuser. Switch to
# root only for the pip install of customer deps, then back to kbmuser.

USER root

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=kbmuser:kbmuser main.py .

USER kbmuser

CMD ["python", "main.py"]
