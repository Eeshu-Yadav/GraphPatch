FROM python:3.12-slim

WORKDIR /app

# System deps for GitPython + tree-sitter compilation
RUN apt-get update && apt-get install -y \
    git \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir -e .

COPY . .

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
