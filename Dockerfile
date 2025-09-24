# Use official Python image (3.11 or later)
FROM python:3.11-slim

# Set work directory
WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install solders

# Copy the rest of the code
COPY . .

# Expose port for FastAPI dashboard (if needed)
EXPOSE 8000

# Set environment variables (optional, can be overridden)
ENV PYTHONUNBUFFERED=1

# Start the FastAPI dashboard server
CMD ["sh", "-c", "uvicorn dashboard:app --host 0.0.0.0 --port ${PORT:-8000}"]
