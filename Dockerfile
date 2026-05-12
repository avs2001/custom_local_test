FROM python:3.11-slim

WORKDIR /app

# Copy and install SDK from local tar.gz
COPY ledsas-sdk-direct-v0.1.5-1.tar.gz .
RUN tar -xzf ledsas-sdk-direct-v0.1.5-1.tar.gz && \
    pip install --no-cache-dir \
    numpy \
    ledsas-sdk-direct-v0.1.5-1/1-sdk/kbm_ledsas_sdk-0.1.5-py3-none-any.whl && \
    rm -rf ledsas-sdk-direct-v0.1.5-1.tar.gz ledsas-sdk-direct-v0.1.5-1

COPY processor1d.py .

# Run as non-root user
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

CMD ["python", "processor1d.py"]
