FROM python:3.11-slim

WORKDIR /app

# Install curl for healthcheck + freetds for SQL Server connectivity
RUN apt-get update && apt-get install -y --no-install-recommends curl freetds-dev && rm -rf /var/lib/apt/lists/*

# Install dependencies first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY server.py .

# Default environment variables
ENV CHROMA_HOST=localhost
ENV CHROMA_PORT=8001

EXPOSE 8080

CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "8080"]
