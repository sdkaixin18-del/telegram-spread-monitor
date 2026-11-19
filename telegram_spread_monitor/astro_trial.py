"""Convert a verified Telegram market preview to an FF card in an isolated Astro SDK trial."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from time import sleep
from typing import Callable, Protocol

from .astro_gc import GC_VENUES, fetch_gc_catalogue, gc_market_rejection, gc_symbols, native_exchange
from .core import now_ms

OPEN_PCT = Decimal("4")
TEST_ASSETS = frozenset({"BTC", "ETH", "QNT"})
VENUE_MAP = {
    "bg": "bitget",
    "bitget": "bitget",
    "bn": "binance",
    "binance": "binance",
    "okx": "okx",
    "aster": "aster",
    "gate": "gate",
}


class TrialError(ValueError):
    pass


class PairSdk(Protocol):
    def list_pairs(self) -> list[dict]: ...
    def add_pair(self, pair: dict) -> None: ...


@dataclass(frozen=True)
class IndexQuote:
    token: str
    venue: str
    price: Decimal
    timestamp_ms: int


def ff_venue(leg: object) -> str:
    if isinstance(leg, str):
        name, _, kind = leg.rpartition("-")
        venue = VENUE_MAP.get(name.casefold()) if kind.upper() == "F" else None
    elif isinstance(leg, dict) and isinstance(exchange := leg.get("exchange"), str):
        venue = VENUE_MAP.get(exchange.casefold())
    else:
        venue = None
    if venue is None:
        raise TrialError("unsupported_ff_leg")
    return venue


def validate_index_time(long_index: IndexQuote, short_index: IndexQuote) -> None:
    measured_at = now_ms()
    if (
        any(not -1000 <= measured_at - quote.timestamp_ms <= 3000 for quote in (long_index, short_index))
        or abs(long_index.timestamp_ms - short_index.timestamp_ms) > 2000
    ):
        raise TrialError("index_time_invalid")


def build_trial_pair(preview: dict, long_index: IndexQuote, short_index: IndexQuote, *, manual_observation: bool = False) -> dict:
    token = preview.get("token")
    verified_observation = manual_observation and token == "OPENAI" and preview.get("requested_card_state") == "paused"
    if not isinstance(token, str) or (token not in TEST_ASSETS and not verified_observation):
        # This isolated trial has unit/identity evidence only for its fixtures.
        raise TrialError("asset_not_verified_for_trial")
    requested_state = preview.get("requested_card_state")
    if preview.get("status") != "preview" or requested_state not in ("enabled", "paused"):
        raise TrialError("explicit_card_state_required")
    if preview.get("basis") == "sender_reported_spread" or preview.get("market") != "USDT 线性永续":
        raise TrialError("unsupported_preview_market")
    try:
        spread = Decimal(str(preview.get("gross_spread_pct")))
    except InvalidOperation as error:
        raise TrialError("invalid_opening_spread") from error
    manual_paused = manual_observation and token in ("OPENAI", "QNT") and requested_state == "paused"
    if not spread.is_finite() or (spread <= OPEN_PCT and not manual_paused):
        raise TrialError("opening_spread_not_above_4pct")
    buy, sell = ff_venue(preview.get("long")), ff_venue(preview.get("short"))
    if token == "OPENAI" and {buy, sell} != {"binance", "bitget"}:
        raise TrialError("openai_contract_identity_unverified")
    if token == "QNT" and not {buy, sell} <= {"binance", "gate"}:
        raise TrialError("qnt_contract_identity_unverified")
    if buy == sell:
        raise TrialError("same_exchange")
    if (long_index.token, long_index.venue, short_index.token, short_index.venue) != (token, buy, token, sell):
        raise TrialError("index_does_not_match_route")
    validate_index_time(long_index, short_index)
    if any(not quote.price.is_finite() or quote.price <= 0 for quote in (long_index, short_index)):
        raise TrialError("invalid_index_price")
    if short_index.price <= long_index.price:
        raise TrialError("short_index_not_above_long")
    pair = {
        "name": token,
        "type": "FF",
        "buyEx": buy,
        "sellEx": sell,
        "status": requested_state == "enabled",
        "disableOpen": requested_state == "paused",
        "disableClose": requested_state == "paused",
        "openPosition": "0.04",
        "closePosition": "0",
        "maxTradeUSDT": "10",
        "leverage": "2",
        "startTime": "0",
        "minNotional": "10",
        "maxNotional": "10",
    }
    if buy in GC_VENUES and sell in GC_VENUES:
        for leg, venue in ((preview["long"], buy), (preview["short"], sell)):
            symbol = {"okx": f"{token}-USDT-SWAP", "gate": f"{token}_USDT"}.get(venue, token + "USDT")
            if isinstance(leg, dict) and (leg.get("contract", symbol) != symbol or leg.get("tokens_per_price_unit", 1) != 1):
                raise TrialError("gc_market_symbol_mismatch")
        pair.update(buyEx=f"gc-{buy}", sellEx=f"gc-{sell}")
    if "aster" in (buy, sell):
        pair.update(independentLeverageSide="a" if buy == "aster" else "b", independentLeverage=pair["leverage"])
    return pair


IDENTITY = ("name", "type", "buyEx", "sellEx")


class TrialSaver:
    """A durable submission claim survives uncertain acknowledgements."""

    def __init__(
        self, sdk: PairSdk, db: sqlite3.Connection, wait: Callable[[float], None] = sleep,
        *, before_claim: Callable[[], None] | None = None, gc_catalogue: Callable[[tuple[str, ...]], object] = fetch_gc_catalogue,
    ):
        self.sdk, self.db, self.wait = sdk, db, wait
        self.gc_catalogue = gc_catalogue
        self.before_claim = before_claim
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS astro_trial_submissions (
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, token TEXT NOT NULL,
                buy TEXT NOT NULL, sell TEXT NOT NULL, card_id TEXT,
                PRIMARY KEY (chat_id, message_id, token),
                UNIQUE (token, buy, sell)
            )
        """)

    def save(
        self, chat_id: int, preview: dict, long_index: IndexQuote, short_index: IndexQuote,
        *, manual_observation: bool = False,
    ) -> tuple[str, str | None]:
        pair = build_trial_pair(preview, long_index, short_index, manual_observation=manual_observation)
        token = pair["name"]
        buy, sell = (native_exchange(pair[field]) for field in ("buyEx", "sellEx"))
        message_id = preview.get("message_id")
        if type(message_id) is not int or message_id <= 0:
            raise TrialError("message_id_required")
        known = self.db.execute(
            "SELECT card_id FROM astro_trial_submissions WHERE "
            "(chat_id=? AND message_id=? AND token=?) OR (token=? AND buy=? AND sell=?)",
            (chat_id, message_id, token, token, buy, sell),
        ).fetchall()
        if known:
            # Multiple claims or an unknown ACK are never permission to submit again.
            return "duplicate_or_pending", known[0][0] if len(known) == 1 else None

        if self._matching_pair(pair) is not None:
            return "already_exists", None
        symbols = gc_symbols(pair)
        if symbols and (reason := gc_market_rejection(symbols, self.gc_catalogue(symbols))):
            raise TrialError(reason)
        validate_index_time(long_index, short_index)
        if self.before_claim:
            self.before_claim()
        with self.db:
            claim = self.db.execute(
                "INSERT OR IGNORE INTO astro_trial_submissions VALUES (?, ?, ?, ?, ?, NULL)",
                (chat_id, message_id, token, buy, sell),
            )
            if claim.rowcount != 1:
                return "duplicate_or_pending", None
        try:
            self.sdk.add_pair(pair)
            self.wait(3.3)  # The SDK asks clients to wait after an add.
            saved = self._matching_pair(pair, exact=True)
            if saved is None or not isinstance(saved.get("id"), str) or not saved["id"]:
                return "unconfirmed", None
            for key in ("status", "disableOpen", "disableClose"):
                if saved.get(key) is not pair[key]:
                    return "unconfirmed", None
            numeric_fields = ("openPosition", "closePosition", "maxTradeUSDT", "maxNotional", "leverage")
            if any(Decimal(str(saved.get(key))) != Decimal(pair[key]) for key in numeric_fields):
                return "unconfirmed", None
            if "independentLeverageSide" in pair and (
                saved.get("independentLeverageSide") != pair["independentLeverageSide"]
                or Decimal(str(saved.get("independentLeverage"))) != Decimal(pair["independentLeverage"])
            ):
                return "unconfirmed", None
        except Exception:
            return "unconfirmed", None
        with self.db:
            self.db.execute(
                "UPDATE astro_trial_submissions SET card_id=? WHERE chat_id=? AND message_id=? AND token=?",
                (saved["id"], chat_id, message_id, token),
            )
        return f"saved_{preview['requested_card_state']}", saved["id"]

    def _matching_pair(self, pair: dict, *, exact: bool = False) -> dict | None:
        expected = tuple(pair[name] for name in IDENTITY)
        native = (*expected[:2], *(native_exchange(venue) for venue in expected[2:]))
        same = [
            item for item in self.sdk.list_pairs()
            if (item.get("name"), item.get("type"), native_exchange(item.get("buyEx")),
                native_exchange(item.get("sellEx"))) == native
        ]
        if len(same) > 1:
            raise TrialError("ambiguous_existing_route")
        if not same or (exact and tuple(same[0].get(name) for name in IDENTITY) != expected):
            return None
        return same[0]
