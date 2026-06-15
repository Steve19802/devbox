FROM docker:cli AS docker-cli

FROM python:3.11-slim 

WORKDIR /app

# Install the Docker CLI (just the client, not the daemon)
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the server logic (Immutable at runtime for security)
COPY server.py .

# Run the Uvicorn web server
CMD ["python", "server.py"]
