from __future__ import annotations

import asyncio
import re
from time import monotonic

import httpx

from .core import LOG, Monitor, Signal, now_ms
from .message_preview import MessagePreviewMonitor


class BotApiError(ValueError):
    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class BotInput:
    def __init__(self, client: httpx.AsyncClient, token: str):
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token):
            raise BotApiError("请在本机 .env 填写 TG_BOT_TOKEN。")
        self.client = client
        self._base_url = f"https://api.telegram.org/bot{token}"

    async def _request(self, method: str, params: dict, wait: int = 0):
        # The token is in the request path. Report only known, sanitized errors.
        try:
            async with asyncio.timeout(wait + 10):
                response = await self.client.post(
                    f"{self._base_url}/{method}", json=params, timeout=wait + 8,
                )
        except (httpx.RequestError, TimeoutError):
            raise BotApiError(f"Telegram {method} 网络请求失败。", retry_after=3) from None
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if response.is_success and payload.get("ok") is True and "result" in payload:
            return payload["result"]
        code = payload.get("error_code") if response.is_success else response.status_code
        if code in (401, 404):
            raise BotApiError("Telegram 拒绝了机器人凭据，请核对 TG_BOT_TOKEN。")
        if code == 409:
            raise BotApiError("该机器人已有 webhook 或其他拉取进程；请用独立的测试机器人。")
        if code == 429:
            parameters = payload.get("parameters")
            delay = parameters.get("retry_after") if isinstance(parameters, dict) else None
            delay = delay if type(delay) is int and delay > 0 else 3
            raise BotApiError("Telegram 暂时限流。", retry_after=delay)
        if code in (500, 502, 503, 504):
            raise BotApiError(f"Telegram {method} 服务暂不可用。", retry_after=3)
        raise BotApiError(f"Telegram {method} 返回异常或机器人访问受限。")

    async def verify(self) -> None:
        result = await self._request("getMe", {})
        if not isinstance(result, dict) or result.get("is_bot") is not True:
            raise BotApiError("Telegram 没有确认该凭据属于机器人。")

    async def updates(self, offset: int | None, wait: int = 25) -> list[dict]:
        params = {"timeout": wait, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        items = await self._request("getUpdates", params, wait=wait)
        if not isinstance(items, list) or any(
            not isinstance(item, dict) or type(item.get("update_id")) is not int for item in items
        ):
            raise BotApiError("Telegram 返回了无法确认顺序的更新。")
        return sorted(items, key=lambda item: item["update_id"])


def signal_from_update(update: dict) -> Signal | None:
    message = update.get("message")
    if not isinstance(message, dict) or message.get("sender_chat") is not None:
        return None
    chat, sender = message.get("chat"), message.get("from")
    if not isinstance(chat, dict) or not isinstance(sender, dict):
        return None
    if chat.get("type") not in ("group", "supergroup"):
        return None
    chat_id, sender_id = chat.get("id"), sender.get("id")
    message_id, posted_at = message.get("message_id"), message.get("date")
    if any(type(value) is not int for value in (chat_id, sender_id, message_id, posted_at)):
        return None
    if chat_id >= 0 or min(sender_id, message_id, posted_at) <= 0:
        return None
    text = message.get("text") or message.get("caption")
    if not isinstance(text, str) or not text.strip():
        return None
    return Signal(chat_id, sender_id, message_id, text, posted_at * 1000)


async def discover_bot(
    bot: BotInput, group: str, sender_name: str, group_id: int | None, seconds: int,
) -> None:
    await bot.verify()
    LOG.info("bot_verified 等待目标群的新消息，验证窗口 %s 秒", seconds)
    deadline = monotonic() + seconds
    offset = None
    group_seen = False
    expected_name = sender_name.casefold()
    while monotonic() < deadline:
        wait = min(20, int(deadline - monotonic()))
        if wait <= 0:
            break
        batch = await bot.updates(offset, wait=wait)
        matches: set[tuple[int, int]] = set()
        for item in batch:
            signal = signal_from_update(item)
            if signal is None or now_ms() - signal.timestamp_ms > 120_000:
                continue
            message = item["message"]
            if group_id is not None:
                if signal.chat_id != group_id:
                    continue
            elif message["chat"].get("title") != group:
                continue
            group_seen = True
            sender = message["from"]
            display_name = " ".join(
                name for key in ("first_name", "last_name")
                if isinstance(name := sender.get(key), str) and name
            )
            username = sender.get("username")
            if expected_name.startswith("@"):
                matched = isinstance(username, str) and "@" + username.casefold() == expected_name
            else:
                matched = display_name.casefold() == expected_name
            if matched:
                matches.add((signal.chat_id, signal.sender_id))
        if len(matches) > 1:
            raise BotApiError("发现重名的群或发送者，请用 --group-id 或发送者 @用户名缩小范围。")
        if matches:
            chat_id, sender_id = matches.pop()
            print(f"TG_CHAT_ID={chat_id}")
            print(f"TG_SENDER_ID={sender_id}")
            LOG.info("bot_discovery_received 已收到目标发送者的群消息")
            return
        if batch:
            offset = batch[-1]["update_id"] + 1
    if group_seen:
        raise BotApiError("已收到目标群消息，但未收到指定发送者的新消息。请核对其名称和机器人互通设置。")
    raise BotApiError("已验证机器人凭据，但未收到目标群的新消息。请确认已加群并核对群隐私设置。")


async def listen_bot(bot: BotInput, monitor: Monitor | MessagePreviewMonitor) -> None:
    await bot.verify()
    source_only = isinstance(monitor, MessagePreviewMonitor)
    senders = (
        {monitor.sender_id, monitor.ticker_sender_id}
        if source_only else {monitor.sender_id}
    )
    if source_only:
        LOG.info(
            "bot_listener_started mode=message_preview sender_count=%s "
            "reported_min_pct=%s ticker_min_pct=%s",
            len(senders), monitor.reported_min_pct, monitor.rules.min_spread_pct,
        )
    else:
        LOG.info("bot_listener_started mode=preview min_spread_pct=%s", monitor.rules.min_spread_pct)
    offset = None
    while True:
        try:
            batch = await bot.updates(offset)
        except BotApiError as error:
            if error.retry_after is None:
                raise
            LOG.warning("bot_poll_retry seconds=%s %s", error.retry_after, error)
            await asyncio.sleep(error.retry_after)
            continue
        for update in batch:
            signal = signal_from_update(update)
            if signal is not None and signal.chat_id == monitor.chat_id and signal.sender_id in senders:
                LOG.info("bot_message_received message_id=%s", signal.message_id)
                await monitor.handle(signal)
            # Telegram confirms the update on the next poll. An interrupted
            # handler leaves the update unconfirmed and SQLite deduplicates it.
            offset = update["update_id"] + 1
