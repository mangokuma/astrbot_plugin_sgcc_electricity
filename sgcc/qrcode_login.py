"""扫码登录兜底：密码登录失败时，切换到扫码登录并把二维码推送给管理员。

本文件为原创实现，思路参考 ARC-MX/sgcc_electricity_new（Apache-2.0）的
LOGIN_FALLBACK=qrcode 方案：https://github.com/ARC-MX/sgcc_electricity_new

二维码通过回调函数交给插件层发送（插件层再转给 AstrBot 会话），
本模块只负责浏览器侧：切换 tab、截取二维码、等待扫码结果。
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from .const import LOGIN_URL

logger = logging.getLogger(__name__)

# 扫码登录 tab 的可能选择器（优先按文字匹配，失败时按位置兜底）
QR_TAB_TEXT_XPATH = "//*[@id='login_box']//span[normalize-space()='扫码登录']"
QR_TAB_INDEX_XPATH = '//*[@id="login_box"]/div[1]/div[1]/div[1]/span'


def login_with_qrcode(page,
                      qr_callback: Callable[[bytes], None],
                      wait_minutes: int = 5,
                      poll_interval: int = 5) -> bool:
    """切换到扫码登录，推送二维码并等待扫码。

    Args:
        page: Playwright 页面对象（当前应停留在登录页）。
        qr_callback: 收到二维码 PNG 字节时调用（可多次调用，如刷新后换新码）。
        wait_minutes: 最长等待分钟数。
        poll_interval: 轮询扫码结果的间隔秒数。

    Returns:
        扫码登录成功返回 True，超时/失败返回 False。
    """
    try:
        # 1. 切换到扫码登录 tab
        tab = page.query_selector("xpath=" + QR_TAB_TEXT_XPATH)
        if tab is None:
            tab = page.query_selector("xpath=" + QR_TAB_INDEX_XPATH)
        if tab is None:
            logger.error("未找到扫码登录入口")
            return False
        tab.click()
        logger.info("已切换到扫码登录")
        time.sleep(3)

        # 2. 定位二维码元素并截图推送
        qr_el = _find_qr_element(page)
        if qr_el is None:
            # 兜底：截取登录区域
            box = page.query_selector("#login_box")
            png = box.screenshot() if box else page.screenshot()
        else:
            png = qr_el.screenshot()
        qr_callback(png)
        logger.info("二维码已推送，等待扫码...")

        # 3. 轮询等待扫码结果（页面跳离登录页即成功）
        deadline = time.time() + wait_minutes * 60
        last_qr_refresh = time.time()
        while time.time() < deadline:
            time.sleep(poll_interval)
            if page.url != LOGIN_URL and "/login" not in page.url:
                logger.info("扫码登录成功")
                return True
            # 二维码约 60 秒过期，过期后刷新并重新推送
            if time.time() - last_qr_refresh > 55:
                refresh = _find_refresh_button(page)
                if refresh is not None:
                    try:
                        refresh.click()
                        time.sleep(2)
                        qr_el = _find_qr_element(page)
                        png = qr_el.screenshot() if qr_el is not None else page.screenshot()
                        qr_callback(png)
                        last_qr_refresh = time.time()
                        logger.info("二维码已刷新并重新推送")
                    except Exception as e:
                        logger.warning(f"二维码刷新失败: {e}")
        logger.warning("扫码登录等待超时")
        return False
    except Exception as e:
        logger.error(f"扫码登录流程异常: {e}")
        return False


def _find_qr_element(page):
    """定位二维码图片元素（多种可能的选择器）。"""
    candidates = [
        "//*[@id='login_box']//img[contains(@class,'qr') or contains(@class,'code')]",
        "//*[@id='login_box']//canvas",
        "//*[@id='login_box']//div[contains(@class,'qrcode') or contains(@class,'qr-code')]",
    ]
    for xpath in candidates:
        try:
            el = page.query_selector("xpath=" + xpath)
            if el is not None and el.is_visible():
                box = el.bounding_box()
                if box and box["width"] >= 80:
                    return el
        except Exception:
            continue
    return None


def _find_refresh_button(page):
    """定位二维码过期后的刷新按钮。"""
    candidates = [
        "//*[@id='login_box']//div[contains(@class,'refresh') or contains(@class,'mask')]//span",
        "//*[@id='login_box']//*[contains(text(),'刷新') or contains(text(),'点击刷新')]",
    ]
    for xpath in candidates:
        try:
            el = page.query_selector("xpath=" + xpath)
            if el is not None and el.is_visible():
                return el
        except Exception:
            continue
    return None
