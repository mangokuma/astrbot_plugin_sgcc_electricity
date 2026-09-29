"""缴费提醒：余额低于阈值时向配置的会话推送提醒（每天最多提醒一次）。"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# send_fn(text: str) -> Awaitable[None]
SendFunc = Callable[[str], Awaitable[None]]


async def maybe_remind(storage, threshold: float, send_fn: SendFunc) -> bool:
    """检查最近余额，低于阈值且今天未提醒过时发送提醒。

    Returns:
        是否发送了提醒。
    """
    last = storage.get_last_balance()
    if not last:
        return False
    balance = last.get("balance")
    if balance is None:
        return False
    if balance >= threshold:
        return False

    today = datetime.now().strftime("%Y-%m-%d")
    if storage.reminded_today(today):
        logger.info(f"今日已提醒过（余额 {balance} 元），跳过")
        return False

    text = (
        "⚡ 国网电费缴费提醒\n"
        f"当前电费余额: {balance:.2f} 元\n"
        f"已低于提醒阈值 {threshold:.2f} 元，请及时缴费，避免停电。\n"
        f"（数据时间: {last.get('ts', '未知')}）"
    )
    await send_fn(text)
    storage.mark_reminded(today, balance)
    logger.info(f"缴费提醒已发送（余额 {balance} 元 < 阈值 {threshold} 元）")
    return True


def reminder_disabled_reason(storage, threshold: float) -> Optional[str]:
    """返回不触发提醒的原因（用于日志），None 表示可能触发。"""
    last = storage.get_last_balance()
    if not last or last.get("balance") is None:
        return "无余额记录"
    if last["balance"] >= threshold:
        return f"余额 {last['balance']:.2f} 元不低于阈值 {threshold:.2f} 元"
    if storage.reminded_today(datetime.now().strftime("%Y-%m-%d")):
        return "今日已提醒"
    return None
