"""Simulate cards using each Telegram sender's configured rules."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from decimal import Decimal

from .core import LOG, REQUESTED_CARD_STATE, Monitor, Signal, now_ms

TICKER = re.compile(r"\$?([A-Z0-9]{1,32})(?:\s*[/_-]\s*USDT)?", re.I | re.ASCII)
HEADER = re.compile(r"【([^】]+)】")
OPEN_ROUTE = re.compile(
    r"([A-Z0-9.]+-[FS])\s*→\s*([A-Z0-9.]+-[FS])\s*[：:]\s*开\s*"
    r"([+-]?\d+(?:\.\d+)?)\s*%(?=\s*(?:/|$))",
    re.I | re.ASCII,
)


def read_ticker(text: str) -> str | None:
    match = TICKER.fullmatch(text.strip())
    if not match:
        return None
    ticker = match[1].upper()
    if len(ticker) > 4 and ticker.endswith("USDT"):
        ticker = ticker[:-4]
    if not any(character.isalpha() for character in ticker):
        return None
    return ticker


def reported_routes(text: str) -> dict[str, tuple[Decimal, str, str]]:
    routes: dict[str, tuple[Decimal, str, str]] = {}
    token = None
    for line in text.splitlines():
        line = line.strip()
        header = HEADER.fullmatch(line)
        if header:
            token = read_ticker(header[1])
            continue
        route = OPEN_ROUTE.match(line) if token else None
        if route and route[1].casefold() != route[2].casefold():
            price = Decimal(route[3])
            if token not in routes or price > routes[token][0]:
                routes[token] = (price, route[1], route[2])
    return routes


class MessagePreviewMonitor:
    def __init__(self, ticker_monitor: Monitor, sender_id: int, reported_min_pct: Decimal):
        self.ticker_monitor = ticker_monitor
        self.store = ticker_monitor.store
        self.rules = ticker_monitor.rules
        self.chat_id = ticker_monitor.chat_id
        self.sender_id = sender_id
        self.ticker_sender_id = ticker_monitor.sender_id
        self.reported_min_pct = reported_min_pct

    async def handle(self, signal: Signal) -> None:
        if signal.chat_id != self.chat_id or signal.sender_id not in (self.sender_id, self.ticker_sender_id):
            return
        if self.store.seen(signal):
            LOG.info("duplicate_message message_id=%s", signal.message_id)
            return
        if now_ms() - signal.timestamp_ms > self.rules.max_message_age_ms:
            LOG.info("message_skipped message_id=%s reason=stale_message", signal.message_id)
            return

        if signal.sender_id == self.ticker_sender_id:
            ticker = read_ticker(signal.text)
            if ticker:
                await self.ticker_monitor.handle(replace(signal, text=ticker))
                return
            else:
                LOG.info("message_skipped message_id=%s reason=not_single_ticker", signal.message_id)
        else:
            routes = reported_routes(signal.text)
            if not routes:
                LOG.info("message_skipped message_id=%s reason=no_reported_route", signal.message_id)
            for ticker, (spread, long, short) in routes.items():
                if spread > self.reported_min_pct:
                    self._preview(signal, ticker, {
                        "basis": "sender_reported_spread",
                        "long": long,
                        "short": short,
                        "reported_open_spread_pct": str(spread),
                        "note": "按提醒原文模拟，盘口未复核",
                    })
                else:
                    LOG.info(
                        "token_skipped token=%s reason=reported_threshold_not_exceeded "
                        "reported_open_pct=%s threshold_pct=%s",
                        ticker, spread, self.reported_min_pct,
                    )
        self.store.finish(signal)

    def _preview(self, signal: Signal, ticker: str, details: dict) -> None:
        payload = json.dumps({
            "status": "preview",
            "message_id": signal.message_id,
            "token": ticker,
            "requested_card_state": REQUESTED_CARD_STATE,
            **details,
        }, ensure_ascii=False, separators=(",", ":"))
        if self.store.save(signal, ticker, payload):
            LOG.info("card_preview token=%s %s", ticker, payload)
