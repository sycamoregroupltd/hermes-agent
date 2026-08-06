#!/usr/bin/env python3

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sycode_4h_refresh as refresh


class BinanceKlineUrlTests(unittest.TestCase):
    def test_non_ascii_symbol_is_percent_encoded_in_klines_url(self):
        url = refresh.build_klines_url("币安人生USDT")

        self.assertIn("symbol=%E5%B8%81%E5%AE%89%E4%BA%BA%E7%94%9FUSDT", url)
        self.assertIn("interval=4h", url)
        self.assertIn("limit=2", url)
        self.assertNotIn("symbol=币安人生USDT", url)

    def test_fetch_latest_4h_uses_encoded_url_for_non_ascii_symbol(self):
        seen_urls = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            @staticmethod
            def read():
                return b"[]"

        def fake_urlopen(request, timeout):
            seen_urls.append(request.full_url)
            self.assertEqual(timeout, 15)
            return FakeResponse()

        with patch.object(refresh.urllib.request, "urlopen", fake_urlopen):
            self.assertEqual(refresh.fetch_latest_4h("币安人生USDT"), [])

        self.assertEqual(len(seen_urls), 1)
        self.assertIn("symbol=%E5%B8%81%E5%AE%89%E4%BA%BA%E7%94%9FUSDT", seen_urls[0])
        self.assertNotIn("币安人生", seen_urls[0])


class RefreshDecisionTests(unittest.TestCase):
    @staticmethod
    def _bar(close_time_ms):
        return {
            "openTime": close_time_ms - 4 * 3600 * 1000 + 1,
            "closeTime": close_time_ms,
            "open": "1",
            "high": "2",
            "low": "0.5",
            "close": "1.5",
            "volume": "100",
        }

    def test_active_recent_non_ascii_symbol_is_upsert_candidate(self):
        now_ms = 2_000_000_000_000
        bar = self._bar(now_ms - 3600 * 1000)

        selected, err = refresh.latest_refreshable_bar("币安人生USDT", [bar], now_ms=now_ms)

        self.assertIs(selected, bar)
        self.assertIsNone(err)

    def test_active_spot_parser_keeps_non_ascii_and_filters_delisted(self):
        payload = {
            "symbols": [
                {"symbol": "BTCUSDT", "status": "TRADING", "quoteAsset": "USDT", "isSpotTradingAllowed": True},
                {"symbol": "ETHBTC", "status": "TRADING", "quoteAsset": "BTC", "isSpotTradingAllowed": True},
                {"symbol": "OLDUSDT", "status": "BREAK", "quoteAsset": "USDT", "isSpotTradingAllowed": True},
                {"symbol": "NOSPOTUSDT", "status": "TRADING", "quoteAsset": "USDT", "isSpotTradingAllowed": False},
                {"symbol": "币安人生USDT", "status": "TRADING", "quoteAsset": "USDT", "isSpotTradingAllowed": True},
            ]
        }

        self.assertEqual(
            refresh.parse_active_binance_spot_symbols(payload),
            {"BTCUSDT", "币安人生USDT"},
        )

    def test_refresh_targets_keep_active_non_ascii_but_exclude_delisted_and_malformed(self):
        stuck = ["ARB", "DELISTEDUSDT", "币安人生USDT", "BTCUSDT"]
        active = {"BTCUSDT", "币安人生USDT"}

        self.assertEqual(refresh.select_refresh_targets(stuck, active), ["币安人生USDT", "BTCUSDT"])

    def test_delisted_symbol_is_not_introduced_when_latest_bar_is_old(self):
        now_ms = 2_000_000_000_000
        stale_bar = self._bar(now_ms - 5 * 86_400_000)

        selected, err = refresh.latest_refreshable_bar("DELISTEDUSDT", [stale_bar], now_ms=now_ms)

        self.assertIsNone(selected)
        self.assertIsNotNone(err)
        self.assertIn("delisted", err)

    def test_refresh_one_skips_delisted_without_upsert(self):
        now_ms = int(refresh.time.time() * 1000)
        stale_bar = self._bar(now_ms - 5 * 86_400_000)

        with patch.object(refresh, "fetch_latest_4h", return_value=[stale_bar]), \
             patch.object(refresh, "upsert") as upsert:
            symbol, ok, err = refresh.refresh_one("DELISTEDUSDT")

        self.assertEqual(symbol, "DELISTEDUSDT")
        self.assertFalse(ok)
        self.assertIsNotNone(err)
        self.assertIn("delisted", err)
        upsert.assert_not_called()


class GridAndFormingBarTests(unittest.TestCase):
    """Regression cover for the 2026-08-06 4h corruption.

    Three compounding defects wrote frozen, future-dated partial bars:
      * upsert() stamped bar["closeTime"], so rows landed at :59:59 — off the 4h
        grid — and for the newest bar, in the FUTURE.
      * latest_refreshable_bar() returned bars[-1], which Binance defines as the
        STILL-FORMING bar.
      * ON CONFLICT DO NOTHING then made that partial permanent.
    Measured on live data before the fix (2026-08-06 11:21Z): max(4h timestamp) =
    11:59:59, i.e. 38 minutes in the future; 452 future-stamped rows, 904 off-grid.
    The 4h coverage SLO read GREEN on exactly those fabricated rows.
    """

    H4_MS = 4 * 3600 * 1000
    NOW_MS = 1785974400000  # 2026-08-06 00:00:00Z, exactly on the 4h grid

    def _bar(self, open_ms):
        # Binance convention: closeTime is the last millisecond of the interval.
        return {
            "openTime": open_ms,
            "closeTime": open_ms + self.H4_MS - 1,
            "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0,
        }

    def test_still_forming_bar_is_never_selected(self):
        closed = self._bar(self.NOW_MS - self.H4_MS)
        forming = self._bar(self.NOW_MS)  # closeTime is in the future

        bar, err = refresh.latest_refreshable_bar("XUSDT", [closed, forming], now_ms=self.NOW_MS)

        self.assertIsNone(err)
        self.assertEqual(bar["openTime"], closed["openTime"])
        self.assertLessEqual(bar["closeTime"], self.NOW_MS)

    def test_only_a_forming_bar_yields_no_write(self):
        forming = self._bar(self.NOW_MS)

        bar, err = refresh.latest_refreshable_bar("XUSDT", [forming], now_ms=self.NOW_MS)

        self.assertIsNone(bar)
        self.assertEqual(err, "no closed bar yet")

    def test_upsert_stamps_grid_aligned_open_time_not_close_time(self):
        bar = self._bar(self.NOW_MS - self.H4_MS)
        captured = {}

        with patch.object(refresh, "DRYRUN", False), \
             patch.object(refresh, "psql", lambda q, read_only=True: captured.setdefault("q", q)):
            refresh.upsert("XUSDT", bar)

        q = captured["q"]
        # The 20:00 bar stamps its OPEN (20:00:00), not its close (23:59:59).
        self.assertIn("'2026-08-05 20:00:00+00'::timestamptz", q)
        self.assertNotIn(":59:59", q)

    def test_upsert_can_correct_a_previously_written_bar(self):
        bar = self._bar(self.NOW_MS - self.H4_MS)
        captured = {}

        with patch.object(refresh, "DRYRUN", False), \
             patch.object(refresh, "psql", lambda q, read_only=True: captured.setdefault("q", q)):
            refresh.upsert("XUSDT", bar)

        # DO NOTHING is what froze the partials permanently.
        self.assertIn("DO UPDATE", captured["q"])
        self.assertNotIn("DO NOTHING", captured["q"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
