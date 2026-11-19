"""オフラインテスト：python -m unittest discover tests"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import watcher as w  # noqa: E402


def cfg(**kw):
    c = w.Config()
    c.state_file = os.path.join(tempfile.mkdtemp(), "state.json")
    for k, v in kw.items():
        setattr(c, k, v)
    return c


class CalcTest(unittest.TestCase):
    def test_walk_book(self):
        asks = [(159.155, 4000), (159.5, 4000), (160.0, 10000)]
        avg = w.walk_book(asks, 10000)
        self.assertAlmostEqual(avg, (159.155 * 4000 + 159.5 * 4000 + 160.0 * 2000) / 10000)
        self.assertIsNone(w.walk_book(asks[:1], 10000))

    def test_tweet_example(self):
        """ツイートの実例（159.70円で10,000DAI購入 → 1USDC=160.48JPYC）で約+5,200円になるか"""
        c = cfg(withdraw_fee_dai=0.68, bridge_fee_pct=0.0, bridge_fee_usd=1.4)
        r = w.build_route(c, "ethereum", 10_000, dai_usdc_rate=0.9999, usdc_jpyc_rate=160.48, gas_usd=1.0)
        p, ratio = w.profit(r, 159.70, 0.0012)
        self.assertTrue(4_500 < p < 6_000, p)
        self.assertEqual(r.redeem_count, 2)  # 160万円 → 100万円上限なので2回

    def test_max_buy_price_consistent(self):
        c = cfg()
        r = w.build_route(c, "polygon", 10_000, 1.0, 160.047, 0.5)
        lim = w.max_buy_price(r, c.maker_fee, 0.0025)
        _, ratio = w.profit(r, lim, c.maker_fee)
        self.assertAlmostEqual(ratio, 0.0025, places=9)


class FlowTest(unittest.TestCase):
    def _run(self, c, dex_rate=160.047, bid=159.154, ask=159.155, fx_bid=159.345, rev_rate=None):
        book = w.Book(asks=[(ask, 20000)], bids=[(bid, 20000)])

        def fake_quote(cfg_, chain, a, b, amt):
            if (a, b) == ("DAI", "USDC"):
                return w.Quote(amt * 0.9998, 0.05)
            if (a, b) == ("USDC", "JPYC"):
                return w.Quote(amt * dex_rate, 0.02)
            if (a, b) == ("JPYC", "USDC"):
                return w.Quote(amt / (rev_rate or dex_rate), 0.02)
            if (a, b) == ("USDC", "DAI"):
                return w.Quote(amt * 1.0001, 0.05)
            raise AssertionError((a, b))

        st = w.AlertState(c.state_file)
        with mock.patch.object(w, "fetch_bitbank_book", return_value=book), \
             mock.patch.object(w, "fetch_gmo_usdjpy", return_value=w.FX(fx_bid, fx_bid + 0.01, True)), \
             mock.patch.object(w, "kyber_quote", side_effect=fake_quote):
            return w.check_once(c, st, dry_run=True), st

    def test_screenshot_like_alerts(self):
        c = cfg(target_chains=("polygon",))
        msgs, st = self._run(c)
        text = "\n".join(msgs)
        self.assertIn("指値チャンス", text)
        self.assertIn("成行で取れる鞘", text)
        self.assertIn("JPYC割安", text)
        self.assertNotIn("JPYC割高", text)
        # 直後の2回目は連投しない
        msgs2, _ = self._run(c)
        self.assertEqual(msgs2, [])

    def test_no_alert_when_flat(self):
        c = cfg(target_chains=("polygon",))
        msgs, _ = self._run(c, dex_rate=159.30, bid=159.30, ask=159.31, fx_bid=159.30)
        self.assertEqual(msgs, [])

    def test_resolved(self):
        c = cfg(target_chains=("polygon",))
        self._run(c)
        msgs, _ = self._run(c, dex_rate=159.30, bid=159.30, ask=159.31, fx_bid=159.30)
        self.assertTrue(any("解消" in m for m in msgs))

    def test_reverse(self):
        c = cfg(target_chains=("polygon",))
        msgs, _ = self._run(c, dex_rate=158.0, bid=160.0, ask=160.01, fx_bid=159.9, rev_rate=158.0)
        self.assertTrue(any("JPYC割高" in m for m in msgs))


if __name__ == "__main__":
    unittest.main()
