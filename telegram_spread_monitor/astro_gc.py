"""Gate CrossEx execution codes for the FF markets monitored by this project."""

from __future__ import annotations

import httpx

GC_VENUES = frozenset({"binance", "okx", "aster", "gate"})
GC_SYMBOLS_PATH = "/api/v4/crossex/rule/symbols"
GC_SYMBOLS_URL = "https://api.gateio.ws" + GC_SYMBOLS_PATH


def native_exchange(exchange: object) -> object:
    if isinstance(exchange, str) and exchange.startswith("gc-") and exchange[3:] in GC_VENUES:
        return exchange[3:]
    return exchange


def gc_symbols(pair: dict) -> tuple[str, ...]:
    return tuple(
        f"{exchange[3:].upper()}_FUTURE_{pair['name']}_USDT"
        for exchange in (pair["buyEx"], pair["sellEx"]) if exchange.startswith("gc-")
    )


def fetch_gc_catalogue(symbols: tuple[str, ...]) -> object:
    try:
        response = httpx.get(GC_SYMBOLS_URL, params={"symbols": ",".join(symbols)}, timeout=1.5)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError, OSError):
        return None


def gc_market_rejection(symbols: tuple[str, ...], rows: object) -> str | None:
    if not isinstance(rows, list):
        return "gc_catalogue_unavailable"
    for symbol in symbols:
        matches = [row for row in rows if isinstance(row, dict) and row.get("symbol") == symbol]
        if len(matches) != 1:
            return "gc_market_not_verified"
        if matches[0].get("tag") != "":
            return "gc_market_identity_changed"
        if matches[0].get("state") != "live":
            return "gc_market_not_live"
    return None
