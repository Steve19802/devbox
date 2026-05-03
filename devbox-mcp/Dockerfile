FROM python:3.11-slim

WORKDIR /app

# Install the Docker CLI (just the client, not the daemon)
RUN apt-get update && apt-get install -y docker.io && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the server logic (Immutable at runtime for security)
COPY server.py .

# Run the Uvicorn web server
CMD ["python", "server.py"]
