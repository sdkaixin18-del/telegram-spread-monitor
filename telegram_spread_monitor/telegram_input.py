from __future__ import annotations

import os
import re
from getpass import getpass
from pathlib import Path

from telethon import TelegramClient, events, types, utils

from .core import LOG, Monitor, Signal


class ConfigurationError(ValueError):
    pass


def telegram_client(session_path: Path) -> TelegramClient:
    api_id = os.environ.get("TG_API_ID", "")
    api_hash = os.environ.get("TG_API_HASH", "")
    if not api_id.isascii() or not api_id.isdecimal() or int(api_id) <= 0 or not re.fullmatch(
        r"[0-9a-fA-F]{32}", api_hash
    ):
        raise ConfigurationError("请在 .env 填写有效的 TG_API_ID 和 TG_API_HASH。")
    session_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    return TelegramClient(str(session_path), int(api_id), api_hash, sequential_updates=True)


async def login(client: TelegramClient) -> None:
    await client.start(
        phone=lambda: input("Telegram 手机号（含国家码）: "),
        code_callback=lambda: getpass("Telegram 验证码: "),
        password=lambda: getpass("Telegram 二步验证密码: "),
    )


async def discover(
    client: TelegramClient, group: str, sender_name: str, group_id: int | None, limit: int
) -> None:
    await login(client)
    groups = [
        dialog async for dialog in client.iter_dialogs()
        if dialog.is_group and (dialog.id == group_id if group_id is not None else dialog.name == group)
    ]
    if not groups:
        raise ConfigurationError("本账号会话中没找到该群。请确认已加入群且群名一致。")
    if len(groups) > 1:
        for dialog in groups:
            print(f"同名群 TG_CHAT_ID={dialog.id}")
        raise ConfigurationError("找到多个同名群。用 --group-id 指定其中一个后重试。")
    dialog = groups[0]
    print(f"TG_CHAT_ID={dialog.id}")
    matches: dict[int, str] = {}
    async for message in client.iter_messages(dialog.entity, limit=limit):
        if message.sender_id is None or message.sender_id <= 0 or message.sender_id in matches:
            continue
        sender = message.sender or await message.get_sender()
        if isinstance(sender, types.User) and utils.get_display_name(sender).casefold() == sender_name.casefold():
            matches[sender.id] = f"@{sender.username}" if sender.username else "无公开用户名"
    if not matches:
        raise ConfigurationError(f"最近 {limit} 条未找到同名用户。请让目标成员在群里发一句话后重试。")
    for sender_id, username in matches.items():
        print(f"TG_SENDER_ID={sender_id} ({username})")
    if len(matches) > 1:
        print("找到多个同名成员。请在 Telegram 核对头像、资料或用户名再选择。")


async def listen(client: TelegramClient, monitor: Monitor) -> None:
    @client.on(events.NewMessage)
    async def on_message(event: events.NewMessage.Event) -> None:
        if event.chat_id != monitor.chat_id or event.sender_id != monitor.sender_id:
            return
        signal = Signal(
            event.chat_id, event.sender_id, event.id, event.raw_text or "",
            int(event.date.timestamp() * 1000),
        )
        try:
            await monitor.handle(signal)
        except Exception as error:
            LOG.error("message_error message_id=%s type=%s", event.id, type(error).__name__)

    await login(client)
    LOG.info("listener_started mode=preview")
    await client.run_until_disconnected()
