FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py storage.py mc.py tracker.py .

# Unbuffered so logs reach `docker logs` immediately instead of sitting
# in a buffer until the process exits.
ENV PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
