"""sgcc 包：95598.cn 数据抓取核心（同步 Playwright，须在独立线程中运行）。"""

from .client import SGCCClient, FetchResult

__all__ = ["SGCCClient", "FetchResult"]
