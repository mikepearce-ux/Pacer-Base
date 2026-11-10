FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY pacer_base ./pacer_base
RUN pip install . && useradd --create-home --uid 10001 app

USER app
EXPOSE 8000
# Railway sets PORT; `serve` reads it.
CMD ["pacer-base", "serve"]
