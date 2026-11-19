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


def gc_candidate():
    return candidate(long={"exchange": "BN", "contract": "BTCUSDT"}, short={"exchange": "Aster"})


def gc_indices():
    long, short = indices()
    return replace(long, venue="binance"), replace(short, venue="aster")


def live_gc_rows(symbols):
    return [{"symbol": symbol, "state": "live", "tag": ""} for symbol in symbols]


class TrialTests(unittest.TestCase):
    def test_settings_and_index_rejections(self):
        long, short = indices()
        pair = build_trial_pair(candidate(), long, short)
        self.assertEqual((pair["type"], pair["openPosition"], pair["maxTradeUSDT"]), ("FF", "0.04", "10"))
        self.assertEqual((pair["maxNotional"], pair["leverage"]), ("10", "2"))
        self.assertIs(pair["status"], True)
        self.assertIs(pair["disableOpen"], False)
        self.assertIs(pair["disableClose"], False)
        self.assertEqual((pair["buyEx"], pair["sellEx"]), ("bitget", "binance"))
        for preview, quote, reason in (
            (candidate(), replace(short, price=long.price), "short_index_not_above_long"),
            (candidate(), replace(short, venue="okx"), "index_does_not_match_route"),
            (candidate(), replace(short, timestamp_ms=now_ms() - 3100), "index_time_invalid"),
            (candidate(gross_spread_pct="4"), short, "opening_spread_not_above_4pct"),
            (candidate(token="OPENAI", gross_spread_pct="931.77"), short, "asset_not_verified_for_trial"),
            (candidate(requested_card_state=None), short, "explicit_card_state_required"),
            (candidate(basis="sender_reported_spread"), short, "unsupported_preview_market"),
        ):
            with self.subTest(reason=reason), self.assertRaisesRegex(TrialError, reason):
                build_trial_pair(preview, long, quote)

    def test_legacy_preview_keeps_its_explicit_paused_state(self):
        pair = build_trial_pair(candidate(requested_card_state="paused"), *indices())
        self.assertIs(pair["status"], False)
        self.assertIs(pair["disableOpen"], True)
        self.assertIs(pair["disableClose"], True)

    def test_gc_routes_use_native_indices_and_aster_override(self):
        pair = build_trial_pair(gc_candidate(), *gc_indices())
        self.assertEqual((pair["buyEx"], pair["sellEx"]), ("gc-binance", "gc-aster"))
        self.assertEqual((pair["independentLeverageSide"], pair["independentLeverage"]), ("b", "2"))
        long, short = gc_indices()
        reversed_pair = build_trial_pair(
            candidate(long="Aster-F", short="Binance-F"), replace(long, venue="aster"), replace(short, venue="binance"),
        )
        self.assertEqual(reversed_pair["independentLeverageSide"], "a")
        with self.assertRaisesRegex(TrialError, "gc_market_symbol_mismatch"):
            build_trial_pair(candidate(long={"exchange": "BN", "contract": "1000BTCUSDT"}, short="Aster-F"), long, short)

    def test_gc_submission_requires_verified_listing_and_exact_readback(self):
        class GcSdk:
            def __init__(self):
                self.rows, self.submitted = [], []

            def list_pairs(self):
                return self.rows

            def add_pair(self, pair):
                self.submitted.append(pair)
                self.rows = [{**pair, "id": "abc1234567", "sellEx": "aster"}]

        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        sdk = GcSdk()
        for catalogue, reason in (
            (lambda _: None, "gc_catalogue_unavailable"),
            (lambda _: [], "gc_market_not_verified"),
            (lambda symbols: [{**row, "tag": "other"} for row in live_gc_rows(symbols)], "gc_market_identity_changed"),
            (lambda symbols: [{**row, "state": "suspend"} for row in live_gc_rows(symbols)], "gc_market_not_live"),
        ):
            with self.subTest(reason=reason), self.assertRaisesRegex(TrialError, reason):
                TrialSaver(sdk, db, gc_catalogue=catalogue).save(-1001, gc_candidate(), *gc_indices())
        self.assertEqual(sdk.submitted, [])
        saver = TrialSaver(sdk, db, lambda _: None, gc_catalogue=live_gc_rows)
        self.assertEqual(saver.save(-1001, gc_candidate(), *gc_indices()), ("unconfirmed", None))
        replay = {**gc_candidate(), "message_id": 8}
        self.assertEqual(saver.save(-1001, replay, *gc_indices()), ("duplicate_or_pending", None))
        self.assertEqual(len(sdk.submitted), 1)
        other_db = sqlite3.connect(":memory:")
        self.addCleanup(other_db.close)
        self.assertEqual(TrialSaver(sdk, other_db).save(-1001, replay, *gc_indices()), ("already_exists", None))

    def test_original_native_route_claim_blocks_gc_submission(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        saver = TrialSaver(None, db)
        db.execute("INSERT INTO astro_trial_submissions VALUES (-1001, 5, 'BTC', 'binance', 'aster', NULL)")
        self.assertEqual(saver.save(-1002, gc_candidate(), *gc_indices()), ("duplicate_or_pending", None))

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
            ({"disableOpen": True}, ("unconfirmed", None)),
            ({"disableClose": True}, ("unconfirmed", None)),
            ({"leverage": "1"}, ("unconfirmed", None)),
            ({"maxNotional": "10000"}, ("unconfirmed", None)),
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
