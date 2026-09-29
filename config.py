"""插件配置读取与校验。

配置来自 AstrBot Web 管理页面的插件配置（metadata.yaml 中声明的 config_schema），
插件初始化时由框架注入为 dict。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List


DEFAULTS: dict[str, Any] = {
    "phone_number": "",
    "password": "",
    "llm_api_key": "",
    "llm_base_url": "https://api.siliconflow.cn/v1",
    "llm_model": "Qwen/Qwen3.5-35B-A3B",
    "admin_qq": "",
    "platform": "aiocqhttp",
    "remind_sessions": [],
    "remind_threshold": 5.0,
    "query_time": "07:00",
    "retention_days": 7,
    "login_fallback_qrcode": True,
    "qrcode_wait_minutes": 5,
    "browser_headless": True,
    "retry_wait_time": 10,
}


@dataclass
class PluginConfig:
    """类型化配置，未填项回落到默认值。"""

    raw: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str) -> Any:
        value = self.raw.get(key, DEFAULTS.get(key))
        return DEFAULTS.get(key) if value is None else value

    # ---- 常用配置属性 ----

    @property
    def phone_number(self) -> str:
        return str(self.get("phone_number")).strip()

    @property
    def password(self) -> str:
        return str(self.get("password")).strip()

    @property
    def llm_api_key(self) -> str:
        return str(self.get("llm_api_key")).strip()

    @property
    def llm_base_url(self) -> str:
        return str(self.get("llm_base_url")).strip()

    @property
    def llm_model(self) -> str:
        return str(self.get("llm_model")).strip()

    @property
    def admin_qqs(self) -> List[str]:
        raw = self.get("admin_qq") or ""
        return [x.strip() for x in str(raw).split(",") if x.strip()]

    @property
    def platform(self) -> str:
        return str(self.get("platform") or "aiocqhttp").strip()

    @property
    def remind_sessions(self) -> List[str]:
        sessions = self.get("remind_sessions") or []
        if isinstance(sessions, str):
            sessions = [s.strip() for s in sessions.split(",") if s.strip()]
        return [str(s).strip() for s in sessions if str(s).strip()]

    @property
    def remind_threshold(self) -> float:
        try:
            return float(self.get("remind_threshold"))
        except (TypeError, ValueError):
            return 5.0

    @property
    def query_time(self) -> str:
        text = str(self.get("query_time")).strip()
        return text if text else "07:00"

    @property
    def retention_days(self) -> int:
        try:
            days = int(self.get("retention_days"))
        except (TypeError, ValueError):
            return 7
        return days if days in (7, 30) else 7

    @property
    def login_fallback_qrcode(self) -> bool:
        return bool(self.get("login_fallback_qrcode"))

    @property
    def qrcode_wait_minutes(self) -> int:
        try:
            return max(1, int(self.get("qrcode_wait_minutes")))
        except (TypeError, ValueError):
            return 5

    @property
    def browser_headless(self) -> bool:
        return bool(self.get("browser_headless"))

    @property
    def retry_wait_time(self) -> int:
        try:
            return max(2, int(self.get("retry_wait_time")))
        except (TypeError, ValueError):
            return 10

    # ---- 派生逻辑 ----

    def is_admin(self, sender_id: str | int | None) -> bool:
        if sender_id is None:
            return False
        return str(sender_id) in self.admin_qqs

    def validate(self) -> list[str]:
        """返回缺失的必要配置项说明，空列表表示可运行。"""
        problems: list[str] = []
        if not self.phone_number:
            problems.append("未配置国网手机号 phone_number")
        if not self.password:
            problems.append("未配置国网登录密码 password")
        if not self.llm_api_key:
            problems.append("未配置大模型 API Key llm_api_key（验证码识别必需）")
        if not self.admin_qqs:
            problems.append("未配置管理员 QQ admin_qq（指令权限控制必需）")
        return problems
