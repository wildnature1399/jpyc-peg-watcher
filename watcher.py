#!/usr/bin/env python3
"""
jpyc-peg-watcher
================
bitbank の DAI/JPY 板・DEX の USDC⇔JPYC 見積・GMO外為の USD/JPY を見比べて
「JPYC が割安（＝円→DAI→USDC→JPYC→円償還で鞘が出る）」状態を検出し、
Telegram / Discord に通知する監視 Bot。

外部ライブラリ不要（Python 3.9+ 標準ライブラリのみ）。

  python watcher.py            # 常駐（INTERVAL_SEC ごとにチェック）
  python watcher.py --once     # 1回だけチェック（cron / GitHub Actions 用）
  python watcher.py --dry-run  # 通知を送らず標準出力に表示
  python watcher.py --test-notify  # 通知の疎通テスト

※ 表示する損益は「その瞬間の見積もり」です。約定・ブリッジ中の価格変動、
   ガス代の変動、出金・償還の処理時間などで結果は変わります。投資助言ではありません。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

JST = dt.timezone(dt.timedelta(hours=9))
log = logging.getLogger("jpyc-peg-watcher")

# ---------------------------------------------------------------------------
# トークン定義（JPYC は資金移動業版＝JPYC EX で償還できる新JPYC。旧 Prepaid とは別アドレス）
# ---------------------------------------------------------------------------
JPYC = ("0xE7C3D8C9a439feDe00D2600032D5dB0Be71C3c29", 18)
TOKENS = {
    "arbitrum": {
        "DAI": ("0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1", 18),
        "USDC": ("0xaf88d065e77c8cC2239327C5EDb3A432268e5831", 6),
    },
    "polygon": {
        "USDC": ("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", 6),
        "JPYC": JPYC,
    },
    "ethereum": {
        "USDC": ("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6),
        "JPYC": JPYC,
    },
    "avalanche": {
        "USDC": ("0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E", 6),
        "JPYC": JPYC,
    },
}
CHAIN_LABEL = {"polygon": "Polygon", "ethereum": "Ethereum", "avalanche": "Avalanche", "arbitrum": "Arbitrum"}


# ---------------------------------------------------------------------------
# 設定（環境変数 or .env で上書き）
# ---------------------------------------------------------------------------
def _load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.split(" #", 1)[0]  # 行末コメントを除去
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _env(name: str, default, cast=str):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    if cast is bool:
        return raw.lower() in ("1", "true", "yes", "on")
    return cast(raw)


@dataclass
class Config:
    # 通知先
    telegram_token: str = ""
    telegram_chat_id: str = ""
    discord_webhook: str = ""
    # 取引条件
    bitbank_pair: str = "dai_jpy"
    trade_size: float = 10_000          # 1回に回す DAI 数量
    taker_fee: float = 0.0012           # bitbank テイカー 0.12%
    maker_fee: float = -0.0002          # bitbank メイカー -0.02%（リベート）
    tick: float = 0.001                 # 呼値
    withdraw_fee_dai: float = 0.68      # bitbank DAI 出金手数料（Arbitrum）
    bridge_fee_pct: float = 0.00025     # ブリッジ手数料（率）
    bridge_fee_usd: float = 1.5         # ブリッジ手数料（固定 $）
    extra_gas_usd: float = 0.3          # 承認(approve)・償還送付などの追加ガス概算 $
    redeem_limit_jpy: int = 1_000_000   # JPYC EX 償還 1回上限
    redeem_fee_jpy: float = 0           # 償還手数料（無料）
    target_chains: tuple = ("polygon", "ethereum")
    # 通知しきい値（%）
    min_taker_pct: float = 0.10         # 成行で取れる鞘がこれ以上なら通知
    min_maker_pct: float = 0.25         # 指値（Post Only）で残したい利幅
    fx_alert_pct: float = 0.30          # DEX の JPYC/USDC が FX 比でこれ以上割安なら通知
    usd_return_cost_pct: float = 0.30   # FX比アラートの「ドル戻しコスト」
    enable_reverse: bool = True         # 逆方向（JPYC割高）も監視
    min_reverse_pct: float = 0.10
    # 動作
    interval_sec: int = 60
    renotify_min: int = 15              # 継続中の再通知間隔（分）
    notify_resolved: bool = True
    state_file: str = "state.json"
    kyber_client_id: str = "jpyc-peg-watcher"

    @classmethod
    def from_env(cls) -> "Config":
        _load_dotenv()
        c = cls()
        c.telegram_token = _env("TELEGRAM_BOT_TOKEN", c.telegram_token)
        c.telegram_chat_id = _env("TELEGRAM_CHAT_ID", c.telegram_chat_id)
        c.discord_webhook = _env("DISCORD_WEBHOOK_URL", c.discord_webhook)
        c.bitbank_pair = _env("BITBANK_PAIR", c.bitbank_pair)
        c.trade_size = _env("TRADE_SIZE_DAI", c.trade_size, float)
        c.taker_fee = _env("TAKER_FEE", c.taker_fee, float)
        c.maker_fee = _env("MAKER_FEE", c.maker_fee, float)
        c.tick = _env("TICK", c.tick, float)
        c.withdraw_fee_dai = _env("WITHDRAW_FEE_DAI", c.withdraw_fee_dai, float)
        c.bridge_fee_pct = _env("BRIDGE_FEE_PCT", c.bridge_fee_pct, float)
        c.bridge_fee_usd = _env("BRIDGE_FEE_USD", c.bridge_fee_usd, float)
        c.extra_gas_usd = _env("EXTRA_GAS_USD", c.extra_gas_usd, float)
        c.redeem_limit_jpy = _env("REDEEM_LIMIT_JPY", c.redeem_limit_jpy, int)
        c.redeem_fee_jpy = _env("REDEEM_FEE_JPY", c.redeem_fee_jpy, float)
        chains = _env("TARGET_CHAINS", ",".join(c.target_chains))
        c.target_chains = tuple(x.strip().lower() for x in chains.split(",") if x.strip())
        c.min_taker_pct = _env("MIN_TAKER_PCT", c.min_taker_pct, float)
        c.min_maker_pct = _env("MIN_MAKER_PCT", c.min_maker_pct, float)
        c.fx_alert_pct = _env("FX_ALERT_PCT", c.fx_alert_pct, float)
        c.usd_return_cost_pct = _env("USD_RETURN_COST_PCT", c.usd_return_cost_pct, float)
        c.enable_reverse = _env("ENABLE_REVERSE", c.enable_reverse, bool)
        c.min_reverse_pct = _env("MIN_REVERSE_PCT", c.min_reverse_pct, float)
        c.interval_sec = _env("INTERVAL_SEC", c.interval_sec, int)
        c.renotify_min = _env("RENOTIFY_MIN", c.renotify_min, int)
        c.notify_resolved = _env("NOTIFY_RESOLVED", c.notify_resolved, bool)
        c.state_file = _env("STATE_FILE", c.state_file)
        c.kyber_client_id = _env("KYBER_CLIENT_ID", c.kyber_client_id)
        for ch in c.target_chains:
            if ch not in TOKENS or "JPYC" not in TOKENS[ch]:
                raise SystemExit(f"TARGET_CHAINS に未対応のチェーン: {ch}（polygon / ethereum / avalanche）")
        return c


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def http_json(url: str, *, method: str = "GET", headers: Optional[dict] = None,
              body: Optional[dict] = None, timeout: int = 15, retries: int = 2):
    hdrs = {"User-Agent": "jpyc-peg-watcher/1.0", "Accept": "application/json"}
    hdrs.update(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"HTTP失敗 {url}: {last}")


# ---------------------------------------------------------------------------
# データ取得
# ---------------------------------------------------------------------------
@dataclass
class Book:
    asks: list  # [(price, qty)] 安い順
    bids: list  # [(price, qty)] 高い順

    @property
    def best_ask(self) -> float:
        return self.asks[0][0]

    @property
    def best_bid(self) -> float:
        return self.bids[0][0]


def fetch_bitbank_book(pair: str) -> Book:
    j = http_json(f"https://public.bitbank.cc/{pair}/depth")
    if j.get("success") != 1:
        raise RuntimeError(f"bitbank depth エラー: {j}")
    d = j["data"]
    asks = sorted(((float(p), float(q)) for p, q in d["asks"]), key=lambda x: x[0])
    bids = sorted(((float(p), float(q)) for p, q in d["bids"]), key=lambda x: -x[0])
    if not asks or not bids:
        raise RuntimeError("bitbank の板が空です")
    return Book(asks, bids)


@dataclass
class FX:
    bid: float
    ask: float
    is_open: bool

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


def fetch_gmo_usdjpy() -> FX:
    j = http_json("https://forex-api.coin.z.com/public/v1/ticker")
    for t in j.get("data", []):
        if t.get("symbol") == "USD_JPY":
            return FX(float(t["bid"]), float(t["ask"]), t.get("status") == "OPEN")
    raise RuntimeError("GMO FX: USD_JPY が見つかりません")


@dataclass
class Quote:
    amount_out: float
    gas_usd: float


def kyber_quote(cfg: Config, chain: str, sym_in: str, sym_out: str, amount_in: float) -> Quote:
    """KyberSwap Aggregator の見積（実際に約定させるわけではない）"""
    tin, din = TOKENS[chain][sym_in]
    tout, dout = TOKENS[chain][sym_out]
    q = urllib.parse.urlencode({
        "tokenIn": tin, "tokenOut": tout,
        "amountIn": str(int(amount_in * 10 ** din)),
    })
    j = http_json(f"https://aggregator-api.kyberswap.com/{chain}/api/v1/routes?{q}",
                  headers={"x-client-id": cfg.kyber_client_id})
    if j.get("code") != 0:
        raise RuntimeError(f"Kyber {chain} {sym_in}->{sym_out}: {j.get('message')}")
    rs = j["data"]["routeSummary"]
    return Quote(int(rs["amountOut"]) / 10 ** dout, float(rs.get("gasUsd") or 0))


# ---------------------------------------------------------------------------
# 計算（純粋関数：テスト可能）
# ---------------------------------------------------------------------------
def walk_book(levels: list, qty: float) -> Optional[float]:
    """板を qty 分だけ食ったときの平均約定価格。板が足りなければ None"""
    remain, cost = qty, 0.0
    for p, q in levels:
        take = min(remain, q)
        cost += take * p
        remain -= take
        if remain <= 1e-12:
            return cost / qty
    return None


@dataclass
class Route:
    """bitbank で DAI を買って JPYC にするまでの見積（購入価格に依存しない部分）"""
    chain: str
    dai: float                 # 購入 DAI
    usdc_arb: float            # Arbitrum で DAI→USDC 後
    usdc_dest: float           # ブリッジ・ガス差引後に宛先チェーンで使える USDC
    jpyc: float                # 最終 JPYC
    dex_rate: float            # 宛先チェーンの 1USDC = ? JPYC
    gas_usd: float
    redeem_count: int = 1

    @property
    def proceeds_jpy(self) -> float:
        return self.jpyc  # 1 JPYC = 1 円で償還


def build_route(cfg: Config, chain: str, dai: float, dai_usdc_rate: float,
                usdc_jpyc_rate: float, gas_usd: float) -> Route:
    after_wd = dai - cfg.withdraw_fee_dai
    usdc_arb = after_wd * dai_usdc_rate
    usdc_dest = usdc_arb * (1 - cfg.bridge_fee_pct) - cfg.bridge_fee_usd - gas_usd
    jpyc = usdc_dest * usdc_jpyc_rate
    n = max(1, math.ceil(jpyc / cfg.redeem_limit_jpy))
    jpyc -= cfg.redeem_fee_jpy * n
    return Route(chain, dai, usdc_arb, usdc_dest, jpyc, usdc_jpyc_rate, gas_usd, n)


def profit(route: Route, buy_price: float, fee: float) -> tuple[float, float]:
    """(利益円, 利益率) を返す"""
    cost = route.dai * buy_price * (1 + fee)
    p = route.proceeds_jpy - cost
    return p, p / cost


def max_buy_price(route: Route, fee: float, margin: float) -> float:
    """利幅 margin を残せる DAI 購入価格の上限"""
    return route.proceeds_jpy / (route.dai * (1 + fee) * (1 + margin))


# ---------------------------------------------------------------------------
# 通知
# ---------------------------------------------------------------------------
def send_notify(cfg: Config, text: str, dry_run: bool = False) -> None:
    if dry_run or not (cfg.telegram_token or cfg.discord_webhook):
        print("-" * 60 + "\n" + text + "\n" + "-" * 60)
        return
    if cfg.telegram_token and cfg.telegram_chat_id:
        try:
            http_json(f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage",
                      method="POST", body={"chat_id": cfg.telegram_chat_id, "text": text,
                                           "disable_web_page_preview": True})
        except Exception as e:  # noqa: BLE001
            log.error("Telegram 送信失敗: %s", e)
    if cfg.discord_webhook:
        try:
            http_json(cfg.discord_webhook, method="POST", body={"content": text[:1900]})
        except Exception as e:  # noqa: BLE001
            log.error("Discord 送信失敗: %s", e)


class AlertState:
    """同じアラートを連投しないための状態管理（state.json に保存）"""

    def __init__(self, path: str):
        self.path = path
        try:
            with open(path, encoding="utf-8") as f:
                self.d = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self.d = {}

    def save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.d, f, ensure_ascii=False, indent=1)

    def on_active(self, key: str, renotify_min: int, now: float) -> Optional[str]:
        """通知すべきなら見出し（🆕/⏰）を返す。不要なら None"""
        s = self.d.get(key)
        if not s:
            self.d[key] = {"since": now, "last": now}
            return "🆕 発生"
        if now - s["last"] >= renotify_min * 60:
            s["last"] = now
            mins = int((now - s["since"]) // 60)
            return f"⏰ 継続中({mins}分)"
        return None

    def on_inactive(self, key: str) -> bool:
        return self.d.pop(key, None) is not None


# ---------------------------------------------------------------------------
# メッセージ整形
# ---------------------------------------------------------------------------
def yen(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}¥{abs(x):,.0f}"


def usd(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}${abs(x):,.0f}"


def pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


def cost_note(cfg: Config) -> str:
    return (f"※出金{cfg.withdraw_fee_dai}DAI・スワップ・ブリッジ({cfg.bridge_fee_pct*100:.3f}%+${cfg.bridge_fee_usd})"
            f"・ガス差引の概算。ブリッジ中にレートが動くリスクあり")


def redeem_note(r: Route, cfg: Config) -> str:
    s = f"※償還はJPYC EXで予約→送付。1回{cfg.redeem_limit_jpy//10000}万円まで"
    if r.redeem_count > 1:
        s += f"（今回は{r.redeem_count}回に分割）"
    return s


def msg_maker(head, r: Route, book: Book, cfg: Config) -> str:
    bid = book.best_bid
    p_jpy, p_pct = profit(r, bid, cfg.maker_fee)
    limit = max_buy_price(r, cfg.maker_fee, cfg.min_maker_pct / 100)
    return (f"{head} 📝 指値チャンス：{bid:.3f} で買えたら手取り {pct(p_pct)}（約 {yen(p_jpy)}）\n"
            f"bitbank 買い板の一番上 {bid:.3f} / 売り板の一番上 {book.best_ask:.3f}\n"
            f"{CHAIN_LABEL[r.chain]} DEX 1USDC={r.dex_rate:.3f} JPYC\n"
            f"利幅{cfg.min_maker_pct:.2f}%を残せる上限 {limit:.3f}（これより上では買わない）\n"
            f"※約定するかは成行の売り次第。Post Onlyで置くこと\n{redeem_note(r, cfg)}")


def msg_taker(head, r: Route, avg: float, book: Book, cfg: Config) -> str:
    p_jpy, p_pct = profit(r, avg, cfg.taker_fee)
    return (f"{head} ⚡ 成行で取れる鞘：手取り {pct(p_pct)}（{cfg.trade_size:,.0f} DAI で約 {yen(p_jpy)}）\n"
            f"bitbank 成行の平均 {avg:.3f}（売り板の一番上 {book.best_ask:.3f}）＋手数料{cfg.taker_fee*100:.2f}%\n"
            f"{CHAIN_LABEL[r.chain]} DEX 1USDC={r.dex_rate:.3f} JPYC\n"
            f"{cost_note(cfg)}\n{redeem_note(r, cfg)}")


def msg_fx(head, chain: str, usdc: float, jpyc: float, fx: FX, cfg: Config) -> str:
    rate = jpyc / usdc
    prem = rate / fx.bid - 1
    gain_usd = usdc * prem
    net_usd = gain_usd - usdc * cfg.usd_return_cost_pct / 100
    closed = "" if fx.is_open else "（FX休場中・最終値）"
    return (f"{head} 💴 JPYC割安 ({CHAIN_LABEL[chain]}) FX比 {pct(prem)}\n"
            f"{usdc:,.0f} USDC → {jpyc:,.0f} JPYC (1USDC={rate:.3f})\n"
            f"ドル円 {fx.bid:.3f} (GMO){closed}\n"
            f"→ 買って JPYC EX で償還すると 約 {usd(gain_usd)}"
            f"（ドル戻しコスト{cfg.usd_return_cost_pct:.1f}%差引で {usd(net_usd)}）\n"
            f"※償還はJPYC EXで予約→送付。1回{cfg.redeem_limit_jpy//10000}万円まで")


def msg_reverse(head, chain: str, jpy_in: float, dai_out: float, bid_avg: float,
                p_jpy: float, p_pct: float, cfg: Config) -> str:
    return (f"{head} 🔁 JPYC割高（逆方向） ({CHAIN_LABEL[chain]})：手取り {pct(p_pct)}（約 {yen(p_jpy)}）\n"
            f"JPYC発行 {jpy_in:,.0f}円 → USDC → Arbitrum → {dai_out:,.1f} DAI\n"
            f"bitbank 成行売りの平均 {bid_avg:.3f} − 手数料{cfg.taker_fee*100:.2f}%\n"
            f"※発行は銀行振込の着金待ちあり。待っている間に鞘が消えることが多い")


# ---------------------------------------------------------------------------
# 1回分のチェック
# ---------------------------------------------------------------------------
def check_once(cfg: Config, state: AlertState, dry_run: bool = False) -> list[str]:
    now = time.time()
    sent: list[str] = []
    book = fetch_bitbank_book(cfg.bitbank_pair)
    try:
        fx = fetch_gmo_usdjpy()
    except Exception as e:  # noqa: BLE001
        log.warning("FX取得失敗（FX比アラートはスキップ）: %s", e)
        fx = None

    # Arbitrum: (DAI - 出金手数料) → USDC
    dai = cfg.trade_size
    q_arb = kyber_quote(cfg, "arbitrum", "DAI", "USDC", dai - cfg.withdraw_fee_dai)
    dai_usdc = q_arb.amount_out / (dai - cfg.withdraw_fee_dai)
    usdc_for_bridge = q_arb.amount_out * (1 - cfg.bridge_fee_pct) - cfg.bridge_fee_usd

    routes: list[Route] = []
    fx_candidates = []
    for ch in cfg.target_chains:
        try:
            q = kyber_quote(cfg, ch, "USDC", "JPYC", usdc_for_bridge)
        except Exception as e:  # noqa: BLE001
            log.warning("%s 見積失敗: %s", ch, e)
            continue
        rate = q.amount_out / usdc_for_bridge
        gas = q_arb.gas_usd + q.gas_usd + cfg.extra_gas_usd
        routes.append(build_route(cfg, ch, dai, dai_usdc, rate, gas))
        fx_candidates.append((ch, usdc_for_bridge, q.amount_out))
    if not routes:
        raise RuntimeError("どのチェーンでも見積が取れませんでした")

    best = max(routes, key=lambda r: r.proceeds_jpy)
    log.info("bid=%.3f ask=%.3f best=%s 1USDC=%.3fJPYC fx=%s", book.best_bid, book.best_ask,
             best.chain, best.dex_rate, f"{fx.bid:.3f}" if fx else "-")

    def handle(key: str, active: bool, make_msg):
        if active:
            head = state.on_active(key, cfg.renotify_min, now)
            if head:
                m = make_msg(head)
                send_notify(cfg, m, dry_run)
                sent.append(m)
        elif state.on_inactive(key) and cfg.notify_resolved:
            m = f"✅ 解消：{key}"
            send_notify(cfg, m, dry_run)
            sent.append(m)

    # ① 成行（テイカー）
    avg = walk_book(book.asks, dai)
    if avg is not None:
        _, p_pct = profit(best, avg, cfg.taker_fee)
        handle("taker", p_pct * 100 >= cfg.min_taker_pct,
               lambda h: msg_taker(h, best, avg, book, cfg))

    # ② 指値（メイカー / Post Only で買い板の一番上に置く）
    limit = max_buy_price(best, cfg.maker_fee, cfg.min_maker_pct / 100)
    handle("maker", book.best_bid <= limit, lambda h: msg_maker(h, best, book, cfg))

    # ③ FX 比で JPYC が割安
    if fx:
        ch, u, j = max(fx_candidates, key=lambda x: x[2] / x[1])
        prem = (j / u) / fx.bid - 1
        handle("fx", prem * 100 >= cfg.fx_alert_pct, lambda h: msg_fx(h, ch, u, j, fx, cfg))

    # ④ 逆方向：円→JPYC発行→USDC→Arbitrum→DAI→bitbank売り
    if cfg.enable_reverse:
        try:
            jpy_in = min(cfg.redeem_limit_jpy, dai * book.best_bid)
            best_rev = None
            for ch in cfg.target_chains:
                q1 = kyber_quote(cfg, ch, "JPYC", "USDC", jpy_in)
                usdc = q1.amount_out * (1 - cfg.bridge_fee_pct) - cfg.bridge_fee_usd
                q2 = kyber_quote(cfg, "arbitrum", "USDC", "DAI", usdc)
                dai_out = q2.amount_out - (q1.gas_usd + q2.gas_usd + cfg.extra_gas_usd)
                bid_avg = walk_book(book.bids, dai_out)
                if bid_avg is None:
                    continue
                p_jpy = dai_out * bid_avg * (1 - cfg.taker_fee) - jpy_in
                cand = (p_jpy, ch, dai_out, bid_avg)
                if best_rev is None or cand[0] > best_rev[0]:
                    best_rev = cand
            if best_rev:
                p_jpy, ch, dai_out, bid_avg = best_rev
                p_pct = p_jpy / jpy_in
                handle("reverse", p_pct * 100 >= cfg.min_reverse_pct,
                       lambda h: msg_reverse(h, ch, jpy_in, dai_out, bid_avg, p_jpy, p_pct, cfg))
        except Exception as e:  # noqa: BLE001
            log.warning("逆方向チェック失敗: %s", e)

    state.save()
    return sent


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="JPYC peg watcher")
    ap.add_argument("--once", action="store_true", help="1回だけチェックして終了")
    ap.add_argument("--dry-run", action="store_true", help="通知を送らず表示のみ")
    ap.add_argument("--test-notify", action="store_true", help="テスト通知を送る")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config.from_env()

    if a.test_notify:
        send_notify(cfg, f"✅ jpyc-peg-watcher 通知テスト {dt.datetime.now(JST):%Y-%m-%d %H:%M}", a.dry_run)
        return 0

    state = AlertState(cfg.state_file)
    while True:
        try:
            check_once(cfg, state, a.dry_run)
        except Exception as e:  # noqa: BLE001
            log.error("チェック失敗: %s", e)
            if a.once:
                return 1
        if a.once:
            return 0
        time.sleep(cfg.interval_sec)


if __name__ == "__main__":
    sys.exit(main())
