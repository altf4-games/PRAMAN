FROM python:3.12-slim

WORKDIR /app

# libgdk-pixbuf2.0-0 was renamed to libgdk-pixbuf-2.0-0 upstream in Debian
# (caught by a real Railway build failure — its builder uses a newer Debian
# release than was current when this Dockerfile was first written, where
# the old name has no installation candidate at all).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY api ./api
COPY alembic.ini ./

RUN pip install --no-cache-dir -e .

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && uvicorn praman.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
