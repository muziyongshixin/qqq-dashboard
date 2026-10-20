"""日线必须已收盘；模拟数据源同时返回昨日收盘和今日盘中行情。"""
import io
import json
import sys
import types
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

import fetch_prices as F


class ClosedDailyBarsTest(unittest.TestCase):
    def test_cutoff_at_shanghai_close_and_weekend(self):
        for now, expected in [
            ("2026-10-20T14:59:59+08:00", "2026-10-19"),
            ("2026-10-20T15:00:00+08:00", "2026-10-20"),
            ("2026-10-19T09:00:00+08:00", "2026-10-16"),
            ("2026-10-18T16:00:00+08:00", "2026-10-16"),
        ]:
            with self.subTest(now=now):
                self.assertEqual(F.latest_expected_trade_date(datetime.fromisoformat(now)), expected)

    @patch.object(F, "latest_expected_trade_date", return_value="2026-10-19")
    def test_http_sources_exclude_intraday_candle(self, _cutoff):
        payloads = [
            (F.fetch_eastmoney_one, ("510300", "20261001"),
             {"data": {"klines": ["2026-10-19,4.0", "2026-10-20,4.9"]}}),
            (F.fetch_sina_one, ("510300",),
             [{"day": "2026-10-19", "close": "4.0"},
              {"day": "2026-10-20", "close": "4.9"}]),
        ]
        for fetch, args, payload in payloads:
            with self.subTest(source=fetch.__name__), patch.object(
                F.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(payload).encode())
            ):
                self.assertEqual(fetch(*args), [("2026-10-19", 4.0)])

    @patch.object(F, "latest_expected_trade_date", return_value="2026-10-19")
    def test_tdx_full_and_incremental_exclude_intraday_candle(self, _cutoff):
        bars = [{"datetime": "2026-10-19", "close": 4.0},
                {"datetime": "2026-10-20", "close": 4.9}]
        cli = Mock()
        cli.get_fund_bars_all.return_value = cli.get_fund_bars.return_value = bars
        constants = types.ModuleType("tdxrs.constants")
        constants.KLINE_DAILY = 9
        with patch.object(F, "POOL", {"510300": {"name": "沪深300ETF"}}), patch.dict(
            sys.modules, {"tdxrs.constants": constants}
        ):
            prices, bad = F._tdxrs_full(cli, {"510300": []})
            self.assertEqual(prices, {"510300": [["2026-10-19", 4.0]]})
            self.assertEqual(bad, 0)
            updated, added, reason = F._tdxrs_incremental(
                cli, {"prices": prices}, {"510300": 5})
            self.assertEqual(updated, prices)
            self.assertEqual((added, reason), (0, ""))


if __name__ == "__main__":
    unittest.main()
