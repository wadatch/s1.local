# s1.local

我が家のサーバ **s1** の管理ポータル。

開けば、s1 で動いているものの一覧・死活・主要な数値が 1 画面で分かる。
詳細は各サービスへ辿れる。

| 入口 | |
|---|---|
| `http://s1.local/` | 宅内 LAN から手軽に |
| `https://s1.tail981a3e.ts.net/` | 本物の Let's Encrypt 証明書つき。Tailscale 経由で宅内外とも |

`.local` は mDNS 用の予約 TLD で所有を証明できないため、公的 CA は証明書を
発行しない。HTTPS は Tailscale の MagicDNS 名で受けている。**端末側に証明書を
インストールする作業は要らない。** Tailscale に入っていない端末（来客の PC など）は
`http://s1.local/` を使う。

## いま出しているもの

| サービス | 内容 |
|---|---|
| 温湿度センサー | SwitchBot 27 台。代表的な数点をトップに、全台は `/sensors` で表に |
| ネットワーク監視 | 区間別のロス率・ゲートウェイ RTT・発報中アラート（→ Grafana） |
| Prometheus | 収集中の系列数・失敗中のターゲット（→ Prometheus） |
| s1 サーバ | CPU / メモリ / ディスク / 温度 / 連続稼働 |

## サービスを増やす

`config/services.yml` にエントリを 1 つ足すだけ。**再起動は不要**で、
ブラウザを再読み込みすれば増えている。書き方は [CLAUDE.md](CLAUDE.md) を参照。

```yaml
- id: my-tool
  name: 自作ツール
  description: 何のためのものか
  url: /my-tool
  category: tools
  health:
    type: http
    url: http://my-tool:8080/healthz
```

## 使い方

```bash
make check    # 設定の検証
make test     # テスト
make deploy   # s1.local へ配備して起動
make logs     # ログ
make switchbot-devices   # SwitchBot の登録デバイスと現在値を一覧表示
```

ローカルに Python は要らない。すべて Docker 上で動く。

## 構成

- **Caddy**（80 / 443 番）— 入口。ポータル本体へのプロキシと、既存サービスへのリダイレクト。HTTPS 証明書は tailscaled から受け取る
- **FastAPI**（Python）— 設定を読み、死活チェックとメトリクス取得を並列に行い、HTML を描画
- **node_exporter** — s1 自身の状態

同じ s1 上で別に動いている
[network-analyzer](https://github.com/wadatch/network-analyzer)（Prometheus + Grafana）とは
Docker ネットワーク越しに繋がっている。あちらの設定は一切変更していない。

## 設計の方針

詳細は [CLAUDE.md](CLAUDE.md)。要点は 3 つ。

1. **追加の摩擦を最小に保つ** — 設定ファイル 1 行で増える。増やしにくい一覧はいずれ実態とずれる
2. **障害時にこそ開ける** — 監視対象が全滅してもトップページは開く
3. **既存を壊さない** — `s1.local:3000` の直アクセスは従来どおり生きている
