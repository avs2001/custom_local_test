FROM python:3.11-slim

WORKDIR /app

# SDK version is parameterized so this Dockerfile follows the SDK without edits.
# The customer bundles the matching tarball into the repo before `docker build`.
ARG SDK_VERSION=0.2.2

# Install pinned service deps (honors the customer's requirements.txt pins)
# plus the SDK wheel from the local tar.gz (Kubyk contract: wheel ships with the repo)
COPY requirements.txt .
COPY ledsas-sdk-production-v${SDK_VERSION}.tar.gz .
RUN tar -xzf ledsas-sdk-production-v${SDK_VERSION}.tar.gz && \
    pip install --no-cache-dir \
    -r requirements.txt \
    ledsas-sdk-production-v${SDK_VERSION}/1-sdk/kbm_ledsas_sdk-${SDK_VERSION}-py3-none-any.whl && \
    rm -rf ledsas-sdk-production-v${SDK_VERSION}.tar.gz ledsas-sdk-production-v${SDK_VERSION}

COPY main.py .

# Run as non-root user
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

CMD ["python", "main.py"]
