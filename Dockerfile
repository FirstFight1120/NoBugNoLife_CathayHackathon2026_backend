# ========== STAGE 1: Build Dependencies ==========
FROM python:3.12-slim AS builder

# Install system dependencies required for PostgreSQL drivers
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev && \
    rm -rf /var/lib/apt/lists/*

# Copy the uv binary from the official image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Copy dependency definition files
COPY pyproject.toml uv.lock ./

# Install dependencies into the virtual environment (.venv)
RUN uv sync --frozen --no-install-project --no-dev

# ========== STAGE 2: Runtime Image ==========
FROM python:3.12-slim AS runner

WORKDIR /app

# Install runtime PostgreSQL library
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev && \
    rm -rf /var/lib/apt/lists/*

# Copy uv and the pre-built virtual environment from Stage 1
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
COPY --from=builder /app/.venv /app/.venv

# Copy your source code
COPY src/ ./src/

# Ensure the virtual environment is prioritized in the PATH
ENV PATH="/app/.venv/bin:$PATH"

# Expose the FastAPI port
EXPOSE 8000

# Start Uvicorn (0.0.0.0 is required for Docker to route traffic)
CMD ["uvicorn", "src.nbnl_backend.main:app", "--host", "0.0.0.0", "--port", "8000"]