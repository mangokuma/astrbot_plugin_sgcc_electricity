"""国网电费查询 AstrBot 插件。

指令：
- /电费            查看最近一次抓取的电费信息卡片（仅管理员）
- /用电统计 [7|30] 近 7/30 天每日用电量图表（默认 7 天）
- /电费更新        手动触发一次抓取（有冷却时间）

定时任务：每天 query_time 自动抓取一次，余额低于阈值时向配置的会话推送提醒。
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timedelta

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

import astrbot.api.message_components as Comp

from .chart import render_bill_card, render_daily_chart
from .config import PluginConfig
from .reminder import maybe_remind
from .sgcc.client import FetchResult, SGCCClient, result_to_json
from .storage import Storage

MANUAL_UPDATE_COOLDOWN = timedelta(minutes=30)
NO_PERMISSION = "⛔ 仅管理员可使用该指令。"


class SGCCPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.cfg = PluginConfig(config or {})
        data_dir = os.path.join(
            get_astrbot_data_path(), "plugin_data", "astrbot_plugin_sgcc_electricity")
        self.storage = Storage(data_dir)
        self._fetch_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

        problems = self.cfg.validate()
        if problems:
            logger.warning("sgcc_electricity 配置不完整：" + "；".join(problems))
        else:
            logger.info("sgcc_electricity 插件已加载")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def initialize(self):
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._scheduler_loop())
        logger.info(f"每日自动抓取任务已注册，执行时间 {self.cfg.query_time}")

    async def terminate(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.storage.close()

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------

    @filter.command("电费")
    async def cmd_bill(self, event: AstrMessageEvent):
        if not self.cfg.is_admin(event.get_sender_id()):
            yield event.plain_result(NO_PERMISSION)
            return

        problems = self.cfg.validate()
        if problems:
            yield event.plain_result("⚠️ 插件配置不完整：\n" + "\n".join(f"- {p}" for p in problems))
            return

        summary = self.storage.load_summary()
        if not summary:
            yield event.plain_result("暂无数据，请先使用 /电费更新 抓取一次。")
            return

        png = render_bill_card(summary)
        if png is None:
            yield event.plain_result("电费卡片生成失败，请查看日志。")
            return
        yield event.chain_result([Comp.Image.fromBytes(png)])

    @filter.command("用电统计")
    async def cmd_usage(self, event: AstrMessageEvent):
        if not self.cfg.is_admin(event.get_sender_id()):
            yield event.plain_result(NO_PERMISSION)
            return

        text = event.message_str or ""
        match = re.search(r"(\d+)", text)
        days = int(match.group(1)) if match else 7
        if days not in (7, 30):
            yield event.plain_result("用法: /用电统计 [7|30]，不带参数默认近 7 天。")
            return

        rows = self.storage.get_daily(days)
        if not rows:
            # 本地无数据，自动触发一次抓取
            yield event.plain_result("暂无历史数据，正在为你抓取，请稍候（约 1-2 分钟）...")
            result = await self._do_fetch(is_manual=True)
            if result is None:
                yield event.plain_result("抓取失败，请查看日志或稍后重试。")
                return
            rows = self.storage.get_daily(days)

        if not rows:
            yield event.plain_result(
                f"未能获取到每日用电量数据。注意：未签约智能交费的账号仅支持近 7 天查询。")
            return

        png = render_daily_chart(rows, days)
        if png is None:
            yield event.plain_result("图表生成失败。")
            return

        note = ""
        if len(rows) < days:
            note = (f"\n（仅获取到 {len(rows)} 天数据：未签约智能交费的账号"
                    f"国网仅提供近 7 天每日明细）")

        yield event.chain_result([
            Comp.Plain(f"近 {len(rows)} 天每日用电量统计：{note}"),
            Comp.Image.fromBytes(png),
        ])

    @filter.command("电费更新")
    async def cmd_update(self, event: AstrMessageEvent):
        if not self.cfg.is_admin(event.get_sender_id()):
            yield event.plain_result(NO_PERMISSION)
            return

        problems = self.cfg.validate()
        if problems:
            yield event.plain_result("⚠️ 插件配置不完整：\n" + "\n".join(f"- {p}" for p in problems))
            return

        # 冷却保护
        last_raw = self.storage.get_kv("last_manual_fetch")
        if last_raw:
            try:
                last_dt = datetime.fromisoformat(last_raw)
                remaining = MANUAL_UPDATE_COOLDOWN - (datetime.now() - last_dt)
                if remaining.total_seconds() > 0:
                    mins = int(remaining.total_seconds() // 60) + 1
                    yield event.plain_result(
                        f"⏳ 更新太频繁，请 {mins} 分钟后再试（频繁登录会触发国网风控）。")
                    return
            except ValueError:
                pass

        yield event.plain_result("⏳ 开始抓取国网数据（登录 + 验证码识别约需 1-3 分钟）...")
        result = await self._do_fetch(is_manual=True)
        if result is None:
            yield event.plain_result("❌ 抓取失败，请查看 AstrBot 日志排查原因。")
            return

        self.storage.set_kv("last_manual_fetch", datetime.now().isoformat())
        yield event.plain_result("✅ 抓取完成\n" + result.summary_text())

    # ------------------------------------------------------------------
    # 抓取与定时任务
    # ------------------------------------------------------------------

    def _build_client(self) -> SGCCClient:
        return SGCCClient(
            phone=self.cfg.phone_number,
            password=self.cfg.password,
            llm_api_key=self.cfg.llm_api_key,
            llm_base_url=self.cfg.llm_base_url,
            llm_model=self.cfg.llm_model,
            data_dir=os.path.join(
                get_astrbot_data_path(), "plugin_data", "astrbot_plugin_sgcc_electricity"),
            headless=self.cfg.browser_headless,
            retry_wait_time=self.cfg.retry_wait_time,
            login_fallback_qrcode=self.cfg.login_fallback_qrcode,
            qrcode_wait_minutes=self.cfg.qrcode_wait_minutes,
            qr_callback=self._on_qrcode,
        )

    def _on_qrcode(self, png_bytes: bytes):
        """浏览器线程中的回调：把二维码转发到管理员私聊。"""
        if self._loop is None:
            return

        async def _send():
            chain = MessageChain([
                Comp.Plain("🔐 国网密码登录失败，请使用国网 App 扫码登录："),
                Comp.Image.fromBytes(png_bytes),
            ])
            await self._send_to_sessions(self._admin_sessions(), chain)

        asyncio.run_coroutine_threadsafe(_send(), self._loop)

    def _admin_sessions(self) -> list[str]:
        return [
            f"{self.cfg.platform}:FriendMessage:{qq}"
            for qq in self.cfg.admin_qqs
        ]

    async def _send_to_sessions(self, sessions: list[str], chain: MessageChain):
        for umo in sessions:
            try:
                await self.context.send_message(umo, chain)
            except Exception as e:
                logger.error(f"向会话 {umo} 发送消息失败: {e}")

    async def _do_fetch(self, is_manual: bool = False) -> FetchResult | None:
        """执行一次抓取并落库。失败返回 None（已记录日志）。"""
        if self._fetch_lock.locked():
            logger.info("已有抓取任务进行中，跳过本次请求")
            return None

        async with self._fetch_lock:
            client = self._build_client()
            try:
                result: FetchResult = await asyncio.to_thread(client.fetch)
            except RuntimeError as e:
                # LLM 配置错误等不可恢复错误
                logger.error(f"抓取失败（不可恢复）: {e}")
                return None
            except Exception as e:
                logger.error(f"抓取失败: {e}")
                if "shared libraries" in str(e) or "libnspr4" in str(e):
                    logger.error(
                        "浏览器缺少系统依赖库。Docker 环境请在容器内执行: "
                        "python -m playwright install-deps chromium"
                        "（或 apt-get install -y libnspr4 libnss3 libatk1.0-0 libatk-bridge2.0-0 "
                        "libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 "
                        "libgbm1 libasound2 libpango-1.0-0 libpangocairo-1.0-0 libcairo2）"
                    )
                return None

            # 落库
            if result.daily:
                self.storage.upsert_daily(result.daily)
                self.storage.cleanup_daily(retention_days=180)
            self.storage.insert_balance(result.balance, result.amount_due, result.fetched_at)
            self.storage.save_summary(result_to_json(result))

            if result.need_login:
                logger.info("本次抓取发生了重新登录，请注意登录频率")

            # 缴费提醒（仅自动抓取时触发，手动更新不重复打扰）
            if not is_manual:
                await self._run_reminder()
            return result

    async def _run_reminder(self):
        sessions = self.cfg.remind_sessions
        if not sessions:
            logger.info("未配置 remind_sessions，跳过缴费提醒")
            return

        async def _send(text: str):
            await self._send_to_sessions(sessions, MessageChain([Comp.Plain(text)]))

        try:
            await maybe_remind(self.storage, self.cfg.remind_threshold, _send)
        except Exception as e:
            logger.error(f"缴费提醒发送失败: {e}")

    async def _scheduler_loop(self):
        """每日定时抓取。"""
        while True:
            wait_seconds = self._seconds_until_next_run()
            logger.info(f"距离下次自动抓取还有 {wait_seconds / 3600:.1f} 小时")
            await asyncio.sleep(wait_seconds)
            problems = self.cfg.validate()
            if problems:
                logger.warning("配置不完整，跳过本次自动抓取：" + "；".join(problems))
                continue
            logger.info("开始每日自动抓取")
            await self._do_fetch(is_manual=False)

    def _seconds_until_next_run(self) -> float:
        try:
            hour, minute = self.cfg.query_time.split(":")
            target = datetime.now().replace(hour=int(hour), minute=int(minute),
                                            second=0, microsecond=0)
        except ValueError:
            target = datetime.now().replace(hour=7, minute=0, second=0, microsecond=0)
        if target <= datetime.now():
            target += timedelta(days=1)
        return (target - datetime.now()).total_seconds()
