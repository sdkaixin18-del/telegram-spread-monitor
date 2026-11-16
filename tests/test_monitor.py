import asyncio
import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from telethon import events

from telegram_spread_monitor.__main__ import DemoMarket
from telegram_spread_monitor.core import (
    VENUES,
    Contract,
    Monitor,
    PreviewStore,
    Quote,
    Rules,
    Signal,
    now_ms,
    ranked_routes,
    tokens_in,
)
from telegram_spread_monitor.market import (
    HOSTS,
    MarketResponseError,
    PublicMarket,
    catalog_contracts,
    parse_quote,
)
from telegram_spread_monitor.telegram_input import listen


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_token_formats_and_catalog_membership(self):
        catalog = await DemoMarket().catalog()
        result = tokens_in(
            "hello WORLD btc，BTC，$btc btcusdt BTC/USDT / 1000PEPEUSDT "
            "@PEPE https://example.org/PEPE foo@pepe.test $MISSING",
            catalog,
        )
        self.assertEqual(result, ["BTC", "PEPE"])
        self.assertEqual(tokens_in("hello btc @BTC https://example.org/BTC", catalog), [])
        self.assertEqual(tokens_in('BTC/USDT $pepe', catalog), ["BTC", "PEPE"])

    async def test_all_directions_and_quote_time_skew(self):
        timestamp = now_ms()
        bg = Quote(Contract("BG", "PEPEUSDT", "PEPE"), Decimal("0.0999"), Decimal("0.1000"), timestamp)
        bn = Quote(Contract("BN", "1000PEPEUSDT", "1000PEPE"), Decimal("101.5"), Decimal("101.6"), timestamp)
        self.assertEqual(bn.bid, Decimal("0.1015"))
        routes = ranked_routes([bg, bn], 2000)
        self.assertEqual([route.direction for route in routes], ["BG 多 / BN 空", "BN 多 / BG 空"])
        self.assertEqual(routes[0].gross_pct, Decimal("1.50"))
        self.assertLess(routes[1].gross_pct, 0)
        self.assertEqual(ranked_routes([bg, replace(bn, timestamp_ms=timestamp + 2001)], 2000), [])
        self.assertEqual(ranked_routes([bg, replace(bn, contract=bg.contract)], 2000), [])

    async def test_durable_deduplication_id_filter_and_partial_quotes(self):
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "monitor.sqlite3"
            signal = Signal(-123, 10, 42, "BTC $PEPE BTCUSDT", now_ms())
            store = PreviewStore(db_path)
            monitor = Monitor(DemoMarket(), store, Rules(), -123, 10)
            try:
                await monitor.handle(replace(signal, sender_id=9))
                await monitor.handle(replace(signal, chat_id=-999))
                self.assertFalse(store.seen(signal))
                with self.assertLogs("spread_monitor", level="INFO") as messages:
                    await monitor.handle(signal)
                logs = "\n".join(messages.output)
                self.assertIn("reason=stale_quote", logs)
                self.assertIn("reason=insufficient_quotes valid=1", logs)
                payload, = store.db.execute("SELECT payload FROM previews").fetchone()
                parsed = json.loads(payload)
                self.assertEqual(parsed["gross_spread_pct"], "1.5")
                self.assertEqual(parsed["direction"], "BG 多 / BN 空")
                self.assertEqual(parsed["requested_card_state"], "paused")
                self.assertIn("未扣手续费和滑点", parsed["note"])
            finally:
                store.close()

            restarted = PreviewStore(db_path)
            try:
                monitor = Monitor(DemoMarket(), restarted, Rules(), -123, 10)
                with self.assertLogs("spread_monitor", level="INFO") as messages:
                    await monitor.handle(signal)
                self.assertIn("duplicate_message", "\n".join(messages.output))
                self.assertEqual(restarted.db.execute("SELECT count(*) FROM previews").fetchone(), (1,))
            finally:
                restarted.close()

    async def test_threshold_boundary_and_success_before_message_completion(self):
        store = PreviewStore(":memory:")
        self.addCleanup(store.close)
        signal = Signal(-123, 10, 42, "$PEPE", now_ms())
        catalog = await DemoMarket().catalog()
        monitor = Monitor(DemoMarket(), store, Rules(min_spread_pct=Decimal("1.5001")), -123, 10)
        await monitor.preview(signal, "PEPE", catalog)
        self.assertEqual(store.db.execute("SELECT count(*) FROM previews").fetchone(), (0,))
        monitor = Monitor(DemoMarket(), store, Rules(min_spread_pct=Decimal("1.50")), -123, 10)
        await monitor.preview(signal, "PEPE", catalog)
        self.assertFalse(store.seen(signal))
        await monitor.handle(signal)
        self.assertTrue(store.seen(signal))
        self.assertEqual(store.db.execute("SELECT count(*) FROM previews").fetchone(), (1,))

    async def test_telegram_new_message_routes_only_the_configured_ids(self):
        event = SimpleNamespace(
            chat_id=-123, sender_id=10, id=9, raw_text="$PEPE", date=datetime.now(UTC),
        )

        class FakeClient:
            def on(self, builder):
                self.builder = builder

                def register(handler):
                    self.handler = handler
                    return handler

                return register

            async def run_until_disconnected(self):
                for chat_id, sender_id in ((-555, 10), (-123, 11), (-123, 10)):
                    event.chat_id, event.sender_id = chat_id, sender_id
                    await self.handler(event)

        client = FakeClient()
        monitor = SimpleNamespace(chat_id=-123, sender_id=10, handle=AsyncMock())
        with patch("telegram_spread_monitor.telegram_input.login", new_callable=AsyncMock):
            await listen(client, monitor)
        self.assertIs(client.builder, events.NewMessage)
        monitor.handle.assert_awaited_once_with(
            Signal(-123, 10, 9, "$PEPE", int(event.date.timestamp() * 1000))
        )


def fixtures(venue: str, ts: int):
    if venue == "BG":
        row = {
            "symbol": "PEPEUSDT", "baseCoin": "PEPE", "quoteCoin": "USDT",
            "supportMarginCoins": ["USDT"], "symbolStatus": "normal", "symbolType": "perpetual",
        }
        return {"code": "00000", "data": [row]}, {
            "code": "00000", "data": [
                {"symbol": "PEPEUSDT", "bidPr": "0.0999", "askPr": "0.1000",
                 "bidSz": "50", "askSz": "45", "ts": str(ts)}
            ],
        }
    if venue in ("BN", "Aster"):
        row = {
            "symbol": "1000PEPEUSDT", "baseAsset": "1000PEPE", "quoteAsset": "USDT",
            "marginAsset": "USDT", "status": "TRADING", "contractType": "PERPETUAL",
        }
        return {"symbols": [row]}, {
            "symbol": "1000PEPEUSDT", "bidPrice": "101.5", "askPrice": "101.6",
            "bidQty": "50", "askQty": "45", "time": ts,
        }
    if venue == "OKX":
        row = {
            "state": "live", "ctType": "linear", "instType": "SWAP", "settleCcy": "USDT",
            "uly": "PEPE-USDT", "instId": "PEPE-USDT-SWAP", "ctValCcy": "PEPE", "ctVal": "10000000",
        }
        return {"code": "0", "data": [row]}, {
            "code": "0", "data": [
                {"asks": [["0.1007", "3", "0", "1"]], "bids": [["0.1005", "4", "0", "1"]], "ts": str(ts)}
            ],
        }
    return [
        {"status": "trading", "in_delisting": False, "type": "direct",
         "name": "PEPE_USDT", "quanto_multiplier": "10000000"}
    ], {
        "current": ts / 1000, "update": (ts - 8000) / 1000,
        "asks": [{"s": -20, "p": "0.1001"}], "bids": [{"s": 3, "p": "0.1000"}],
    }


class VenueTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_adapter_shapes_and_parallel_requests(self):
        timestamp = now_ms()
        hosts = {httpx.URL(host).host: venue for venue, host in HOSTS.items()}
        listing_done, books_done = asyncio.Event(), asyncio.Event()
        listing_started, books_started = set(), set()

        async def handler(request):
            venue = hosts[request.url.host]
            is_listing = request.url.path.endswith(("/contracts", "/exchangeInfo", "/instruments"))
            started, done = (listing_started, listing_done) if is_listing else (books_started, books_done)
            started.add(venue)
            if len(started) == len(VENUES):
                done.set()
            await done.wait()
            if not is_listing and venue == "Aster":
                raise httpx.ReadTimeout("timeout")
            return httpx.Response(200, json=fixtures(venue, timestamp)[0 if is_listing else 1])

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            market = PublicMarket(client, timeout=1.0, catalog_ttl=300)
            catalog = await market.catalog()
            quotes = await market.quotes("PEPE", catalog)
        self.assertEqual(listing_started, set(VENUES))
        self.assertEqual(books_started, set(VENUES))
        self.assertEqual(quotes["Aster"], "timeout")
        self.assertEqual(quotes["BG"].ask, Decimal("0.1000"))
        self.assertEqual(quotes["BN"].bid, Decimal("0.1015"))
        self.assertEqual(quotes["OKX"].ask, Decimal("0.1007"))
        self.assertEqual(quotes["Gate"].ask, Decimal("0.1001"))
        self.assertEqual(quotes["Gate"].timestamp_ms, timestamp - 8000)

    async def test_garbage_book_and_inactive_markets_are_rejected(self):
        timestamp = now_ms()
        listing, book = fixtures("BN", timestamp)
        listing["symbols"].append({
            **listing["symbols"][0], "symbol": "BTCUSDT", "baseAsset": "BTC", "status": "SETTLING",
        })
        listing["symbols"].append({
            **listing["symbols"][0], "symbol": "ETHUSDT", "baseAsset": "ETH", "contractType": "CURRENT_QUARTER",
        })
        allowed = catalog_contracts("BN", listing)
        self.assertEqual(list(allowed), ["PEPE"])
        for invalid in (
            {**book, "bidQty": "0"},
            {**book, "askPrice": "NaN"},
            {**book, "askPrice": "0.099"},
            {**book, "time": 0},
            {**book, "symbol": "BTCUSDT"},
        ):
            with self.subTest(book=invalid), self.assertRaises(MarketResponseError):
                parse_quote(allowed["PEPE"], invalid)
