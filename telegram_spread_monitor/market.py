from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation
from time import monotonic
from typing import Any

import httpx

from .core import BASE, VENUES, Catalog, Contract, Quote

HOSTS = {
    "BG": "https://api.bitget.com",
    "BN": "https://fapi.binance.com",
    "OKX": "https://www.okx.com",
    "Aster": "https://fapi.asterdex.com",
    "Gate": "https://api.gateio.ws",
}
FAPI = {"BN": "/fapi/v1", "Aster": "/fapi/v3"}
BITGET_TYPE = {"productType": "USDT-FUTURES"}
CATALOG_ENDPOINTS = {
    "BG": ("/api/v2/mix/market/contracts", BITGET_TYPE),
    "BN": ("/fapi/v1/exchangeInfo", {}),
    "OKX": ("/api/v5/public/instruments", {"instType": "SWAP"}),
    "Aster": ("/fapi/v3/exchangeInfo", {}),
    "Gate": ("/api/v4/futures/usdt/contracts", {}),
}


class MarketResponseError(ValueError):
    pass


def error_reason(error: Exception) -> str:
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(error, httpx.HTTPStatusError):
        return f"http_{error.response.status_code}"
    if isinstance(error, httpx.RequestError):
        return f"network_{type(error).__name__}"
    if isinstance(error, MarketResponseError):
        return str(error)
    return "invalid_response"


def rows_for(venue: str, payload: Any) -> list:
    if venue in FAPI:
        rows = payload["symbols"]
    elif venue == "Gate":
        rows = payload
    else:
        success = "00000" if venue == "BG" else "0"
        if payload["code"] != success:
            raise MarketResponseError("venue_rejected")
        rows = payload["data"]
    if not isinstance(rows, list):
        raise MarketResponseError("invalid_response")
    return rows


def catalog_contracts(venue: str, payload: Any) -> dict[str, Contract]:
    selected: dict[str, Contract] = {}
    for row in rows_for(venue, payload):
        if venue in FAPI:
            if (
                row.get("status") != "TRADING"
                or row.get("contractType") != "PERPETUAL"
                or row.get("quoteAsset") != "USDT"
                or row.get("marginAsset") != "USDT"
            ):
                continue
            base, symbol = row["baseAsset"], row["symbol"]
            if symbol != base + "USDT":
                continue
        elif venue == "BG":
            if (
                row.get("symbolStatus") != "normal"
                or row.get("symbolType") != "perpetual"
                or row.get("quoteCoin") != "USDT"
                or "USDT" not in (row.get("supportMarginCoins") or [])
            ):
                continue
            base, symbol = row["baseCoin"], row["symbol"]
            if symbol != base + "USDT":
                continue
        elif venue == "OKX":
            if (
                row.get("state") != "live"
                or row.get("ctType") != "linear"
                or row.get("instType") != "SWAP"
                or row.get("settleCcy") != "USDT"
            ):
                continue
            underlying = row["uly"]
            symbol = row["instId"]
            if not underlying.endswith("-USDT") or symbol != underlying + "-SWAP":
                continue
            base = underlying.removesuffix("-USDT")
            if row.get("ctValCcy") != base:
                continue
        else:
            if (
                row.get("status") != "trading"
                or row.get("in_delisting") is not False
                or row.get("type") != "direct"
            ):
                continue
            symbol = row["name"]
            if not symbol.endswith("_USDT"):
                continue
            base = symbol.removesuffix("_USDT")
        if not BASE.fullmatch(base) or base.isdigit():
            continue
        contract = Contract(venue, symbol, base)
        previous = selected.get(contract.token)
        # Prefer the market quoted per single token, then the smaller bundle.
        if previous is None or contract.price_unit < previous.price_unit:
            selected[contract.token] = contract
    if not selected:
        raise MarketResponseError("empty_catalog")
    return selected


def orderbook_request(contract: Contract) -> tuple[str, dict]:
    venue, symbol = contract.venue, contract.symbol
    if venue in FAPI:
        return FAPI[venue] + "/ticker/bookTicker", {"symbol": symbol}
    if venue == "BG":
        return "/api/v2/mix/market/ticker", {**BITGET_TYPE, "symbol": symbol}
    if venue == "OKX":
        return "/api/v5/market/books", {"instId": symbol, "sz": 1}
    return "/api/v4/futures/usdt/order_book", {"contract": symbol, "limit": 1}


def parse_quote(contract: Contract, payload: Any) -> Quote:
    venue = contract.venue
    if venue in FAPI:
        if payload.get("symbol") != contract.symbol:
            raise MarketResponseError("symbol_mismatch_or_venue_error")
        bid, ask = payload["bidPrice"], payload["askPrice"]
        bid_size, ask_size, timestamp = payload["bidQty"], payload["askQty"], payload["time"]
    elif venue == "BG":
        if payload.get("code") != "00000":
            raise MarketResponseError("venue_rejected")
        (row,) = payload["data"]
        if row.get("symbol") != contract.symbol:
            raise MarketResponseError("symbol_mismatch")
        bid, ask = row["bidPr"], row["askPr"]
        bid_size, ask_size, timestamp = row["bidSz"], row["askSz"], row["ts"]
    elif venue == "OKX":
        if payload.get("code") != "0":
            raise MarketResponseError("venue_rejected")
        (row,) = payload["data"]
        if not row["bids"] or not row["asks"]:
            raise MarketResponseError("empty_book")
        bid, bid_size = row["bids"][0][:2]
        ask, ask_size = row["asks"][0][:2]
        timestamp = row["ts"]
    else:
        if not payload["bids"] or not payload["asks"]:
            raise MarketResponseError("empty_book")
        bid_row, ask_row = payload["bids"][0], payload["asks"][0]
        bid, ask = bid_row["p"], ask_row["p"]
        bid_size, ask_size = bid_row["s"], ask_row["s"]
        # 'current' is when Gate generated the HTTP response. 'update' is
        # the exchange time of the book and can be much older.
        timestamp = Decimal(str(payload["update"])) * 1000
        bid_size = abs(Decimal(str(bid_size)))
        ask_size = abs(Decimal(str(ask_size)))
    bid, ask, bid_size, ask_size = (
        Decimal(str(value)) for value in (bid, ask, bid_size, ask_size)
    )
    if any(not value.is_finite() or value <= 0 for value in (bid, ask, bid_size, ask_size)):
        raise MarketResponseError("invalid_price_or_size")
    if bid > ask:
        raise MarketResponseError("crossed_book")
    if not Decimal("1000000000000") <= Decimal(str(timestamp)) < Decimal("10000000000000"):
        raise MarketResponseError("invalid_exchange_time")
    return Quote(contract, bid, ask, int(Decimal(str(timestamp))))


MARKET_ERRORS = (
    httpx.HTTPError, TimeoutError, AttributeError, KeyError, IndexError,
    TypeError, ValueError, InvalidOperation, OverflowError,
)


class PublicMarket:
    def __init__(self, client: httpx.AsyncClient, timeout: float, catalog_ttl: int):
        self.client = client
        self.timeout = timeout
        self.catalog_ttl = catalog_ttl
        self.cached: dict[str, tuple[float, dict[str, Contract] | str]] = {}

    async def get(self, venue: str, path: str, params: dict) -> Any:
        async with asyncio.timeout(self.timeout):
            response = await self.client.get(HOSTS[venue] + path, params=params)
            response.raise_for_status()
            return response.json()

    async def venue_catalog(self, venue: str) -> dict[str, Contract] | str:
        cached = self.cached.get(venue)
        if cached is not None and monotonic() < cached[0]:
            return cached[1]
        try:
            path, params = CATALOG_ENDPOINTS[venue]
            data = catalog_contracts(venue, await self.get(venue, path, params))
        except MARKET_ERRORS as error:
            # Retry transient failures sooner than a successful list refresh.
            reason = error_reason(error)
            self.cached[venue] = (monotonic() + min(self.catalog_ttl, 10), reason)
            return reason
        self.cached[venue] = (monotonic() + self.catalog_ttl, data)
        return data

    async def catalog(self) -> Catalog:
        results = await asyncio.gather(*(self.venue_catalog(venue) for venue in VENUES))
        return dict(zip(VENUES, results, strict=True))

    async def venue_quote(self, token: str, catalog: Catalog, venue: str) -> Quote | str:
        contracts = catalog[venue]
        if isinstance(contracts, str):
            return f"catalog_{contracts}"
        contract = contracts.get(token)
        if contract is None:
            return "no_live_usdt_perpetual"
        try:
            path, params = orderbook_request(contract)
            return parse_quote(contract, await self.get(venue, path, params))
        except MARKET_ERRORS as error:
            return error_reason(error)

    async def quotes(self, token: str, catalog: Catalog) -> dict[str, Quote | str]:
        results = await asyncio.gather(*(self.venue_quote(token, catalog, venue) for venue in VENUES))
        return dict(zip(VENUES, results, strict=True))
