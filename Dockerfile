FROM python:3.12-slim

# Python 3.12 is intentional: discord-ext-voice-recv still uses stdlib audioop,
# which was removed in Python 3.13.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libopus0 git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
CMD ["python", "-u", "bootstrap.py"]
