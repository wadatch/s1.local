# アイコン

[Lucide](https://lucide.dev/) から必要なぶんだけ取り込んでいる。

## なぜ取り込むのか

CDN から読むと、**回線が落ちているときにアイコンが出ない**。ポータルは
不調のときにこそ開く画面なので、外の何かに依存させたくない。

ライブラリ全体（数千個）を持つ必要も無いので、使うものだけを置いている。
増やすときは `https://lucide.dev/icons/` から探して、同じようにここへ置く。

## なぜ Lucide を選んだか

電池の状態が **charging / full / medium / low / warning** と過不足なく揃って
いるのが決め手。1 個 300〜400 バイトで、`stroke="currentColor"` なので
CSS のマスクとして使えば色をテーマに追従させられる。

## 使い方

`app.css` の `.battery-icon` を参照。`mask-image` で読み、色は
`background-color: currentColor` で付ける。`<img>` で読むと色を変えられない。

## ライセンス

ISC License

Copyright (c) 2026 Lucide Icons and Contributors

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH
REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND
FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT,
INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM
LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR
OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
PERFORMANCE OF THIS SOFTWARE.
