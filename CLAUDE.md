# s1.local ホームポータル

我が家のサーバ s1 で動いているものを 1 画面に集約する入口。

| 入口 | 用途 |
|---|---|
| `http://s1.local/` | 宅内 LAN から手軽に |
| `https://s1.tail981a3e.ts.net/` | 本物の証明書つき。Tailscale 経由で宅内外とも |

## 設計の中心にあるもの

**サービスを追加する ＝ `config/services.yml` にエントリを 1 つ足すだけ。**

Python コードは触らない。コンテナの再起動もしない。

このポータルの価値は、増え続けるサービスを集約し「続けられる」ことにある。
追加のたびにコード改修が要る作りでは、遠からず更新されなくなって
実態とずれた一覧が残るだけになる。**追加の摩擦を最小に保つことが最優先。**

この性質は `portal/tests/test_app.py::test_サービスの追加が再起動なしで反映される`
で担保している。ここを壊す変更は入れない。

## 構成

```
[ブラウザ] --:80 / :443--> [portal-caddy] --> [portal-app] --> na-prometheus     （数値）
                                 |                        `-> portal-node-exporter（ホストの状態）
                                 `-- /grafana, /prometheus --> 302 で既存サービスへ
```

| コンテナ | 役割 |
|---|---|
| `portal-caddy` | 80 / 443 番の入口。Caddy |
| `portal-app` | FastAPI。死活チェックとメトリクス取得、HTML 描画 |
| `portal-node-exporter` | s1 自身の CPU / メモリ / ディスク / 温度 |
| `portal-switchbot-exporter` | SwitchBot の温湿度センサー |

`portal-app` は `network-analyzer_default` ネットワークにも属している。
これにより `na-prometheus` / `na-grafana` へ**コンテナ名で**到達でき、
network-analyzer 側の設定を一切変更せずに済んでいる。

## 触るときに知っておくこと

### サービスを追加する

`config/services.yml` に足すだけ。ファイルの更新時刻を見て自動で読み直す
（`registry.py` の `RegistryCache`）ので、**再起動は不要**。

s1 上で直接編集してもよいが、正はこのリポジトリ側にある。
`make deploy` で上書きされるので、リポジトリにも反映しておくこと。

```yaml
- id: 一意なID              # 必須。重複するとエラー表示になる
  name: 表示名               # 必須
  description: 何のためのものか。カードの説明文になる
  url: /somewhere           # 遷移先。数値だけ出したいなら null
  category: tools           # categories で定義したキー
  health:
    type: http              # http | tcp | always_up
    url: http://コンテナ名:ポート/healthz
  metrics:
    - label: 表示名
      source: prometheus    # prometheus | node_exporter | none
      endpoint: http://na-prometheus:9090
      query: 'PromQL'
      format: percent       # percent | seconds_ms | bytes | count | celsius | duration | raw
      thresholds: {warn: 0.01, crit: 0.05}   # 以上で黄 / 赤
```

**`health.url` はコンテナ名で書く。** ポータルはコンテナの中から見に行くので、
`localhost` や `s1.local` ではなく Docker ネットワーク上の名前を使う。
新しいサービスを別の compose で立てる場合は、そのネットワークを
`docker-compose.yml` に external で足す必要がある。

**`thresholds` は「大きいほど悪い」前提。** 空き容量のように
小さいほど悪い値は、PromQL 側で反転させて登録する。

**PromQL には `or vector(0)` を付けるか検討する。** 件数を数えるクエリは
0 件のときに空の結果になり、「取得失敗」と区別がつかなくなる。

追加したら `make check` で検証する。設定ミスは**そのサービスのカードだけ**が
エラー表示になり、他のサービスは巻き込まれない設計になっている。

### 新しいサービスはサブパス配信で設計する

Grafana と Prometheus だけは `reverse_proxy` ではなく `redir`（302）で繋いでいる。
これは意図した選択で、理由は `Caddyfile` のコメントに書いてある
（要約：真にプロキシするには別リポジトリ network-analyzer 側の変更が必要で、
かつ既存の `s1.local:3000` 直アクセスが壊れる）。

**これから s1 に立てるものは、最初からサブパス配信に対応させること。**
そうすれば `Caddyfile` に `handle_path /foo/* { reverse_proxy foo:8080 }` を
足すだけで、ポート番号を意識しない本物の統合になる。

### 障害時にこそ開けること

ポータルは「何かおかしい」と思ったときに最初に開く画面なので、
**監視対象が全滅していてもトップページは 200 で開かなければならない。**

そのために：
- メトリクス取得と死活チェックは例外を投げない。失敗は `None` になりカードが `—` になるだけ
- 設定の検証エラーはサービス単位で閉じ込める
- 死活チェックは全サービス並列（`asyncio.gather`）。1 つの遅いサービスが全体を待たせない

この 3 つはいずれもテストで担保している。

### SwitchBot は必ず exporter を挟む

**ポータルから SwitchBot API を直接叩いてはいけない。** API には
**1 日 10,000 回**の呼び出し上限があり、直接叩くとページを開くたび・
タブの数だけ消費が増えて、すぐ上限に達して**全センサーが取れなくなる**。

`portal-switchbot-exporter` がバックグラウンドで低頻度に取得してキャッシュし、
`/metrics` はそれを返すだけにしてある。これで API の消費が閲覧回数と無関係になる。
「スクレイプで API を呼ばないこと」はテストで固定している。

消費回数の目安:

```
1 日の消費回数 ≒ (センサー台数 + 1) × 86400 / SWITCHBOT_POLL_SECONDS
```

我が家は 32 台登録されていて、うち 5 台は値を返さないので除外している（27 台）。
600 秒間隔で 1 日約 4,000 回。**センサーを増やしたら間隔も見直すこと。**

#### 値を返さないセンサー

電池切れや圏外のセンサーに対し、SwitchBot API はエラーではなく
**温度・湿度・電池すべて 0** の状態を返す。そのまま通すと 0℃ が現在の室温として
表示される。実測で 32 台中 5 台がこの状態だった。

- 既知の 5 台は `config/switchbot.yml` で**取得対象から除外**している
  （問い合わせ自体を行わないので API の消費も減る）
- 新たに値を返さなくなったセンサーは `looks_offline()` が検出し、
  温度の系列から外したうえで `switchbot_device_offline` として見えるようにする。
  黙って消すと、センサーが死んだことに気づけない

除外リストは毎周期で読み直すので、**編集してもコンテナの再起動は不要**。

#### センサーは 32 台あるので一覧は別ページ

トップページのカードには代表的な数点だけを出し、全台は `/sensors` で表にする。
`/sensors` は exporter が公開している系列を**全部**拾うので、
**センサーを増やしても設定変更は要らない**（未対応づけのものは「未分類」に出る）。

代表を入れ替えたいときは `config/services.yml` の `device_name` を書き換える。

#### ホームとルームは API から取れない

**SwitchBot API v1.1 はホームもルームも返さない。** 返るのは `deviceId` /
`deviceName` / `deviceType` / `hubDeviceId` / `group` 系だけ。
`hubDeviceId` も多くのデバイスで `000000000000` になるので、
ハブからの推定も当てにならない。

そのため対応づけは `config/switchbot.yml` の `homes` と `devices` で持つ。
`homes` に書いた順がそのまま `/sensors` の表示順になる。

```yaml
homes: [南棟, 北棟, 苗場]
devices:
  B0E9FE53353E: {home: 南棟, room: リビング}
```

名前に「北棟」「南棟」が入っているものと苗場のハブ経由のものは確実だが、
それ以外（リビング・ダイニング・寝室など）は**消去法で南棟と推定**している。
違っていればこの対応づけを直す。

#### 取得 ON/OFF

`/sensors` のトグルが `config/switchbot.yml` の `exclude` を書き換える。
センサー 1 台ずつと、ホームごとにまとめて、の 2 通り。

**書き戻すときは必ず文書全体を読み直し、`homes` と `devices` を持ち越すこと。**
ここを取りこぼすと、トグルを 1 回押しただけで 32 台ぶんの対応づけが全部消える。
`test_トグルしてもホームと所属が消えない` で固定している。

**書き戻しでは元のファイルの所有者とパーミッションも引き継ぐこと。**
ポータルのコンテナは root で動くので、これをやらないと `root:root 0600` になり、
**開発機からの rsync 配備が失敗してホスト上で手編集もできなくなる**。
実際に一度そうなった。

OFF はすぐ効く（exporter が 15 秒ごとに設定を見て、その場で値を捨てる）。
ON に戻したときは次の取得（最大 10 分後）で値が入る。
取得そのものを 15 秒間隔にはできない（API の回数制限）が、
ON/OFF は画面操作なので待たされると壊れて見える。この非対称は意図したもの。

### CPU 使用率だけ挙動が違う

`node_cpu_seconds_total` は累積値なので、1 回のスクレイプでは使用率を出せない。
前回の値を `metrics.py` の `_cpu_samples` に覚えておいて差分から算出している。
**そのため起動直後の初回だけ「計測中」になり値が出ない。** これは正常。

## コマンド

```bash
make check    # Caddyfile と services.yml の検証
make test     # ポータルのテスト（Docker 上で走るのでローカルに Python 不要）
make deploy   # s1.local へ rsync して起動
make logs     # ログ追尾
```

`make test` はマルチステージビルドの test 段を構築する。
ビルドが通ること自体がテスト成功を意味する。

## 配備

`ssh s1.local` の `~/s1.local`。`make deploy` が rsync と `docker compose up -d --build`
までやる。ビルドは s1 上で走るので、開発機（arm64）と s1（x86_64）の
アーキテクチャ差を気にしなくてよい。

`.env` は rsync の対象外。初回だけ `.env.example` からコピーされる。
`BIND_ADDR` は `0.0.0.0` でないと別マシンから見られない。

### HTTPS は Tailscale の名前でだけ張っている

**`s1.local` には公的に信頼される証明書を発行できない。** `.local` は mDNS 用の
予約 TLD で、所有を証明する手段が無いため公的 CA は発行しない。これは設定の
問題ではなく原理的な制約なので、`s1.local` を HTTPS にする道は
「自己署名 + 全端末にルート CA を手動インストール」しかない。

代わりに Tailscale の MagicDNS 名 `s1.tail981a3e.ts.net` に対して
**本物の Let's Encrypt 証明書**を張っている。証明書は Caddy が tailscaled の
LocalAPI（`/var/run/tailscale/tailscaled.sock`）から受け取る。ACME は使わない。
更新も Caddy と tailscaled が自動でやる。**端末側の作業はゼロ。**

そのため `docker-compose.yml` で以下を渡している。どちらも消すと HTTPS が壊れる。

- `/var/run/tailscale:/var/run/tailscale:ro`（証明書の受け取り口）
- `TS_HOSTNAME`（HTTPS を張る名前。`tailscale status --json` の `CertDomains`）

証明書の発行には root 権限が要る。Caddy コンテナは root で動くので通っている。

**Tailscale に入っていない端末からは ts.net 名を使えない。** 来客の PC や
家族の未参加端末は `http://s1.local/` を使うことになる。両方の入口を
残してあるのはこのため。

**`/grafana` への 1 区間だけは HTTP のまま。** Grafana 自身が平文 HTTP で
動いているため。ただし ts.net 経由なら Tailscale が WireGuard で暗号化するので、
通信路が平文になるわけではない。ブラウザの鍵マークは外れる。

## やっていないこと

- **`s1.local` の HTTPS 化**：上記のとおり全端末へのルート CA インストールが
  必要になる。家族の端末が増えるたびに再作業が発生するので割に合わない
- **ポータルの認証**：出しているのは死活とサマリ数値のみ。Grafana 側は従来どおりログインが要る
- **ホスト指標の履歴**：node_exporter を直接読んで現在値だけを出している。
  履歴が要るなら Prometheus に scrape job を足し、`services.yml` の
  `source` を `prometheus` に書き換える（コードの変更は不要）
