FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# kbm_ledsas_sdk is not on a public index — install it here before building,
# e.g. COPY ./kbm_ledsas_sdk*.whl . && pip install --no-cache-dir ./kbm_ledsas_sdk*.whl

COPY . .

CMD ["python", "processor1d.py"]
