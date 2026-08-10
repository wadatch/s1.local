SHELL := /bin/bash
DEPLOY_HOST ?= s1.local
DEPLOY_PATH ?= ~/s1.local

.PHONY: help up down restart logs ps check test deploy switchbot-devices

help:
	@echo "up      - 起動（ビルド込み）"
	@echo "down    - 停止"
	@echo "restart - 再起動"
	@echo "logs    - ログ追尾"
	@echo "ps      - コンテナ状態"
	@echo "check   - 設定ファイルの構文チェック"
	@echo "test    - ポータルのテスト（Docker 上で実行。ローカルに Python 不要）"
	@echo "deploy  - $(DEPLOY_HOST):$(DEPLOY_PATH) へ配備して起動"
	@echo "switchbot-devices - SwitchBot の登録デバイスと現在値を一覧表示"

up:
	docker compose up -d --build

down:
	docker compose down

restart:
	docker compose restart

logs:
	docker compose logs -f --tail=100

ps:
	docker compose ps

check:
	docker compose config --quiet
	docker run --rm -v "$(CURDIR)/caddy:/etc/caddy:ro" \
		-e TS_HOSTNAME=example.ts.net caddy:2.9-alpine caddy validate --config /etc/caddy/Caddyfile
	docker build -q -t s1-portal/portal:local ./portal >/dev/null
	docker run --rm -v "$(CURDIR)/config:/app/config:ro" s1-portal/portal:local \
		python -m app.registry --validate /app/config/services.yml

test:
	docker build -t s1-portal/portal:test --target test ./portal
	docker build -t s1-portal/switchbot-exporter:test --target test ./switchbot-exporter
	docker build -t s1-portal/speed-exporter:test --target test ./speed-exporter

# services.yml に書くデバイス名を調べるためのもの。
# 認証情報は s1 の .env にあるので、s1 側で実行する。
switchbot-devices:
	ssh $(DEPLOY_HOST) 'cd $(DEPLOY_PATH) && docker compose run --rm --no-deps \
		switchbot-exporter python exporter.py --list-devices'

deploy:
	rsync -av --delete \
		--exclude '.git' --exclude '.env' --exclude '__pycache__' \
		--exclude '.pytest_cache' --exclude '.claude' \
		./ $(DEPLOY_HOST):$(DEPLOY_PATH)/
	ssh $(DEPLOY_HOST) 'cd $(DEPLOY_PATH) && [ -f .env ] || cp .env.example .env'
	ssh $(DEPLOY_HOST) 'cd $(DEPLOY_PATH) && docker compose up -d --build'
	# Caddyfile はバインドマウントなので、書き換えてもコンテナは作り直されず、
	# Caddy 自身も読み直さない。配備しても反映されない事故になるので明示的に読ませる。
	ssh $(DEPLOY_HOST) 'cd $(DEPLOY_PATH) && docker compose exec -T caddy \
		caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile'
	ssh $(DEPLOY_HOST) 'cd $(DEPLOY_PATH) && docker compose ps'
