#!/usr/bin/env python3

import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sycode_candle_per_symbol_freshness as monitor


class TradeableUniverseTests(unittest.TestCase):
    @staticmethod
    def _ages(active_4h: set[str], stale_4h: set[str] | None = None):
        stale_4h = stale_4h or set()
        ages = {
            "1m": {f"CORE{i}": 60 for i in range(10)},
            "5m": {f"CORE{i}": 60 for i in range(10)},
            "15m": {f"BROAD{i}": 60 for i in range(250)},
            "1h": {f"CORE{i}": 60 for i in range(10)},
            "4h": {symbol: 60 for symbol in active_4h},
            "1D": {f"CORE{i}": 60 for i in range(10)},
        }
        for symbol in stale_4h:
            ages["4h"][symbol] = 9 * 3600
        return ages

    def test_dynamic_4h_floor_excludes_delisted_but_keeps_active_stale(self):
        active = {f"ACTIVE{i:03d}USDT" for i in range(300)}
        stale_active = "ACTIVE299USDT"
        ages = self._ages(active, {stale_active})
        ages["4h"]["DELISTEDUSDT"] = 365 * 24 * 3600

        configs, fresh_counts = monitor.materialize_configs(ages, active)
        four_hour = next(config for config in configs if config.timeframe == "4h")

        self.assertEqual(four_hour.floor, 300)
        self.assertEqual(four_hour.universe_size, 300)
        self.assertFalse(four_hour.soft_drop)
        self.assertEqual(fresh_counts["4h"], 299)
        alerts, rows = monitor.evaluate(configs, fresh_counts)
        self.assertEqual(len(alerts), 1)
        self.assertIn("299/300 tradeable", alerts[0])
        self.assertEqual(next(row for row in rows if row[0] == "4h")[-1], "ALERT_FLOOR")

    def test_dynamic_4h_floor_ignores_old_baseline_after_legitimate_delisting(self):
        active = {f"ACTIVE{i:03d}USDT" for i in range(300)}
        configs, fresh_counts = monitor.materialize_configs(self._ages(active), active)

        alerts, _ = monitor.evaluate(configs, fresh_counts, baseline={"4h": 440})

        self.assertEqual(alerts, [])

    def test_static_curated_floor_alerts_when_one_core_symbol_stops(self):
        active = {f"ACTIVE{i:03d}USDT" for i in range(300)}
        ages = self._ages(active)
        ages["1m"]["CORE9"] = 4 * 3600
        configs, fresh_counts = monitor.materialize_configs(ages, active)

        alerts, _ = monitor.evaluate(configs, fresh_counts)

        self.assertEqual(len(alerts), 1)
        self.assertIn("candles[1m]", alerts[0])
        self.assertIn("floor=10", alerts[0])

    def test_active_spot_parser_filters_non_usdt_non_trading_and_non_spot(self):
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
            monitor.parse_active_binance_spot_symbols(payload),
            {"BTCUSDT", "币安人生USDT"},
        )

    def test_dynamic_universe_fails_visible_when_live_source_collapses(self):
        active = {f"ACTIVE{i:03d}USDT" for i in range(299)}

        with self.assertRaisesRegex(RuntimeError, "tradeable 4h universe unexpectedly small"):
            monitor.materialize_configs(self._ages(active), active)

    def test_latest_row_query_is_parameterized_and_index_bounded(self):
        query = monitor.build_symbol_age_query("1m", {"BTCUSDT", "ETHUSDT"})
        self.assertIn("CROSS JOIN LATERAL", query)
        self.assertIn("ORDER BY c.timestamp DESC LIMIT 1", query)
        self.assertIn("'[\"BTCUSDT\",\"ETHUSDT\"]'", query)
        self.assertIn("'1m'", query)
        injection = monitor.build_symbol_age_query("1m", {"BAD'); DROP TABLE candles; --"})
        self.assertIn("BAD''); DROP TABLE candles; --", injection)

    def test_all_six_timeframes_are_evaluated_without_floor_weakening(self):
        ages = {spec.timeframe: {"BTCUSDT": 1} for spec in monitor.TIMEFRAME_SPECS}
        active = {f"ACTIVE{i:03d}USDT" for i in range(300)}
        ages["4h"] = {symbol: 1 for symbol in active}
        configs, counts = monitor.materialize_configs(ages, active)
        self.assertEqual([c.timeframe for c in configs], ["1m", "5m", "15m", "1h", "4h", "1D"])
        self.assertEqual([c.floor for c in configs if c.timeframe != "4h"], [10, 10, 250, 10, 10])
        self.assertEqual(next(c for c in configs if c.timeframe == "4h").floor, 300)
        self.assertEqual(set(counts), {"1m", "5m", "15m", "1h", "4h", "1D"})

    def test_empty_or_malformed_binance_payload_fails_visible(self):
        for payload in ({}, {"symbols": "not-a-list"}):
            with self.assertRaisesRegex(RuntimeError, "missing symbols list"):
                monitor.parse_active_binance_spot_symbols(payload)

    def test_probe_error_is_not_converted_to_health_result(self):
        with mock.patch.object(monitor, "fetch_symbol_ages", side_effect=RuntimeError("statement timeout")):
            with self.assertRaisesRegex(RuntimeError, "statement timeout"):
                monitor.fetch_symbol_ages("1m")


if __name__ == "__main__":
    unittest.main(verbosity=2)
