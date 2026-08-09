# --- 共通のベース ---------------------------------------------------------
FROM python:3.13-slim AS base

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app


# --- テスト段 -------------------------------------------------------------
# `make test` は --target test でここを構築する。ビルドが通ること自体が
# テストの成功を意味するので、ローカルに Python 環境が要らない。
FROM base AS test

COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY tests ./tests
COPY pytest.ini .
RUN python -m pytest -q


# --- 本番段 ---------------------------------------------------------------
FROM base AS runtime

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--no-access-log"]
