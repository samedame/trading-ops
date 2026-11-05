"""Offline tests for scripts/tradier_check.py (fake provider, no network)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tradier_check as tc  # noqa: E402
from trading_ops.snapshot import TradierProvider  # noqa: E402

NOW = 1_800_000_000.0


class Fake(TradierProvider):
    def __init__(self, age_s, state="open", empty_chain=False):
        self.token, self.base = "SECRET-ABC", "https://api.tradier.com/v1"
        self.age_s, self.state, self.empty = age_s, state, empty_chain

    def _get(self, path, params):
        st = int((NOW - self.age_s) * 1000)
        if "clock" in path:
            return {"clock": {"state": self.state}}
        if "quotes" in path:
            return {"quotes": {"quote": {"symbol": "SPY", "last": 600.0, "trade_date": st, "bid_date": st, "ask_date": st}}}
        if "expirations" in path:
            return {"expirations": {"date": ["2026-11-05"]}}
        if "chains" in path:
            if self.empty:
                return {"options": None}
            return {"options": {"option": [
                {"strike": 600, "option_type": "put", "bid": 1.0, "ask": 1.1, "bid_date": st, "ask_date": st,
                 "greeks": {"delta": -0.5, "mid_iv": 0.2}, "volume": 5, "open_interest": 9}]}}
        raise AssertionError(path)


class Check(unittest.TestCase):
    def run_it(self, fake):
        out = []
        rc = tc.run(provider=fake, now=NOW, say=out.append)
        self.assertFalse(any("SECRET-ABC" in l for l in out))
        return rc, out

    def test_live(self):
        self.assertEqual(self.run_it(Fake(10))[0], 0)

    def test_stale_while_open_fails(self):
        self.assertEqual(self.run_it(Fake(14 * 60))[0], 1)

    def test_closed_market_passes_if_parser_works(self):
        self.assertEqual(self.run_it(Fake(7200, "closed"))[0], 0)

    def test_empty_chain_fails(self):
        self.assertEqual(self.run_it(Fake(10, empty_chain=True))[0], 1)


if __name__ == "__main__":
    unittest.main()
