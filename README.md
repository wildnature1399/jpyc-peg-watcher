# jpyc-peg-watcher

bitbank の **DAI/JPY 板**、DEX（KyberSwap 経由）の **USDC⇔JPYC 見積**、GMO外為の **USD/JPY** を見比べて、
「JPYC が割安＝円 → DAI → USDC → JPYC → JPYC EX で円償還 で鞘が出る」状態を Telegram / Discord に通知する Bot です。

## 通知の種類

| 通知 | 意味 |
|---|---|
| ⚡ 成行で取れる鞘 | bitbank の売り板を `TRADE_SIZE_DAI` 分成行で買った平均価格＋テイカー手数料でも、出金・スワップ・ブリッジ・ガス差引後にプラス |
| 📝 指値チャンス | 買い板の一番上に Post Only で置いて約定したら利幅が残る。「これより上では買わない」上限価格も表示 |
| 💴 JPYC割安 FX比 | DEX の 1USDC=◯JPYC が GMO のドル円より高い（＝ドルを持っている人が JPYC を買って償還すると得） |
| 🔁 JPYC割高（逆方向） | 円で JPYC を発行 → USDC → DAI → bitbank で売ると得（発行の着金待ちがあるので参考） |

初回は「🆕 発生」、続いている間は `RENOTIFY_MIN` 分おきに「⏰ 継続中」、消えたら「✅ 解消」を送ります。

## 使い方（PC / サーバー / Android の Termux）

外部ライブラリ不要。Python 3.9 以上だけで動きます。

```bash
cp .env.example .env        # Telegram のトークン等を記入
python watcher.py --test-notify   # 通知の疎通テスト
python watcher.py --dry-run --once  # 送信せずに1回だけ計算結果を表示
python watcher.py           # 常駐（60秒ごと）
```

## 使い方（GitHub Actions：PC不要）

1. このフォルダを GitHub リポジトリにする
2. Settings → Secrets and variables → Actions に `TELEGRAM_BOT_TOKEN` と `TELEGRAM_CHAT_ID` を登録
3. Actions タブで workflow を有効化（5分ごとに自動実行。`Run workflow` で手動実行も可）

private リポジトリは無料枠が月2,000分なので、cron を `*/20 * * * *` 程度に落としてください。

## Telegram Bot の作り方

1. Telegram で `@BotFather` に `/newbot` → 名前を決めるとトークンが出る → `TELEGRAM_BOT_TOKEN`
2. 作った Bot に何か1通送る
3. ブラウザで `https://api.telegram.org/bot<トークン>/getUpdates` を開き、`"chat":{"id":...}` の数字 → `TELEGRAM_CHAT_ID`

## 計算モデル

```
費用   = DAI数量 × bitbank買値 × (1 + 手数料)
手取り = ((DAI数量 − 出金手数料) × [Arbitrum DAI→USDC]
          × (1 − ブリッジ率) − ブリッジ固定$ − ガス$) × [宛先チェーン USDC→JPYC]
利益   = 手取り(JPYC=円で償還) − 費用
指値の上限価格 = 手取り ÷ (DAI数量 × (1+メイカー手数料) × (1+残したい利幅))
```

bitbank の DAI 出金は Ethereum / Arbitrum / OP Mainnet のみで、JPYC は Ethereum / Polygon / Avalanche / Kaia なので、
Arbitrum で USDC に替えてから宛先チェーンにブリッジする想定です。

## 注意

- 表示するのは**その瞬間の見積もり**です。約定・ブリッジ中の価格変動、ガス代の変動、出金や償還の処理時間で結果は変わります。
- JPYC EX の償還は 1回 100万円まで（10,000 DAI ≒ 160万円なら2回に分割）。本人確認（マイナンバーカード）と出金口座・ウォレットの登録が必要です。
- 手数料・上限は変わることがあります。`.env` で必ず最新値に合わせてください。
- 利益は課税対象になり得ます。投資助言ではありません。自己責任でご利用ください。

## テスト

```bash
python -m unittest discover tests -v
```
