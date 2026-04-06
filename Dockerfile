FROM python:3.11-slim

WORKDIR /app

# Install curl for healthcheck + freetds for SQL Server connectivity
RUN apt-get update && apt-get install -y --no-install-recommends curl freetds-dev && rm -rf /var/lib/apt/lists/*

# Install dependencies first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/ src/
COPY server.py .

# Default environment variables
ENV CHROMA_HOST=localhost
ENV CHROMA_PORT=8001
ENV PYTHONPATH=/app/src
# Lock container to UTC so all server-generated timestamps are unambiguous.
# All datetime.now() calls in code use timezone.utc, but this guards against
# any host TZ leakage and makes container logs match stored timestamps.
ENV TZ=UTC

EXPOSE 8080

CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "8080"]
