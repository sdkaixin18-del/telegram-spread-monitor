import sqlite3
import unittest
from dataclasses import replace
from decimal import Decimal

from telegram_spread_monitor.astro_trial import IndexQuote, TrialError, TrialSaver, build_trial_pair
from telegram_spread_monitor.core import now_ms


def candidate(**extra):
    return {
        "status": "preview", "requested_card_state": "enabled", "message_id": 7,
        "token": "BTC", "market": "USDT 线性永续",
        "gross_spread_pct": "4.10",
        "long": {"exchange": "BG"}, "short": {"exchange": "BN"},
        **extra,
    }


def indices():
    measured = now_ms()
    return (
        IndexQuote("BTC", "bitget", Decimal("100"), measured),
        IndexQuote("BTC", "binance", Decimal("105"), measured),
    )


class TrialTests(unittest.TestCase):
    def test_settings_and_index_rejections(self):
        long, short = indices()
        pair = build_trial_pair(candidate(), long, short)
        self.assertEqual((pair["type"], pair["openPosition"], pair["maxTradeUSDT"]), ("FF", "0.04", "10000"))
        self.assertIs(pair["status"], True)
        self.assertIs(pair["disableOpen"], True)
        self.assertIs(pair["disableClose"], True)
        for preview, quote, reason in (
            (candidate(), replace(short, price=long.price), "short_index_not_above_long"),
            (candidate(), replace(short, venue="okx"), "index_does_not_match_route"),
            (candidate(), replace(short, timestamp_ms=now_ms() - 3100), "index_time_invalid"),
            (candidate(gross_spread_pct="4"), short, "opening_spread_not_above_4pct"),
            (candidate(token="OPENAI", gross_spread_pct="931.77"), short, "asset_not_verified_for_trial"),
            (candidate(requested_card_state=None), short, "explicit_card_state_required"),
        ):
            with self.subTest(reason=reason), self.assertRaisesRegex(TrialError, reason):
                build_trial_pair(preview, long, quote)

    def test_legacy_preview_keeps_its_explicit_paused_state(self):
        pair = build_trial_pair(candidate(requested_card_state="paused"), *indices())
        self.assertIs(pair["status"], False)
        self.assertIs(pair["disableOpen"], True)
        self.assertIs(pair["disableClose"], True)

    def test_readback_requires_the_requested_state_and_both_trade_guards(self):
        class ReadbackSdk:
            def __init__(self, changes):
                self.changes = changes
                self.rows = []

            def list_pairs(self):
                return self.rows

            def add_pair(self, pair):
                self.rows = [{**pair, "id": "abc1234567", **self.changes}]

        for changes, result in (
            ({}, ("saved_enabled", "abc1234567")),
            ({"status": False}, ("unconfirmed", None)),
            ({"disableOpen": False}, ("unconfirmed", None)),
            ({"disableClose": False}, ("unconfirmed", None)),
        ):
            with self.subTest(changes=changes):
                db = sqlite3.connect(":memory:")
                self.addCleanup(db.close)
                saver = TrialSaver(ReadbackSdk(changes), db, wait=lambda _: None)
                self.assertEqual(saver.save(-1001, candidate(), *indices()), result)

    def test_uncertain_add_is_never_retried_even_if_absent(self):
        class FailedSdk:
            add_count = 0

            def list_pairs(self):
                return []

            def add_pair(self, pair):
                self.add_count += 1
                raise TimeoutError()

        sdk = FailedSdk()
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        self.assertEqual(TrialSaver(sdk, db).save(-1001, candidate(), *indices()), ("unconfirmed", None))
        self.assertEqual(
            TrialSaver(sdk, db).save(-1001, candidate(), *indices()), ("duplicate_or_pending", None),
        )
        self.assertEqual(sdk.add_count, 1)
