"""Tencent-hosted Telegram input and the installed Astro panel's SDK / depth adapters."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
import logging
import os

import httpx
from app.astro_sdk import AstroSdkClient, astro_sdk_config
from app.astro_spread_scanner import _fetch_direct_depth_book, _consume_asks_by_notional, _consume_bids_by_quantity
from .astro_live import preflight
from .astro_trial import CARD_LIMIT_USDT, OPEN_PCT, IndexQuote, TrialError, TrialSaver, VENUE_MAP
from .astro_gc import native_exchange
from .bot_input import BotInput, listen_bot
from .core import LOG, REQUESTED_CARD_STATE, Monitor, PreviewStore, Rules, now_ms
from .market import PublicMarket


def depth_snapshot(token, buy, sell):
    with httpx.Client(timeout=2.5) as client, ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [pool.submit(_fetch_direct_depth_book, client, exchange, "future", token, {})
                 for exchange in (buy, sell)]
        a, b = [task.result() for task in tasks]
    for book in (a, b):
        if book["rawSymbol"] != token or book["priceDivisor"] != 1 or book["timestampSource"] == "response_received_depth":
            raise TrialError("unverified_depth_identity_or_time")
    opened = _consume_asks_by_notional(a["asks"], float(CARD_LIMIT_USDT))
    closed = _consume_bids_by_quantity(b["bids"], opened["quantity"])
    ask, bid = (Decimal(str(x["averagePrice"])) for x in (opened, closed))
    return (bid - ask) / ask * 100, [a["exchangeTimestamp"], b["exchangeTimestamp"]]


def sdk_client():
    config = replace(astro_sdk_config(), base_url="https://127.0.0.1:8443")
    return AstroSdkClient(config)


class CardMonitor(Monitor):
    async def preview(self, signal, token, catalog):
        try:
            report = await preflight(self.market, token, signal.message_id)
            if report["status"] != "depth_review_required":
                LOG.info("card_skipped msg=%s token=%s reason=%s best=%s", signal.message_id, token,
                         report["reason"], report.get("routes", [])[:1])
                return
            pair = report["candidate"]
            buy, sell = (native_exchange(pair[key]) for key in ("buyEx", "sellEx"))
            sources = {VENUE_MAP[k.lower()]: v for k, v in report["sources"].items()}
            indices = [IndexQuote(token, venue, Decimal(sources[venue]["index"]),
                                  int(datetime.fromisoformat(sources[venue]["index_time_utc"]).timestamp()*1000))
                       for venue in (buy, sell)]
            spread, times = await asyncio.to_thread(depth_snapshot, token, buy, sell)
            times.extend(index.timestamp_ms for index in indices)
            if spread <= OPEN_PCT:
                raise TrialError("executable_spread_not_above_4pct")

            def check_fresh():
                now = now_ms()
                if now - signal.timestamp_ms > self.rules.max_message_age_ms:
                    raise TrialError("message_expired")
                if max(times)-min(times) > 2000 or max(times)>now+1000 or min(times)<now-3000:
                    raise TrialError("depth_index_timing_invalid")

            check_fresh()
            preview = dict(status="preview", requested_card_state=REQUESTED_CARD_STATE, token=token,
                           message_id=signal.message_id, market="USDT 线性永续", gross_spread_pct=str(spread),
                           long=sources[buy], short=sources[sell])
            with sdk_client() as sdk:
                result, card_id = TrialSaver(sdk, self.store.db, before_claim=check_fresh).save(
                    signal.chat_id, preview, *indices)
            LOG.info("astro_result msg=%s token=%s result=%s id=%s route=%s/%s pct=%s",
                     signal.message_id, token, result, card_id, buy, sell, spread)
        except TrialError as exc:
            LOG.info("card_skipped msg=%s token=%s reason=%s", signal.message_id, token, exc)
        except Exception as exc:
            LOG.error("card_check_failed msg=%s token=%s error=%s", signal.message_id, token, type(exc).__name__)


async def main():
    state = PreviewStore(os.environ["STATE_DB_PATH"])
    try:
        with sdk_client() as sdk:
            LOG.info("astro_connected cards=%d mode=real state=%s cap_usdt=%s", len(sdk.list_pairs()),
                     REQUESTED_CARD_STATE, CARD_LIMIT_USDT)
        async with httpx.AsyncClient() as client:
            monitor = CardMonitor(PublicMarket(client, timeout=2.5, catalog_ttl=300), state, Rules(min_spread_pct=OPEN_PCT),
                                  int(os.environ["TG_CHAT_ID"]), int(os.environ["TG_SENDER_ID"]))
            await listen_bot(BotInput(client, os.environ["TG_BOT_TOKEN"]), monitor)
    finally:
        state.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main())
