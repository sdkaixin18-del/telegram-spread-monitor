import io
import json
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from telegram_spread_monitor.__main__ import DemoMarket, run
from telegram_spread_monitor.bot_input import BotApiError, BotInput, discover_bot, listen_bot
from telegram_spread_monitor.core import Monitor, PreviewStore, Quote, Rules, now_ms


TEST_TOKEN = "123456789:sample_token_for_mock_only"


def group_update(update_id, *, group=-10001, sender=2001, message_id=42):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": group, "type": "supergroup", "title": "示例群"},
            "from": {
                "id": sender, "is_bot": True, "first_name": "Example Alert", "username": "example_alert",
            },
            "text": "🔔 价差通知【PEPE】 Binance-F → Bybit-F：开 +1.00% / 清 -2.00%",
            "message_id": message_id,
            "date": now_ms() // 1000,
        },
    }


class BotInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_matches_group_and_sender(self):
        wrong_group = group_update(1)
        wrong_group["message"]["chat"]["title"] = "其他群"
        wrong_sender = group_update(2, sender=2002)
        wrong_sender["message"]["from"]["username"] = "other"
        valid = group_update(3)

        async def handler(request):
            if request.url.path.endswith("/getMe"):
                return httpx.Response(200, json={"ok": True, "result": {"is_bot": True}})
            self.assertTrue(request.url.path.endswith("/getUpdates"))
            self.assertEqual(json.loads(request.content)["allowed_updates"], ["message"])
            return httpx.Response(200, json={"ok": True, "result": [wrong_group, wrong_sender, valid]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            output = io.StringIO()
            with redirect_stdout(output):
                await discover_bot(BotInput(client, TEST_TOKEN), "示例群", "@example_alert", None, 5)
        self.assertEqual(output.getvalue().splitlines(), ["TG_CHAT_ID=-10001", "TG_SENDER_ID=2001"])

    async def test_listener_only_accepts_the_member_and_recomputes_live_spread(self):
        wrong_group = group_update(10, group=-10002, message_id=44)
        wrong_sender = group_update(11, sender=2002, message_id=43)
        valid = group_update(12)
        duplicate = group_update(13)
        for update in (valid, duplicate, wrong_group):
            update["message"]["from"].update(first_name="示例成员", is_bot=False)
            update["message"]["text"] = "pepe"
        queued_offsets = []

        async def handler(request):
            if request.url.path.endswith("/getMe"):
                return httpx.Response(200, json={"ok": True, "result": {"is_bot": True}})
            self.assertTrue(request.url.path.endswith("/getUpdates"))
            params = json.loads(request.content)
            queued_offsets.append(params.get("offset"))
            if len(queued_offsets) == 1:
                return httpx.Response(200, json={
                    "ok": True, "result": [valid, duplicate, wrong_group, wrong_sender],
                })
            return httpx.Response(409, json={"ok": False, "description": request.url.path})

        store = PreviewStore(":memory:")
        self.addCleanup(store.close)
        market = DemoMarket()
        catalog = await market.catalog()
        ts = now_ms()
        venue_quotes = {
            "BG": Quote(catalog["BG"]["PEPE"], Decimal("0.0999"), Decimal("0.1000"), ts),
            "BN": Quote(catalog["BN"]["PEPE"], Decimal("104.25"), Decimal("105"), ts),
        }
        monitor = Monitor(market, store, Rules(min_spread_pct=Decimal("4")), -10001, 2001)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch.object(market, "quotes", new=AsyncMock(return_value=venue_quotes)) as quotes:
                with self.assertLogs("spread_monitor", level="INFO") as messages:
                    with self.assertRaisesRegex(BotApiError, "webhook") as failure:
                        await listen_bot(BotInput(client, TEST_TOKEN), monitor)
                quotes.assert_awaited_once()
        self.assertEqual(queued_offsets, [None, 14])
        self.assertNotIn(TEST_TOKEN, str(failure.exception))
        self.assertNotIn(TEST_TOKEN, "\n".join(messages.output))
        payload, = store.db.execute("SELECT payload FROM previews").fetchone()
        self.assertEqual(json.loads(payload)["gross_spread_pct"], "4.25")
        self.assertIn("duplicate_message message_id=42", "\n".join(messages.output))
        self.assertNotIn("bot_message_received message_id=43", "\n".join(messages.output))
        self.assertNotIn("bot_message_received message_id=44", "\n".join(messages.output))

    async def test_legacy_two_sender_configuration_selects_only_the_ticker_member(self):
        args = Namespace(command="listen-bot", env=Path("offline-test.env"), message_preview=True)
        settings = {"TG_CHAT_ID": "-10001", "TG_TICKER_SENDER_ID": "2002", "TG_SENDER_ID": "2001"}
        with (
            patch.dict("os.environ", settings, clear=True),
            patch("dotenv.load_dotenv"),
            patch("telegram_spread_monitor.__main__.PreviewStore", return_value=PreviewStore(":memory:")),
            patch("telegram_spread_monitor.bot_input.BotInput"),
            patch("telegram_spread_monitor.bot_input.listen_bot", new_callable=AsyncMock) as listen,
        ):
            await run(args)
        _, monitor = listen.await_args.args
        self.assertEqual(monitor.sender_id, 2002)

    async def test_long_poll_errors_do_not_expose_request_path(self):
        async def handler(request):
            raise httpx.ReadTimeout(str(request.url), request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(BotApiError) as failure:
                await BotInput(client, TEST_TOKEN).updates(None)
        self.assertEqual(failure.exception.retry_after, 3)
        self.assertNotIn(TEST_TOKEN, str(failure.exception))
