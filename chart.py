"""用电量统计图表生成（matplotlib，支持 CJK 字体自动探测）。"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Windows / Linux 常见中文字体候选（按优先级）
_CJK_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑
    r"C:\Windows\Fonts\simhei.ttf",    # 黑体
    r"C:\Windows\Fonts\simsun.ttc",    # 宋体
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/wqy-microhei/wqy-microhei.ttc",
]


def _setup_cjk_font():
    """配置 matplotlib 中文字体，找不到时返回 False（图中将退回英文）。"""
    from matplotlib import font_manager

    for path in _CJK_FONT_CANDIDATES:
        if os.path.isfile(path):
            try:
                font_manager.fontManager.addfont(path)
                font_name = font_manager.FontProperties(fname=path).get_name()
                import matplotlib

                matplotlib.rcParams["font.family"] = font_name
                matplotlib.rcParams["axes.unicode_minus"] = False
                return True
            except Exception as e:
                logger.warning(f"加载字体 {path} 失败: {e}")
    logger.warning("未找到中文字体，图表文字可能显示为方块")
    return False


def render_daily_chart(rows: list[dict[str, Any]], days: int,
                       user_id: str = "") -> Optional[bytes]:
    """生成近 N 天每日用电量柱状图，返回 PNG 字节；无数据返回 None。"""
    if not rows:
        return None

    import io

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    _setup_cjk_font()

    dates = [str(r.get("date", ""))[5:] for r in rows]  # 取 MM-DD
    usages = [float(r.get("total_usage") or 0) for r in rows]
    total = sum(usages)
    avg = total / len(usages) if usages else 0

    has_cjk = any(os.path.isfile(p) for p in _CJK_FONT_CANDIDATES)
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

    if user_id:
        fig.text(0.99, 0.01, f"户号 {user_id}", ha="right", va="bottom",
                 fontsize=8, color="#888888")

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="PNG", bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()
