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


if __name__ == "__main__":
    unittest.main(verbosity=2)
