from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from itertools import permutations
from pathlib import Path
from time import time
from typing import Protocol

LOG = logging.getLogger("spread_monitor")
VENUES = ("BG", "BN", "OKX", "Aster", "Gate")
GROSS_NOTE = "开仓毛价差，未扣手续费和滑点"
REQUESTED_CARD_STATE = "paused"
# Price denominations of known exchange base symbols. A venue's contract size
# (e.g. Gate quanto_multiplier or OKX ctVal) is a different quantity.
BUNDLES = {
    "1000PEPE": ("PEPE", 1000),
    "1000SHIB": ("SHIB", 1000),
    "1000FLOKI": ("FLOKI", 1000),
    "1000BONK": ("BONK", 1000),
    "10000BONK": ("BONK", 10000),
    "1000RATS": ("RATS", 1000),
    "1000CAT": ("CAT", 1000),
    "1000SATS": ("SATS", 1000),
    "1000XEC": ("XEC", 1000),
    "1000LUNC": ("LUNC", 1000),
    "1000CHEEMS": ("CHEEMS", 1000),
    "1MCHEEMS": ("CHEEMS", 1000000),
    "1000000MOG": ("MOG", 1000000),
    "1MBABYDOGE": ("BABYDOGE", 1000000),
}
BASE = re.compile(r"[A-Z0-9]{1,32}\Z")
MESSAGE_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_$@#])(?P<dollar>\$)?(?P<ticker>[A-Za-z0-9]+)"
    r"(?P<pair>\s*[/_-]\s*USDT\b)?(?![A-Za-z0-9_])",
    re.IGNORECASE | re.ASCII,
)
LINK_OR_HANDLE = re.compile(
    r"(?:[A-Z][A-Z0-9+.-]*://|www\.|t\.me/)[^\s，。；、]+"
    r"|[A-Z0-9._+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|@[A-Z0-9_]+", re.IGNORECASE
)
VENUE_MARKET_LABEL = re.compile(
    r"(?<![A-Z0-9_@$])(?:Binance|BN|Bitget|BG|Bybit|OKX|Aster|Gate(?:\.io)?|Hyperliquid)"
    r"[-‐‑–—][FS](?![A-Z0-9_]|\s*[/_-]\s*USDT\b)",
    re.IGNORECASE | re.ASCII,
)


def now_ms() -> int:
    return int(time() * 1000)


def utc_text(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, UTC).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class Contract:
    venue: str
    symbol: str
    base: str

    @property
    def token(self) -> str:
        return BUNDLES.get(self.base, (self.base, 1))[0]

    @property
    def price_unit(self) -> int:
        return BUNDLES.get(self.base, (self.base, 1))[1]


@dataclass(frozen=True)
class Quote:
    contract: Contract
    raw_bid: Decimal
    raw_ask: Decimal
    timestamp_ms: int

    @property
    def bid(self) -> Decimal:
        return self.raw_bid / self.contract.price_unit

    @property
    def ask(self) -> Decimal:
        return self.raw_ask / self.contract.price_unit

    def details(self) -> dict:
        return {
            "exchange": self.contract.venue,
            "contract": self.contract.symbol,
            "bid": str(self.bid),
            "ask": str(self.ask),
            "raw_bid": str(self.raw_bid),
            "raw_ask": str(self.raw_ask),
            "tokens_per_price_unit": self.contract.price_unit,
            "quote_time_utc": utc_text(self.timestamp_ms),
        }


@dataclass(frozen=True)
class Route:
    long: Quote
    short: Quote

    @property
    def gross_pct(self) -> Decimal:
        return (self.short.bid - self.long.ask) / self.long.ask * 100

    @property
    def direction(self) -> str:
        return f"{self.long.contract.venue} 多 / {self.short.contract.venue} 空"


@dataclass(frozen=True)
class Signal:
    chat_id: int
    sender_id: int
    message_id: int
    text: str
    timestamp_ms: int


Catalog = dict[str, dict[str, Contract] | str]


class Market(Protocol):
    async def catalog(self) -> Catalog: ...

    async def quotes(self, token: str, catalog: Catalog) -> dict[str, Quote | str]: ...


def tokens_in(text: str, catalog: Catalog) -> list[str]:
    aliases = {}
    for contracts in catalog.values():
        if isinstance(contracts, dict):
            for contract in contracts.values():
                aliases[contract.token] = contract.token
    for alias, (token, _) in BUNDLES.items():
        if token in aliases:
            aliases[alias] = token
    found = {}
    message = VENUE_MARKET_LABEL.sub(" ", LINK_OR_HANDLE.sub(" ", text))
    only_symbol = message.strip()
    for match in MESSAGE_TOKEN.finditer(message):
        raw = match["ticker"]
        symbol = raw.upper()
        if match["pair"] or match["dollar"]:
            # A quoted pair already defines the base. For $BTCUSDT the joined
            # pair form also works when there is no exact token of that name.
            if not match["pair"] and symbol not in aliases and symbol.endswith("USDT"):
                symbol = symbol[:-4]
        elif symbol.endswith("USDT") and symbol not in aliases:
            symbol = symbol[:-4]
        elif not raw.isupper() and raw != only_symbol:
            continue
        if symbol in aliases:
            found[aliases[symbol]] = None
    return list(found)


def ranked_routes(quotes: list[Quote], max_skew_ms: int) -> list[Route]:
    routes = [
        Route(long, short)
        for long, short in permutations(quotes, 2)
        if long.contract.venue != short.contract.venue
        and long.contract.token == short.contract.token
        and abs(long.timestamp_ms - short.timestamp_ms) <= max_skew_ms
    ]
    return sorted(
        routes,
        key=lambda route: (-route.gross_pct, route.long.contract.venue, route.short.contract.venue),
    )


class PreviewStore:
    def __init__(self, path: Path | str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS previews (
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, token TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (chat_id, message_id, token)
            );
            CREATE TABLE IF NOT EXISTS processed_messages (
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                PRIMARY KEY (chat_id, message_id)
            );
        """)

    def close(self) -> None:
        self.db.close()

    def seen(self, signal: Signal) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM processed_messages WHERE chat_id=? AND message_id=?",
            (signal.chat_id, signal.message_id),
        ).fetchone()
        return row is not None

    def finish(self, signal: Signal) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO processed_messages VALUES (?, ?)",
                (signal.chat_id, signal.message_id),
            )

    def save(self, signal: Signal, token: str, payload: str) -> bool:
        with self.db:
            return self.db.execute(
                "INSERT OR IGNORE INTO previews VALUES (?, ?, ?, ?)",
                (signal.chat_id, signal.message_id, token, payload),
            ).rowcount == 1


@dataclass(frozen=True)
class Rules:
    min_spread_pct: Decimal = Decimal("0.10")
    max_quote_age_ms: int = 3000
    max_quote_skew_ms: int = 2000
    max_message_age_ms: int = 120_000


class Monitor:
    def __init__(
        self, market: Market, store: PreviewStore, rules: Rules, chat_id: int, sender_id: int
    ):
        self.market = market
        self.store = store
        self.rules = rules
        self.chat_id = chat_id
        self.sender_id = sender_id

    async def handle(self, signal: Signal) -> None:
        if signal.chat_id != self.chat_id or signal.sender_id != self.sender_id:
            return
        if self.store.seen(signal):
            LOG.info("duplicate_message message_id=%s", signal.message_id)
            return
        if now_ms() - signal.timestamp_ms > self.rules.max_message_age_ms:
            LOG.info("message_skipped message_id=%s reason=stale_message", signal.message_id)
            return
        if not signal.text.strip():
            return
        catalog = await self.market.catalog()
        for venue, contracts in catalog.items():
            if isinstance(contracts, str):
                LOG.warning("catalog_unavailable exchange=%s reason=%s", venue, contracts)
        if not any(isinstance(value, dict) for value in catalog.values()):
            LOG.warning("message_skipped message_id=%s reason=no_catalog", signal.message_id)
            return
        tokens = tokens_in(signal.text, catalog)
        LOG.info("tokens_identified message_id=%s tokens=%s", signal.message_id, ",".join(tokens) or "-")
        for token in tokens:
            if now_ms() - signal.timestamp_ms > self.rules.max_message_age_ms:
                LOG.info("token_skipped token=%s reason=stale_message", token)
                break
            await self.preview(signal, token, catalog)
        self.store.finish(signal)

    async def preview(self, signal: Signal, token: str, catalog: Catalog) -> None:
        results = await self.market.quotes(token, catalog)
        measured_at = now_ms()
        quotes: list[Quote] = []
        for venue, value in results.items():
            if isinstance(value, str):
                LOG.warning("quote_skipped token=%s exchange=%s reason=%s", token, venue, value)
                continue
            age_ms = measured_at - value.timestamp_ms
            if age_ms > self.rules.max_quote_age_ms or age_ms < -1000:
                reason = "stale_quote" if age_ms > self.rules.max_quote_age_ms else "future_quote"
                LOG.warning(
                    "quote_skipped token=%s exchange=%s reason=%s age_ms=%s %s",
                    token, venue, reason, age_ms, json.dumps(value.details(), ensure_ascii=False),
                )
                continue
            quotes.append(value)
            LOG.info("quote token=%s %s", token, json.dumps(value.details(), ensure_ascii=False))
        if len(quotes) < 2:
            LOG.info("token_skipped token=%s reason=insufficient_quotes valid=%s", token, len(quotes))
            return
        routes = ranked_routes(quotes, self.rules.max_quote_skew_ms)
        if not routes:
            LOG.info("token_skipped token=%s reason=quote_time_skew", token)
            return
        LOG.info(
            "spread_ranked token=%s %s",
            token, " | ".join(f"{route.direction} {route.gross_pct:+.4f}%" for route in routes),
        )
        best = routes[0]
        if best.gross_pct <= max(Decimal("0"), self.rules.min_spread_pct):
            LOG.info(
                "token_skipped token=%s reason=threshold_not_exceeded gross_pct=%s threshold_pct=%s",
                token, best.gross_pct, self.rules.min_spread_pct,
            )
            return
        card = {
            "status": "preview",
            "message_id": signal.message_id,
            "token": token,
            "market": "USDT 线性永续",
            "direction": best.direction,
            "long": best.long.details(),
            "short": best.short.details(),
            "gross_spread_pct": format(best.gross_pct.normalize(), "f"),
            "price_unit": "USDT / 1 " + token,
            "requested_card_state": REQUESTED_CARD_STATE,
            "note": GROSS_NOTE,
        }
        payload = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
        if self.store.save(signal, token, payload):
            LOG.info(
                "card_preview token=%s direction=%s gross_pct=%.2f%% %s",
                token, best.direction, best.gross_pct, payload,
            )
        else:
            LOG.info("duplicate_preview message_id=%s token=%s", signal.message_id, token)
