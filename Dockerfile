# Stage 1: Build stage
FROM python:3.11-slim AS builder

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install uv via pip
RUN pip install --no-cache-dir uv

# Set working directory
WORKDIR /app

# Enable bytecode compilation
ENV UV_COMPILE_BYTECODE=1

# Copy only the files needed for dependency installation
COPY pyproject.toml uv.lock ./

# Install dependencies into a virtualenv
RUN uv sync --frozen --no-dev --no-install-project

# Stage 2: Final run stage
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Create log directory
RUN mkdir -p /app/logs

# Copy the virtual environment from the builder stage
COPY --from=builder /app/.venv /app/.venv

# Place the virtual environment's executable on the PATH
ENV PATH="/app/.venv/bin:$PATH"

# Set Python environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Copy the source code and entrypoint
COPY src/ /app/src/
COPY main.py /app/main.py

# Expose port for Webview only (other components communicate internally via IPC Pipe)
EXPOSE 8000

# Run Master process in foreground to manage all services
CMD ["python", "src/main.py", "--foreground"]
