FROM python:3.12-slim

# System deps (minimal; Vosk has wheels)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Expose web port
EXPOSE 8000

# Default envs (override at run)
ENV MODEL_PATH=/models/vosk
ENV ROSTER="Caden,Eli,JP,Rudy,Dylan,Mario,Adam,Pow,Leo,Nate,Shane,Jake,Brandon,Justice,Kale,Martin,Walker,Tucker,Beemer,Big Jake,Christian"
ENV DATA_DIR=/data

CMD ["python", "app.py"]
