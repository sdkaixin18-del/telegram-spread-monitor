import json
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock

from telegram_spread_monitor.__main__ import DemoMarket
from telegram_spread_monitor.core import Monitor, PreviewStore, Rules, Signal, now_ms
from telegram_spread_monitor.message_preview import MessagePreviewMonitor


class MessagePreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_the_ticker_sender_uses_market_quotes(self):
        store = PreviewStore(":memory:")
        self.addCleanup(store.close)
        market = DemoMarket()
        market.quotes = AsyncMock(wraps=market.quotes)
        ticker_monitor = Monitor(market, store, Rules(min_spread_pct=Decimal("0")), -10001, 2002)
        monitor = MessagePreviewMonitor(ticker_monitor, 2001, Decimal("4"))
        ts = now_ms()
        alert = (
            "🔔 价差通知\n【INIT】\n"
            "Gate-S → Binance-F：开 +4.0123% / 清 -9.0%\n"
            "OKX-F → Binance-F：开 +4.1% / 清 -5.0%\n"
            "【SOPH】\nBitget-F → OKX-F：开 +4.0000% / 清 -9.0%"
        )
        await monitor.handle(Signal(-10001, 9999, 1, "INIT", ts))
        await monitor.handle(Signal(-10002, 2002, 1, "INIT", ts))
        await monitor.handle(Signal(-10001, 2002, 2, "some prose", ts))
        await monitor.handle(Signal(-10001, 2001, 3, alert, ts))
        market.quotes.assert_not_awaited()
        await monitor.handle(Signal(-10001, 2002, 4, "$pepe/usdt", ts))
        await monitor.handle(Signal(-10001, 2002, 4, "$pepe/usdt", ts))
        market.quotes.assert_awaited_once()

        saved = [
            json.loads(row[0])
            for row in store.db.execute("SELECT payload FROM previews ORDER BY message_id")
        ]
        self.assertEqual([item["token"] for item in saved], ["INIT", "PEPE"])
        self.assertEqual(saved[0]["reported_open_spread_pct"], "4.1")
        self.assertEqual((saved[0]["long"], saved[0]["short"]), ("OKX-F", "Binance-F"))
        self.assertEqual(saved[1]["gross_spread_pct"], "1.5")
        self.assertEqual(saved[1]["direction"], "BG 多 / BN 空")
        self.assertTrue(all(item["status"] == "preview" for item in saved))
