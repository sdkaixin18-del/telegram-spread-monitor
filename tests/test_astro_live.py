import unittest
from decimal import Decimal
from unittest.mock import patch

from telegram_spread_monitor.astro_live import preflight
from telegram_spread_monitor.core import VENUES, Contract, Quote, now_ms
from telegram_spread_monitor.astro_trial import IndexQuote


class TrialMarket:
    async def catalog(self):
        return {
            venue: {"BTC": Contract(venue, "BTCUSDT", "BTC")} if venue in {"BG", "BN"} else "timeout"
            for venue in VENUES
        }


class LiveIndexTest(unittest.IsolatedAsyncioTestCase):
    async def test_strict_spread_index_direction_and_freshness(self):
        for bid, short_index, delay, status in (
            ("104", "101", 0, "skipped"),
            ("104.01", "100", 0, "skipped"),
            ("104.01", "101", 3100, "skipped"),
            ("104.01", "101", 0, "depth_review_required"),
        ):
            with self.subTest(bid=bid, short_index=short_index, delay=delay):
                timestamp = now_ms()

                async def sample(market, contract):
                    long = contract.venue == "BG"
                    return (
                        Quote(contract, Decimal("99" if long else bid), Decimal("100" if long else "105"), timestamp),
                        IndexQuote("BTC", "bitget" if long else "binance", Decimal("100" if long else short_index),
                                   timestamp if long else timestamp - delay),
                    )

                with patch("telegram_spread_monitor.astro_live.sample_venue", side_effect=sample):
                    report = await preflight(TrialMarket(), "BTC", 9)
                self.assertEqual(report["status"], status)
                if status == "depth_review_required":
                    self.assertEqual(report["candidate"]["type"], "FF")
                    self.assertIs(report["candidate"]["status"], True)
                    self.assertIs(report["candidate"]["disableOpen"], True)
                    self.assertIs(report["candidate"]["disableClose"], True)
                    self.assertEqual(report["candidate"]["maxTradeUSDT"], "10000")

    async def test_openai_does_not_compare_unverified_denominations(self):
        report = await preflight(TrialMarket(), "OPENAI", 10)
        self.assertEqual(report["reason"], "contract_identity_and_index_basis_unverified_for_trial")
