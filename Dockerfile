FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Match Railway's configured public target port unless Railway provides PORT.
ENV PORT=8080

# Install system dependencies
RUN apt-get update && apt-get install -y \
    libgl1 libsm6 libglib2.0-0 \
    libgomp1 \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create directories
RUN mkdir -p logs uploads models

# Expose port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://localhost:' + os.environ.get('PORT', '8080') + '/api/health')"

# Run application
CMD ["python", "app.py"]
