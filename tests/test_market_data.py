from datetime import datetime
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import market_data as data


class TaskQuoteTests(unittest.TestCase):
    day = "2025-05-08"
    after_slots = datetime.fromisoformat("2025-05-08T16:30:00+08:00")

    def test_all_task_slots_ignore_data_after_cutoff(self):
        previous = dict(date="2025-05-07", open=9, high=10, low=8, close=10, volume=100)
        rows = {time: dict(price=price, volume=volume, amount=1000)
                for time, price, volume in (("0930", 11, 10), ("1430", 12, 200), ("1431", 13, 210),
                                            ("1530", 14, 300), ("1531", 15, 310), ("1600", 99, 900))}
        old_day = {previous["date"]: previous}
        with patch.object(data, "intraday_rows", return_value={self.day: rows}), \
                patch.object(data, "recent_daily", side_effect=[old_day, old_day, {}, old_day, {}]):
            morning = data.track("hk00700", self.day, "morning", self.after_slots)
            a_close = data.track("sh600036", self.day, "afternoon", self.after_slots)
            hk_close = data.track("hk00700", self.day, "afternoon", self.after_slots)
        self.assertEqual(morning["quote"]["price"], 11)
        self.assertIsNone(morning["quote"]["auction_volume"])
        for result, price, volume, minutes, unit in ((a_close, 12, 200, 210, "手"),
                                                      (hk_close, 14, 300, 300, "股")):
            self.assertEqual(result["quote"]["price"], price)
            self.assertEqual(result["quote"]["high"], price)
            self.assertEqual(result["quote"]["volume_unit"], unit)
            self.assertEqual(result["volprice"]["elapsed_minutes"], minutes)
            self.assertEqual(result["volprice"]["adjustment"], "raw")
            self.assertEqual(result["volprice"]["bars"][-1]["volume"], volume)

    def test_early_or_missing_slot_returns_gap(self):
        before = datetime.fromisoformat("2025-05-08T09:29:40+08:00")
        with patch.object(data, "intraday_rows", return_value={self.day: {"0931": {"price": 11}}}) as fetch:
            result = data.track("sh600036", self.day, "morning", before)
            fetch.assert_not_called()
            self.assertIsNone(result["quote"])
            missing = data.track("sh600036", self.day, "morning", self.after_slots)
        self.assertIsNone(missing["quote"])
        self.assertTrue(missing["gaps"])

    def test_adjusted_history_never_uses_full_day_close(self):
        previous = dict(date="2025-05-07", open=9, high=10, low=8, close=10, volume=100)
        rows = {"0930": dict(price=11, volume=10, amount=11),
                "1430": dict(price=12, volume=200, amount=12)}
        with patch.object(data, "intraday_rows", return_value={self.day: rows}), \
                patch.object(data, "recent_daily", side_effect=[
                    {previous["date"]: previous},
                    {previous["date"]: {**previous, "close": 8}, self.day: {"open": 11, "close": 999}},
                ]):
            result = data.track("sh600036", self.day, "afternoon", self.after_slots)
        volprice = result["volprice"]
        self.assertEqual(volprice["adjustment"], "qfq")
        self.assertEqual([bar["close"] for bar in volprice["bars"]], [8, 12])

    def test_replay_and_session_parse_same_minute(self):
        feed = {"data": {"hk00700": {"data": [
            {"date": "20250508", "data": ["0930 11.2 12 134.4", "0931 11.3 bad"]},
            {"date": "20250509", "data": ["0930 12.2 10 122"]},
        ]}}}
        with patch.object(data, "get_json", return_value=feed):
            minute = data.intraday_rows("hk00700", self.day)
            prices = data.get_minutes("hk00700", self.day)
        self.assertEqual(prices, {self.day: {"0930": 11.2, "0931": 11.3}})
        self.assertEqual(minute[self.day]["0930"]["volume"], 12)
        self.assertIsNone(minute[self.day]["0931"]["volume"])

    def test_revised_financials_and_fx_use_prior_dates(self):
        reports = [{"period": "2024-12-31", "notice": "2025-04-01", "updated": "2025-05-08"}]
        self.assertFalse(data.available(reports, self.day))
        self.assertEqual(data.available(reports, "2025-05-09"), reports)
        rates = {"2025-05-07": 1.08, self.day: 1.1}
        self.assertEqual(data.previous_fx(rates, sorted(rates), self.day), ("2025-05-07", 1.08))


if __name__ == "__main__":
    unittest.main()
