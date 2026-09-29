"""图表生成（matplotlib，中文字体内置，任何环境均渲染中文）。

- render_daily_chart: 近 N 天每日用电量柱状图
- render_bill_card:    /电费 指令的电费信息卡片

中文字体为 Noto Sans SC（SIL OFL 1.1，可自由分发）的子集，
常规/粗体两个字重以 base64 形式内置在 assets/ 下，
首次使用时解码到临时目录加载，无需宿主机安装任何中文字体。
"""

from __future__ import annotations

import base64
import logging
import os
import tempfile
from typing import Any, Optional

logger = logging.getLogger(__name__)

_ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")

# 内置字体子集（base64）：(文件名, 解码后的目标文件名)
_BUNDLED_FONTS = [
    ("NotoSansSC-400.ttf.b64", "astrbot_sgcc_noto_sans_sc_400.ttf"),
    ("NotoSansSC-700.ttf.b64", "astrbot_sgcc_noto_sans_sc_700.ttf"),
]


def _bundled_font_paths() -> list[str]:
    """把内置的 base64 字体解码到临时目录，返回全部 ttf 路径。"""
    paths: list[str] = []
    for b64_name, ttf_name in _BUNDLED_FONTS:
        src = os.path.join(_ASSETS_DIR, b64_name)
        if not os.path.isfile(src):
            continue
        dest = os.path.join(tempfile.gettempdir(), ttf_name)
        try:
            if not os.path.isfile(dest):
                with open(src, "rb") as f:
                    data = base64.b64decode(f.read())
                with open(dest, "wb") as f:
                    f.write(data)
            paths.append(dest)
        except Exception as e:
            logger.warning(f"解压内置中文字体 {b64_name} 失败: {e}")
    return paths


# Windows / Linux 常见中文字体候选（内置字体优先，保证无中文字体的容器也能出中文图）
_CJK_FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/wqy-microhei/wqy-microhei.ttc",
    r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑
    r"C:\Windows\Fonts\simhei.ttf",    # 黑体
    r"C:\Windows\Fonts\simsun.ttc",    # 宋体
]


def _candidate_fonts() -> list[str]:
    return _bundled_font_paths() + list(_CJK_FONT_CANDIDATES)


def _setup_cjk_font():
    """配置 matplotlib 中文字体，优先使用内置字体；返回是否成功。

    内置的 400/700 两个字重全部注册，粗体文字由 matplotlib 按字重匹配。
    """
    from matplotlib import font_manager

    candidates = _candidate_fonts()
    loaded_any = False
    font_name: Optional[str] = None
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            font_manager.fontManager.addfont(path)
            name = font_manager.FontProperties(fname=path).get_name()
            if font_name is None:
                font_name = name
            loaded_any = True
        except Exception as e:
            logger.warning(f"加载字体 {path} 失败: {e}")
    if loaded_any and font_name:
        import matplotlib

        matplotlib.rcParams["font.family"] = font_name
        matplotlib.rcParams["axes.unicode_minus"] = False
        return True
    logger.warning("未找到可用中文字体，图表文字可能显示为方块")
    return False


def render_daily_chart(rows: list[dict[str, Any]], days: int) -> Optional[bytes]:
    """生成近 N 天每日用电量柱状图，返回 PNG 字节；无数据返回 None。"""
    if not rows:
        return None

    import io

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    has_cjk = _setup_cjk_font()

    dates = [str(r.get("date", ""))[5:] for r in rows]  # 取 MM-DD
    usages = [float(r.get("total_usage") or 0) for r in rows]
    total = sum(usages)
    avg = total / len(usages) if usages else 0

    if has_cjk:
        title = f"近{days}天每日用电量（度）"
        subtitle = f"合计 {total:.1f} 度，日均 {avg:.2f} 度"
        xlabel = "日期"
        ylabel = "用电量（度）"
    else:
        title = f"Daily Electricity Usage ({days}d)"
        subtitle = f"Total {total:.1f} kWh, avg {avg:.2f} kWh/day"
        xlabel = "Date"
        ylabel = "kWh"

    fig, ax = plt.subplots(figsize=(max(8, len(rows) * 0.55), 4.5), dpi=150)
    bars = ax.bar(range(len(dates)), usages, color="#4C8BF5", edgecolor="#2f6fd6", width=0.65)
    ax.set_title(f"{title}\n{subtitle}", fontsize=13)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.set_xticks(range(len(dates)))
    ax.set_xticklabels(dates, rotation=45 if len(dates) > 10 else 0, ha="right" if len(dates) > 10 else "center")
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    ax.set_ylim(0, max(usages) * 1.2 if usages else 1)

    # 柱顶标注数值
    for bar, usage in zip(bars, usages):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(),
                f"{usage:.1f}", ha="center", va="bottom", fontsize=8)

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="PNG", bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def render_bill_card(summary: dict[str, Any]) -> Optional[bytes]:
    """生成 /电费 指令的电费信息卡片图，返回 PNG 字节。"""
    import io

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyBboxPatch

    has_cjk = _setup_cjk_font()

    def num(key: str) -> Optional[float]:
        v = summary.get(key)
        return float(v) if v is not None else None

    balance = num("balance")
    amount_due = num("amount_due")
    yearly_usage = num("yearly_usage")
    yearly_charge = num("yearly_charge")
    month_usage = num("month_usage")
    month_charge = num("month_charge")
    fetched_at = summary.get("fetched_at", "")

    if has_cjk:
        title = "国网电费"
        label_balance = "电费余额"
        label_due = "应交金额"
        label_yearly = "本年用电"
        label_month = "本月用电"
        unit_yuan, unit_kwh = "元", "度"
        label_time = "数据时间"
    else:
        title = "Electricity Bill"
        label_balance = "Balance"
        label_due = "Amount Due"
        label_yearly = "Year"
        label_month = "Month"
        unit_yuan, unit_kwh = "CNY", "kWh"
        label_time = "Updated"

    fig, ax = plt.subplots(figsize=(7.2, 4.6), dpi=150)
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 10)
    ax.axis("off")

    # 卡片背景
    card = FancyBboxPatch((0.3, 0.3), 9.4, 9.4,
                          boxstyle="round,pad=0.15,rounding_size=0.45",
                          facecolor="#F4F7FE", edgecolor="#D6E0F5", linewidth=1.5)
    ax.add_patch(card)

    # 顶部标题（不显示任何用户/户号信息）
    ax.text(0.8, 8.7, title, fontsize=17, fontweight="bold", color="#1F3B73",
            ha="left", va="center")

    # 余额大数字
    balance_color = "#E25C5C" if (balance is not None and balance < 20) else "#2F6FD6"
    balance_text = f"{balance:.2f}" if balance is not None else "--"
    ax.text(0.8, 6.5, balance_text, fontsize=34, fontweight="bold",
            color=balance_color, ha="left", va="center")
    ax.text(0.85, 5.55, label_balance, fontsize=11, color="#7A8BB0",
            ha="left", va="center")

    # 右侧应交金额
    due_text = f"{amount_due:.2f} {unit_yuan}" if amount_due is not None else "--"
    ax.text(9.2, 7.0, due_text, fontsize=15, fontweight="bold", color="#1F3B73",
            ha="right", va="center")
    ax.text(9.2, 6.3, label_due, fontsize=10.5, color="#7A8BB0",
            ha="right", va="center")

    # 分隔线
    ax.plot([0.8, 9.2], [5.0, 5.0], color="#D6E0F5", linewidth=1.2)

    # 本年 / 本月用电
    def fmt_usage(usage: Optional[float], charge: Optional[float]) -> str:
        if usage is None:
            return "--"
        if charge is not None:
            return f"{usage:.1f} {unit_kwh} / {charge:.2f} {unit_yuan}"
        return f"{usage:.1f} {unit_kwh}"

    ax.text(0.8, 3.9, label_yearly, fontsize=11, color="#7A8BB0", ha="left", va="center")
    ax.text(0.8, 3.25, fmt_usage(yearly_usage, yearly_charge),
            fontsize=13.5, fontweight="bold", color="#1F3B73", ha="left", va="center")

    ax.text(0.8, 2.15, label_month, fontsize=11, color="#7A8BB0", ha="left", va="center")
    ax.text(0.8, 1.5, fmt_usage(month_usage, month_charge),
            fontsize=13.5, fontweight="bold", color="#1F3B73", ha="left", va="center")

    # 底部数据时间
    ax.text(9.2, 0.85, f"{label_time}: {fetched_at}", fontsize=9, color="#9AA7C2",
            ha="right", va="center")

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="PNG", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return buf.getvalue()
