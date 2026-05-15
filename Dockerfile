FROM python:3.11-slim

WORKDIR /app/src

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /app/
RUN python -m pip install --upgrade pip setuptools wheel
RUN python -m pip install uv
RUN uv --directory /app sync

COPY src/ /app/src/

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
