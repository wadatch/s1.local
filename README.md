# s1.local

我が家のサーバ **s1** の管理ポータル。

`http://s1.local/` を開けば、s1 で動いているものの一覧・死活・主要な数値が
1 画面で分かる。詳細は各サービスへ辿れる。

## いま出しているもの

| サービス | 内容 |
|---|---|
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
```

ローカルに Python は要らない。すべて Docker 上で動く。

## 構成

- **Caddy**（80 番）— 入口。ポータル本体へのプロキシと、既存サービスへのリダイレクト
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
