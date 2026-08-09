FROM python:3.13-slim AS base

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY exporter.py .


# --- テスト段 -------------------------------------------------------------
FROM base AS test

COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY tests ./tests
RUN python -m pytest -q tests


# --- 本番段 ---------------------------------------------------------------
FROM base AS runtime

EXPOSE 9110
CMD ["python", "exporter.py"]
