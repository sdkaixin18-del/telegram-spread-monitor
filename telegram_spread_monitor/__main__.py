from __future__ import annotations

import argparse
import asyncio
import logging
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .core import LOG, VENUES, Catalog, Contract, Monitor, PreviewStore, Quote, Rules, Signal, now_ms


class DemoMarket:
    async def catalog(self) -> Catalog:
        return {
            "BG": {"PEPE": Contract("BG", "PEPEUSDT", "PEPE"), "BTC": Contract("BG", "BTCUSDT", "BTC")},
            "BN": {"PEPE": Contract("BN", "1000PEPEUSDT", "1000PEPE"), "BTC": Contract("BN", "BTCUSDT", "BTC")},
            "OKX": {"PEPE": Contract("OKX", "PEPE-USDT-SWAP", "PEPE")},
            "Aster": {"PEPE": Contract("Aster", "1000PEPEUSDT", "1000PEPE")},
            "Gate": {"PEPE": Contract("Gate", "PEPE_USDT", "PEPE")},
        }

    async def quotes(self, token: str, catalog: Catalog) -> dict[str, Quote | str]:
        now = now_ms()
        if token == "PEPE":
            return {
                "BG": Quote(catalog["BG"][token], Decimal("0.0999"), Decimal("0.1000"), now - 120),
                "BN": Quote(catalog["BN"][token], Decimal("101.5"), Decimal("101.6"), now - 90),
                "OKX": Quote(catalog["OKX"][token], Decimal("0.1005"), Decimal("0.1007"), now - 50),
                "Aster": "timeout",
                "Gate": Quote(catalog["Gate"][token], Decimal("0.2500"), Decimal("0.2600"), now - 9000),
            }
        return {
            "BG": Quote(catalog["BG"][token], Decimal("65000.0"), Decimal("65000.1"), now - 100),
            "BN": "timeout",
            **{venue: "no_live_usdt_perpetual" for venue in VENUES[2:]},
        }


async def demo() -> None:
    store = PreviewStore(":memory:")
    try:
        monitor = Monitor(DemoMarket(), store, Rules(), -10012345, 12345)
        signal = Signal(-10012345, 12345, 42, "看 $PEPE / 1000PEPEUSDT，BTC，$BTC，BTC/USDT hello", now_ms())
        LOG.info("demo_started mode=simulation")
        await monitor.handle(signal)
        await monitor.handle(signal)
    finally:
        store.close()


def integer_setting(name: str, default: int | None, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, "" if default is None else str(default)))
        if minimum <= value <= maximum:
            return value
    except ValueError:
        pass
    raise ValueError(f"{name} 必须为 {minimum} 至 {maximum} 之间的整数。")


def decimal_setting(name: str, default: str, minimum: str, maximum: str) -> Decimal:
    try:
        value = Decimal(os.getenv(name, default))
        if value.is_finite() and Decimal(minimum) <= value <= Decimal(maximum):
            return value
    except InvalidOperation:
        pass
    raise ValueError(f"{name} 必须为 {minimum} 至 {maximum} 之间的有限数。")


def rules_from_env() -> Rules:
    return Rules(
        min_spread_pct=decimal_setting("MIN_SPREAD_PCT", "0.10", "0", "10000"),
        max_quote_age_ms=integer_setting("MAX_QUOTE_AGE_MS", 3000, 1, 3000),
        max_quote_skew_ms=integer_setting("MAX_QUOTE_SKEW_MS", 2000, 1, 2000),
        max_message_age_ms=1000 * integer_setting("MAX_MESSAGE_AGE_SECONDS", 120, 1, 3600),
    )


async def run(args: argparse.Namespace) -> None:
    if args.command == "demo":
        await demo()
        return
    from dotenv import load_dotenv

    dotenv_path = args.env.expanduser().resolve()
    load_dotenv(dotenv_path, interpolate=False)

    watching = args.command in {"listen", "listen-bot"}
    if watching:
        chat_id = integer_setting("TG_CHAT_ID", None, -(2**63), -1)
        # Existing installs used TG_SENDER_ID for the alert bot and the other
        # setting for the member. Keep the member as the sole source on upgrade.
        sender_name = "TG_TICKER_SENDER_ID" if os.getenv("TG_TICKER_SENDER_ID", "").strip() else "TG_SENDER_ID"
        sender_id = integer_setting(sender_name, None, 1, 2**63 - 1)

    if args.command in {"listen", "discover"}:
        from .telegram_input import discover, listen, telegram_client

        session_path = dotenv_path.parent / Path(
            os.getenv("TG_SESSION_PATH") or ".local/telegram"
        ).expanduser()
        tg = telegram_client(session_path)
        if args.command == "discover":
            try:
                await discover(tg, args.group, args.sender, args.group_id, args.limit)
            finally:
                await tg.disconnect()
            return

    import httpx

    if args.command == "bot-discover":
        from .bot_input import BotInput, discover_bot

        async with httpx.AsyncClient(follow_redirects=False) as client:
            bot = BotInput(client, os.getenv("TG_BOT_TOKEN", ""))
            await discover_bot(bot, args.group, args.sender, args.group_id, args.wait)
        return

    from .market import PublicMarket

    rules = rules_from_env()
    request_timeout = float(decimal_setting("HTTP_TIMEOUT_SECONDS", "2", "0.1", "15"))
    catalog_ttl = integer_setting("CONTRACT_CACHE_SECONDS", 300, 1, 3600)
    state_path: Path | str = ":memory:"
    if watching:
        state_path = dotenv_path.parent / Path(
            os.getenv("STATE_DB_PATH") or ".local/monitor.sqlite3"
        ).expanduser()
    else:
        chat_id, sender_id = -10012345, 12345
    store = PreviewStore(state_path)
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": "telegram-spread-monitor/1.0", "Cache-Control": "no-cache"},
            follow_redirects=False,
        ) as client:
            market = PublicMarket(client, timeout=request_timeout, catalog_ttl=catalog_ttl)
            monitor = Monitor(market, store, rules, chat_id, sender_id)
            if args.command == "listen":
                try:
                    await listen(tg, monitor)
                finally:
                    await tg.disconnect()
            elif args.command == "listen-bot":
                from .bot_input import BotInput, listen_bot

                bot = BotInput(client, os.getenv("TG_BOT_TOKEN", ""))
                await listen_bot(bot, monitor)
            else:
                LOG.info("market_check_started mode=public_quotes")
                await monitor.handle(Signal(chat_id, sender_id, 1, args.text, now_ms()))
    finally:
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Telegram 代币价差监控")
    parser.add_argument(
        "--env", type=Path, default=Path(__file__).resolve().parents[1] / ".env", help="本机 .env 路径",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo", help="离线模拟消息、盘口和卡片预览")
    commands.add_parser("listen", help="登录 Telegram 监听并输出预览")
    bot_listen = commands.add_parser("listen-bot", help="用 Telegram Bot API 监听并输出预览")
    bot_listen.add_argument("--message-preview", action="store_true", help=argparse.SUPPRESS)
    bot_ids = commands.add_parser("bot-discover", help="从机器人收到的新群消息获取 ID")
    bot_group = bot_ids.add_mutually_exclusive_group(required=True)
    bot_group.add_argument("--group", help="目标群显示名")
    bot_group.add_argument("--group-id", type=int, help="已知的目标群负数 ID")
    bot_ids.add_argument("--sender", required=True, help="发送者显示名或 @用户名")
    bot_ids.add_argument("--wait", type=int, choices=range(5, 301), metavar="5–300", default=60)
    ids = commands.add_parser("discover", help="从目标群聊天记录获取 Telegram ID")
    user_group = ids.add_mutually_exclusive_group(required=True)
    user_group.add_argument("--group", help="当前显示的 Telegram 群名")
    user_group.add_argument("--group-id", type=int, help="有重名群时可指定带负号的群 ID")
    ids.add_argument("--sender", required=True, help="当前显示的成员名")
    ids.add_argument("--limit", type=int, choices=range(1, 1001), metavar="1–1000", default=200)
    check = commands.add_parser("quotes", help="直接查询公开盘口，无需登录 Telegram")
    check.add_argument("text", help="代币代码或待解析的测试消息")
    args = parser.parse_args()

    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except ValueError as error:
        LOG.error("配置错误：%s", error)
        return 2
    except ModuleNotFoundError:
        LOG.error("缺少依赖，请安装 requirements.txt。")
        return 2
    except Exception as error:
        LOG.error("运行失败 type=%s", type(error).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
