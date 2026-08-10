# --- コードと依存だけの段 -------------------------------------------------
# ここには speedtest の CLI を入れない。テストは実行部分を差し替えるので
# CLI を必要とせず、開発機（arm64）でも `make test` が通るようにするため。
# CLI は x86_64 のバイナリなので、入れると arm64 でビルドできなくなる。
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


# --- Ookla の公式 CLI ------------------------------------------------------
# network-analyzer でも使っている speedtest-exporter のイメージから取り出す。
# 静的リンクなので slim へそのまま移せる（下の --version で確かめている）。
FROM miguelndecarvalho/speedtest-exporter:v3.5.4 AS cli


# --- 本番段 ---------------------------------------------------------------
FROM base AS runtime

COPY --from=cli /usr/local/bin/speedtest /usr/local/bin/speedtest
RUN speedtest --version > /dev/null

EXPOSE 9800
CMD ["python", "exporter.py"]
