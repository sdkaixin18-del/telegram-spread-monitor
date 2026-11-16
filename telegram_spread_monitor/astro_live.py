"""Read-only preflight for a single new Telegram ticker or live market probe."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from time import monotonic

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from .astro_trial import OPEN_PCT, TEST_ASSETS, VENUE_MAP, IndexQuote, TrialError, build_trial_pair
from .core import REQUESTED_CARD_STATE, VENUES, Contract, Quote, now_ms, ranked_routes, utc_text
from .market import (
    FAPI, MARKET_ERRORS, MarketResponseError, PublicMarket, error_reason, orderbook_request, parse_quote,
)

MAX_AGE_MS = 3000
MAX_SKEW_MS = 2000
TICKER_LOG = re.compile(r"^(.{23}) INFO tokens_identified message_id=(\d+) tokens=([A-Z0-9,]+)\s*$")
SOURCE_ERRORS = (*MARKET_ERRORS, WebSocketException, OSError)


def make_index(contract: Contract, price: object, timestamp: object) -> IndexQuote:
    value, date = Decimal(str(price)), Decimal(str(timestamp))
    if not value.is_finite() or value <= 0 or not date.is_finite() or not 10**12 <= date < 10**13:
        raise MarketResponseError("invalid_index_price_or_time")
    return IndexQuote(contract.token, VENUE_MAP[contract.venue.lower()], value / contract.price_unit, int(date))


async def gate_index(contract: Contract, timeout: float) -> IndexQuote:
    async with asyncio.timeout(timeout), connect("wss://fx-ws.gateio.ws/v4/ws/usdt", close_timeout=0.1) as ws:
        await ws.send(json.dumps({
            "time": now_ms() // 1000, "channel": "futures.tickers",
            "event": "subscribe", "payload": [contract.symbol],
        }))
        async for message in ws:
            data = json.loads(message)
            if data.get("channel") != "futures.tickers":
                continue
            if data.get("error"):
                raise MarketResponseError("gate_subscription_rejected")
            if data.get("event") == "update":
                for row in data["result"]:
                    if row.get("contract") == contract.symbol:
                        timestamp = row.get("t") or data.get("time_ms")
                        return make_index(contract, row["index_price"], timestamp)
    raise MarketResponseError("gate_index_disconnected")


async def fetch_index(market: PublicMarket, contract: Contract) -> IndexQuote:
    venue, symbol = contract.venue, contract.symbol
    if venue == "Gate":
        return await gate_index(contract, market.timeout)
    if venue in FAPI:
        data = await market.get(venue, FAPI[venue] + "/premiumIndex", {"symbol": symbol})
        if data.get("symbol") != symbol:
            raise MarketResponseError("index_symbol_mismatch")
        return make_index(contract, data["indexPrice"], data["time"])
    underlying = symbol.removesuffix("-SWAP")
    data = await market.get(venue, "/api/v5/market/index-tickers", {"instId": underlying})
    if data.get("code") != "0":
        raise MarketResponseError("index_venue_rejected")
    (row,) = data["data"]
    if row.get("instId") != underlying:
        raise MarketResponseError("index_symbol_mismatch")
    return make_index(contract, row["idxPx"], row["ts"])


async def sample_venue(market: PublicMarket, contract: Contract) -> tuple[Quote, IndexQuote] | str:
    async def book() -> tuple[Quote, dict]:
        path, params = orderbook_request(contract)
        payload = await market.get(contract.venue, path, params)
        return parse_quote(contract, payload), payload

    try:
        if contract.venue == "BG":
            quote, data = await book()
            (row,) = data["data"]
            return quote, make_index(contract, row["indexPrice"], row["ts"])
        async with asyncio.TaskGroup() as group:
            book_task = group.create_task(book())
            index_task = group.create_task(fetch_index(market, contract))
        return book_task.result()[0], index_task.result()
    except ExceptionGroup as errors:
        if any(not isinstance(error, SOURCE_ERRORS) for error in errors.exceptions):
            raise
        return "+".join(error_reason(error) for error in errors.exceptions)
    except SOURCE_ERRORS as error:
        return error_reason(error)


async def preflight(market: PublicMarket, token: str, message_id: int | None) -> dict:
    report = {"token": token, "message_id": message_id, "status": "skipped"}
    if token not in TEST_ASSETS:
        return {**report, "reason": "contract_identity_and_index_basis_unverified_for_trial"}
    catalog = await market.catalog()

    async def sample(venue: str) -> tuple[Quote, IndexQuote] | str:
        contracts = catalog[venue]
        if isinstance(contracts, str):
            return f"catalog_{contracts}"
        contract = contracts.get(token)
        if contract is None:
            return "no_live_usdt_perpetual"
        return await sample_venue(market, contract)

    results = await asyncio.gather(*(sample(venue) for venue in VENUES))
    measured_at = now_ms()
    sources, valid = {}, {}
    for venue, result in zip(VENUES, results, strict=True):
        if isinstance(result, str):
            sources[venue] = {"reason": result}
            continue
        book, index = result
        ages = (measured_at - book.timestamp_ms, measured_at - index.timestamp_ms)
        source = {**book.details(), "index": str(index.price), "index_time_utc": utc_text(index.timestamp_ms)}
        sources[venue] = source
        if any(not -1000 <= age <= MAX_AGE_MS for age in ages):
            source["reason"] = "stale_or_future_book_or_index"
        else:
            valid[venue] = result

    report["as_of_utc"] = utc_text(measured_at)
    report["sources"] = sources
    report["routes"] = output = []
    for route in ranked_routes([book for book, _ in valid.values()], MAX_SKEW_MS):
        long_index = valid[route.long.contract.venue][1]
        short_index = valid[route.short.contract.venue][1]
        result = {"direction": route.direction, "gross_pct": str(route.gross_pct),
                  "long_index": str(long_index.price), "short_index": str(short_index.price)}
        output.append(result)
        if route.gross_pct <= OPEN_PCT:
            result["reason"] = "opening_spread_not_above_4pct"
            continue
        times = [route.long.timestamp_ms, route.short.timestamp_ms, long_index.timestamp_ms, short_index.timestamp_ms]
        if max(times) - min(times) > MAX_SKEW_MS:
            result["reason"] = "book_and_index_time_skew"
            continue
        try:
            pair = build_trial_pair({
                "token": token, "status": "preview", "requested_card_state": REQUESTED_CARD_STATE,
                "market": "USDT 线性永续", "gross_spread_pct": route.gross_pct,
                "long": route.long.details(), "short": route.short.details(),
            }, long_index, short_index)
        except TrialError as error:
            result["reason"] = str(error)
            continue
        return {**report, "status": "depth_review_required", "candidate": pair}

    return {**report, "reason": "no_route_passed" if output else "insufficient_synchronized_sources"}


async def run(args: argparse.Namespace) -> None:
    async with httpx.AsyncClient() as client:
        market = PublicMarket(client, timeout=2.5, catalog_ttl=180)
        if args.token:
            print(json.dumps(await preflight(market, args.token.upper(), None), ensure_ascii=False), flush=True)
            return
        with args.watch_log.open(encoding="utf-8") as source:
            source.seek(0, 2)
            started = now_ms()
            end = monotonic() + args.wait
            print("watching_for_new_ticker", flush=True)
            await market.catalog()
            while monotonic() < end:
                line = source.readline()
                if not line:
                    await asyncio.sleep(0.2)
                    continue
                match = TICKER_LOG.fullmatch(line)
                if not match:
                    continue
                timestamp, message, symbols = match.groups()
                logged = int(datetime.fromisoformat(timestamp.replace(",", ".")).timestamp() * 1000)
                if not started <= logged <= now_ms() <= logged + 120_000:
                    continue
                for token in symbols.split(","):
                    print(json.dumps(await preflight(market, token, int(message)), ensure_ascii=False), flush=True)
                return
        print("no_new_ticker_received", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    input_type = parser.add_mutually_exclusive_group(required=True)
    input_type.add_argument("--token")
    input_type.add_argument("--watch-log", type=Path)
    parser.add_argument("--wait", type=int, default=300)
    asyncio.run(run(parser.parse_args()))
