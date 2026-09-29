"""95598.cn 数据抓取客户端（同步 Playwright）。

本文件基于 ARC-MX/sgcc_electricity_new（Apache-2.0）的 data_fetcher.py 修改：
https://github.com/ARC-MX/sgcc_electricity_new
修改内容：去除 HomeAssistant/Selenium/CDP/CloakBrowser 相关逻辑，
改为持久化 Profile 复用会话，单账号抓取，返回结构化 FetchResult。

注意：Playwright 同步 API 不能运行在 asyncio 事件循环线程中，
本类的所有方法必须在独立线程中调用（插件层通过 asyncio.to_thread 包装）。

设计要点：
- 使用 launch_persistent_context + 固定 user_data_dir，Cookie/本地存储跨次复用，
  最大限度减少登录次数（国网有每日登录风控限制）。
- 登录优先密码登录 + LLM 识别腾讯点选验证码；失败且开启兜底时切换扫码登录，
  二维码通过回调交给插件层推送给管理员。
- 数据提取优先 Vue 状态注入，DOM 解析兜底。
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, List, Optional

from . import vue_state
from .captcha import ClickCaptchaSolver, solve_captcha_in_browser
from .const import (
    AGREE_XPATH,
    BALANCE_URL,
    ELECTRIC_USAGE_URL,
    LOGIN_BUTTON_SELECTOR,
    LOGIN_INPUT_SELECTOR,
    LOGIN_URL,
    PASSWORD_TAB_XPATH,
)
from .qrcode_login import login_with_qrcode

logger = logging.getLogger(__name__)


@dataclass
class FetchResult:
    """一次抓取的完整结果。"""

    user_id: str = ""
    user_name: str = ""
    balance: Optional[float] = None          # 预付费余额 / 后付费上月应交
    amount_due: Optional[float] = None       # 后付费应交金额
    as_of: str = ""                          # 余额数据日期
    yearly_usage: Optional[float] = None
    yearly_charge: Optional[float] = None
    month_usage: Optional[float] = None
    month_charge: Optional[float] = None
    daily: List[dict[str, Any]] = field(default_factory=list)  # [{date, total_usage, ...}]
    fetched_at: str = ""
    need_login: bool = False                 # True 表示本次发生了重新登录

    def summary_text(self) -> str:
        lines = []
        if self.balance is not None:
            lines.append(f"电费余额: {self.balance:.2f} 元")
        if self.amount_due is not None:
            lines.append(f"应交金额: {self.amount_due:.2f} 元")
        if self.as_of:
            lines.append(f"数据日期: {self.as_of}")
        if self.yearly_usage is not None:
            lines.append(f"本年用电: {self.yearly_usage:.0f} 度 / {self.yearly_charge or 0:.2f} 元")
        if self.month_usage is not None:
            lines.append(f"本月用电: {self.month_usage:.1f} 度 / {self.month_charge or 0:.2f} 元")
        if self.daily:
            latest = sorted(self.daily, key=lambda d: d.get("date", ""))[-1]
            lines.append(f"最近用电: {latest.get('date')} {latest.get('total_usage', 0):.2f} 度")
        lines.append(f"抓取时间: {self.fetched_at}")
        return "\n".join(lines)


class SGCCClient:
    def __init__(self,
                 phone: str,
                 password: str,
                 llm_api_key: str,
                 llm_base_url: str,
                 llm_model: str,
                 data_dir: str,
                 headless: bool = True,
                 retry_wait_time: int = 10,
                 login_fallback_qrcode: bool = True,
                 qrcode_wait_minutes: int = 5,
                 qr_callback: Optional[Callable[[bytes], None]] = None):
        self._phone = phone
        self._password = password
        self._data_dir = data_dir
        self._headless = headless
        self._wait = retry_wait_time
        self._login_fallback_qrcode = login_fallback_qrcode
        self._qrcode_wait_minutes = qrcode_wait_minutes
        self._qr_callback = qr_callback
        self._solver = ClickCaptchaSolver(llm_api_key, llm_base_url, llm_model)
        self._pw = None
        self._context = None
        self._page = None
        self._rk001_detected = False

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------

    def fetch(self) -> FetchResult:
        """完整抓取一次电费/电量数据。失败抛异常。"""
        self._setup_browser()
        need_login = False
        try:
            if not self._is_logged_in():
                logger.info("浏览器会话未登录，开始登录流程")
                if not self._login():
                    raise RuntimeError("登录失败（验证码识别失败、RK001 风控或扫码超时）")
                need_login = True
            else:
                logger.info("浏览器会话有效，跳过登录")

            result = self._collect_data()
            result.need_login = need_login
            result.fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return result
        finally:
            self._teardown_browser()

    # ------------------------------------------------------------------
    # 浏览器生命周期
    # ------------------------------------------------------------------

    def _setup_browser(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        profile_dir = os.path.join(self._data_dir, "browser_profile")
        os.makedirs(profile_dir, exist_ok=True)

        args = [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--disable-extensions",
            "--disable-sync",
            "--no-first-run",
            "--window-size=1920,1080",
        ]
        self._context = self._pw.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=self._headless,
            args=args,
            viewport={"width": 1920, "height": 1080},
            locale="zh_CN",
            timezone_id="Asia/Shanghai",
        )
        self._context.add_init_script(_STEALTH_INIT_JS)
        pages = self._context.pages
        self._page = pages[0] if pages else self._context.new_page()
        self._page.set_default_timeout(60_000)
        logger.info("浏览器就绪（持久化 Profile + 反检测脚本）")

    def _teardown_browser(self):
        try:
            if self._context is not None:
                self._context.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._context = None
        self._page = None
        self._pw = None

    # ------------------------------------------------------------------
    # 登录态检测 / 登录
    # ------------------------------------------------------------------

    def _is_logged_in(self) -> bool:
        """访问余额页，通过页面特征判断是否已登录。"""
        try:
            self._page.goto(BALANCE_URL, wait_until="domcontentloaded", timeout=60_000)
            time.sleep(self._wait)
            if self._page.url.startswith(LOGIN_URL) or "/login" in self._page.url:
                return False
            # 已登录页面应能找到余额特征元素
            for selector in (".cff8", "[class*='balance']", ".el-dropdown"):
                try:
                    el = self._page.query_selector(selector)
                    if el is not None:
                        return True
                except Exception:
                    continue
            return False
        except Exception as e:
            logger.warning(f"登录态检测异常，按未登录处理: {e}")
            return False

    def _login(self) -> bool:
        """密码登录（+LLM 验证码），失败时可选扫码兜底。成功返回 True。"""
        page = self._page
        self._rk001_detected = False

        def _on_response(response):
            try:
                url = response.url
                if any(kw in url for kw in ("/api/login", "/user/login", "/oauth/login",
                                            "/login.action", "/doLogin", "/auth/login",
                                            "riskControl", "risk_control")):
                    try:
                        if "json" in response.headers.get("content-type", ""):
                            body = response.text()
                            if "RK001" in body:
                                self._rk001_detected = True
                                logger.error("[RK001] 登录 API 返回风控码")
                    except Exception:
                        pass
                    if "riskControl" in url or "risk_control" in url:
                        self._rk001_detected = True
            except Exception:
                pass

        page.on("response", _on_response)
        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=180_000)
            time.sleep(self._wait * 2)
            page.wait_for_selector(".user", state="visible", timeout=180_000)
            try:
                page.wait_for_load_state("networkidle", timeout=30_000)
            except Exception:
                pass
            logger.info("登录页加载完成")

            self._wait_captcha_sdk(page)
            if self._rk001_detected or self._is_rk001_blocked(page):
                logger.error("页面加载阶段检测到 RK001，中止登录以保护账号")
                return False

            self._random_delay(1.5, 3.0)
            user_el = page.wait_for_selector(".user", state="visible", timeout=60_000)
            self._random_delay(0.5, 1.5)
            user_el.click()

            # 账号密码登录 tab
            self._random_delay(1.0, 2.5)
            if not self._click_xpath(PASSWORD_TAB_XPATH):
                logger.error("未找到「账号密码登录」入口")
                return False

            # 勾选「我已阅读并同意」
            self._random_delay(0.8, 2.0)
            self._click_xpath(AGREE_XPATH)

            self._random_delay(1.0, 2.5)
            if self._rk001_detected or self._is_rk001_blocked(page):
                logger.error("同意协议后检测到 RK001，中止登录")
                return False

            # 输入账号密码
            inputs = page.query_selector_all(LOGIN_INPUT_SELECTOR)
            if len(inputs) < 2:
                logger.error("未找到账号/密码输入框")
                return False
            self._random_delay(0.5, 1.0)
            inputs[0].click()
            self._random_delay(0.3, 0.8)
            inputs[0].fill(self._phone)
            logger.info(f"已输入用户名: {self._phone}")
            self._random_delay(0.5, 1.2)
            inputs[1].click()
            self._random_delay(0.3, 0.8)
            inputs[1].fill(self._password)
            logger.info("已输入密码")

            # 提交登录
            self._random_delay(1.0, 2.5)
            self._click_selector(LOGIN_BUTTON_SELECTOR)
            self._random_delay(2.0, 4.0)
            logger.info("已点击登录按钮")

            if self._rk001_detected or self._is_rk001_blocked(page):
                logger.error("[RK001] 提交登录后检测到风控，中止")
                return False

            if "/login" not in page.url:
                logger.info("登录成功（无需验证码）")
                return True

            # 图标点选验证码
            try:
                passed = solve_captcha_in_browser(page, self._solver, max_retries=3)
            except RuntimeError:
                raise  # LLM 配置错误等不可恢复错误直接上抛
            if passed:
                for _ in range(5):
                    time.sleep(self._wait)
                    if "/login" not in page.url:
                        logger.info("验证码通过，登录成功")
                        return True
                if "/login" not in page.url:
                    return True

            # 密码登录失败 → 扫码兜底
            logger.warning("密码登录失败")
            if self._login_fallback_qrcode and self._qr_callback is not None:
                logger.info("尝试扫码登录兜底")
                if "/login" in page.url:
                    return login_with_qrcode(
                        page, self._qr_callback,
                        wait_minutes=self._qrcode_wait_minutes,
                    )
            return False
        finally:
            try:
                page.remove_listener("response", _on_response)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 数据抓取
    # ------------------------------------------------------------------

    def _collect_data(self) -> FetchResult:
        result = FetchResult()
        page = self._page

        # ---- 余额页 ----
        self._random_delay(1, 3)
        if BALANCE_URL not in page.url:
            page.goto(BALANCE_URL)
            time.sleep(self._wait)
        result.balance = self._get_electric_balance()

        # ---- 用户信息 ----
        user_info = self._get_user_info()
        result.user_id = user_info.get("user_id", "")
        result.user_name = user_info.get("user_name", "")

        # ---- 用电量页（Vue 状态优先）----
        page.goto(ELECTRIC_USAGE_URL)
        time.sleep(self._wait)
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        time.sleep(self._wait)

        usage_info: Optional[dict[str, Any]] = None
        enhanced_balance: Optional[dict[str, Any]] = None
        try:
            components = vue_state.selected_vue_data(page)
            enhanced_balance = vue_state.normalize_balance(components)
            usage_info = vue_state.normalize_usage(components)
            if usage_info and usage_info.get("daily"):
                logger.info(
                    f"Vue 状态提取成功: 月度 {len(usage_info.get('months', []))} 条, "
                    f"日数据 {len(usage_info['daily'])} 条"
                )
            else:
                usage_info = None
        except Exception as e:
            logger.warning(f"Vue 状态提取失败: {e}")

        if usage_info:
            result.yearly_usage = usage_info.get("yearly_usage")
            result.yearly_charge = usage_info.get("yearly_charge")
            months = usage_info.get("months") or []
            if months:
                latest = sorted(months, key=lambda m: m.get("month", ""))[-1]
                result.month_usage = latest.get("total_usage")
                result.month_charge = latest.get("total_charge")
            result.daily = sorted(usage_info.get("daily") or [], key=lambda d: d.get("date", ""))
        else:
            # DOM 兜底
            result.yearly_usage, result.yearly_charge = self._get_yearly_dom()
            date_list, usage_list = self._get_daily_dom()
            result.daily = [
                {"date": d, "total_usage": float(u)}
                for d, u in zip(date_list, usage_list)
                if _is_float(u)
            ]
            latest = result.daily[-1] if result.daily else None
            result.month_usage = latest["total_usage"] if latest else None

        # 余额增强信息（应交金额 / 数据日期）
        if enhanced_balance:
            if enhanced_balance.get("amount_due") is not None:
                result.amount_due = enhanced_balance["amount_due"]
            result.as_of = str(enhanced_balance.get("as_of") or "")

        if not result.user_id:
            result.user_id = self._get_current_userid()
        return result

    def _get_electric_balance(self) -> Optional[float]:
        """预付费账户余额 / 后付费账户余额。"""
        page = self._page
        try:
            # 后付费：页面有「应交金额」标题时，读取「账户余额」
            try:
                title = page.query_selector(
                    "xpath=" + "//p[contains(@class, 'balance_title') and contains(text(), '应交金额')]")
                if title is not None and "应交金额" in (title.inner_text() or ""):
                    balance_el = page.query_selector(
                        "xpath=" + "//p[contains(@class, 'balance_title') and contains(text(), '账户余额')]")
                    if balance_el is not None:
                        text = re.sub(r"[^\d.\-]", "", balance_el.inner_text())
                        if text:
                            return float(text)
            except Exception:
                pass

            # 预付费：.cff8 元素
            el = page.query_selector(".cff8")
            if el is not None:
                text = el.inner_text() or ""
                number = re.sub(r"[^\d.\-]", "", text.replace("欠费", "-"))
                if number:
                    return float(number)
        except Exception as e:
            logger.error(f"获取余额失败: {e}")
        return None

    def _get_user_info(self) -> dict[str, str]:
        """优先 Vue 状态，失败则从页面文本解析户号。"""
        try:
            components = vue_state.selected_vue_data(self._page)
            info = vue_state.normalize_user_info(components)
            if info.get("user_id"):
                return info
        except Exception:
            pass
        return {"user_id": self._get_current_userid(), "user_name": "", "address": ""}

    def _get_current_userid(self) -> str:
        """从页面读取 13 位用电户号。"""
        try:
            label = self._page.query_selector(
                "xpath=" + "//*[contains(normalize-space(.), '用电户号')]")
            if label is not None:
                matches = re.findall(r"\b\d{13}\b", label.inner_text() or "")
                if matches:
                    return matches[-1]
        except Exception:
            pass
        try:
            source = self._page.content() or ""
            match = re.search(r"用电户号[:：\s]*([0-9]{13})", source)
            if match:
                return match.group(1)
            matches = re.findall(r"\b(\d{13})\b", source)
            if matches:
                return matches[0]
        except Exception:
            pass
        return ""

    # ---- DOM 兜底解析 ----

    def _get_yearly_dom(self):
        page = self._page
        try:
            tab = page.query_selector("xpath=" + "//div[@class='el-tabs__nav is-top']/div[@id='tab-first']")
            if tab is not None:
                tab.click()
                time.sleep(self._wait)
            usage_el = page.query_selector("xpath=" + "//ul[@class='total']/li[1]/span")
            charge_el = page.query_selector("xpath=" + "//ul[@class='total']/li[2]/span")
            usage = float(usage_el.inner_text()) if usage_el else None
            charge = float(charge_el.inner_text()) if charge_el else None
            return usage, charge
        except Exception as e:
            logger.warning(f"DOM 年度数据获取失败: {e}")
            return None, None

    def _get_daily_dom(self):
        """DOM 方式获取近 7/30 天每日用电量。"""
        page = self._page
        try:
            tab = page.query_selector(
                "xpath=" + "//div[@class='el-tabs__nav is-top']/div[@id='tab-second']")
            if tab is not None:
                tab.click()
                time.sleep(self._wait * 2)

            page.wait_for_selector(
                "xpath=" + "//*[@id='pane-second']//div[contains(@class,'el-table__body-wrapper')]"
                           "/table/tbody/tr",
                state="attached", timeout=60_000)

            rows = page.query_selector_all(
                "xpath=" + "//*[@id='pane-second']//div[contains(@class,'el-table__body-wrapper')]"
                           "/table/tbody/tr")
            dates, usages = [], []
            for row in rows:
                try:
                    day = row.query_selector("xpath=td[1]/div").inner_text().strip()
                    usage = row.query_selector("xpath=td[2]/div").inner_text().strip()
                    if day and usage:
                        dates.append(day)
                        usages.append(usage)
                except Exception:
                    continue
            logger.info(f"DOM 方式获取到 {len(dates)} 天每日用电量")
            return dates, usages
        except Exception as e:
            logger.warning(f"DOM 每日用电量获取失败: {e}")
            return [], []

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def _click_xpath(self, xpath: str) -> bool:
        try:
            el = self._page.query_selector("xpath=" + xpath)
            if el is None:
                return False
            el.click()
            return True
        except Exception as e:
            logger.debug(f"点击失败 {xpath}: {e}")
            return False

    def _click_selector(self, selector: str) -> bool:
        try:
            el = self._page.query_selector(selector)
            if el is None:
                return False
            el.click()
            return True
        except Exception as e:
            logger.debug(f"点击失败 {selector}: {e}")
            return False

    def _random_delay(self, min_seconds: float = 0.5, max_seconds: float = 3.0):
        time.sleep(random.uniform(min_seconds, max_seconds))

    def _wait_captcha_sdk(self, page, timeout: int = 20) -> bool:
        """等待腾讯验证码 SDK 加载完成（SDK 未就绪就提交会触发 RK001）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                ready = page.evaluate("""() => {
                    if (typeof window.TencentCaptcha !== 'undefined') return 'TencentCaptcha';
                    if (typeof window.tc_captcha !== 'undefined') return 'tc_captcha';
                    var scripts = document.querySelectorAll('script[src*="captcha"], script[src*="tencent"]');
                    if (scripts.length > 0) return 'scripts:' + scripts.length;
                    if (document.querySelector('#tCaptchaDyContent, .tencent-captcha-dy__warp')) return 'dom_ready';
                    return '';
                }""")
                if ready:
                    logger.info(f"腾讯验证码 SDK 已就绪: {ready}")
                    self._random_delay(1.5, 3.0)
                    return True
            except Exception:
                pass
            time.sleep(1)
        logger.warning("验证码 SDK 等待超时，继续尝试登录")
        return False

    def _is_rk001_blocked(self, page) -> bool:
        """分级检测页面可见文本中的 RK001 风控码。"""
        if self._rk001_detected:
            return True
        try:
            result = page.evaluate("""() => {
                var errorSelectors = ['.errmsg-tip', '.error-msg', '.el-message--error',
                    '.tencent-captcha-dy__header-text', '[class*="error"]'];
                for (var i = 0; i < errorSelectors.length; i++) {
                    var els = document.querySelectorAll(errorSelectors[i]);
                    for (var j = 0; j < els.length; j++) {
                        var text = (els[j].textContent || '').trim();
                        if (text && /RK001|risk.?control/i.test(text)) return text;
                    }
                }
                return '';
            }""")
            if result:
                logger.error(f"[RK001] 页面可见文本检测到风控码: {result[:80]}")
                return True
        except Exception:
            pass
        return False


# ======================================================================
# 反检测 init_script（标准 Chromium 模式的完整指纹伪装）
# ======================================================================

_STEALTH_INIT_JS = r"""(() => {
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
const patchWebGL = (proto) => {if(!proto||!proto.getParameter)return;
const orig=proto.getParameter;proto.getParameter=function(p){
if(p===37445)return'Intel Inc.';if(p===37446)return'Intel(R) UHD Graphics 620';
return orig.apply(this,arguments);};};
if(typeof WebGLRenderingContext!=='undefined')
patchWebGL(WebGLRenderingContext.prototype);
if(typeof WebGL2RenderingContext!=='undefined')
patchWebGL(WebGL2RenderingContext.prototype);
Object.defineProperty(navigator,'languages',{get:()=>Object.freeze(['zh-CN','zh','en-US','en'])});
Object.defineProperty(navigator,'platform',{get:()=>'Win32'});
Object.defineProperty(navigator,'hardwareConcurrency',{get:()=>8});
Object.defineProperty(navigator,'maxTouchPoints',{get:()=>0});
Object.defineProperty(navigator,'doNotTrack',{get:()=>null});
const oq=(navigator.permissions||{}).query;
if(oq)navigator.permissions.query=function(p){
if(p.name==='notifications')return Promise.resolve({state:'prompt',onchange:null});
if(p.name==='geolocation')return Promise.resolve({state:'prompt',onchange:null});
return oq.call(navigator.permissions,p);};
Object.defineProperty(window,'chrome',{get:()=>({runtime:{}})});
})();"""


def _is_float(text: Any) -> bool:
    try:
        float(str(text).strip())
        return True
    except (TypeError, ValueError):
        return False


def result_to_json(result: FetchResult) -> str:
    """把 FetchResult 序列化为 JSON（去掉冗余字段），便于缓存。"""
    data = {
        "user_id": result.user_id,
        "user_name": result.user_name,
        "balance": result.balance,
        "amount_due": result.amount_due,
        "as_of": result.as_of,
        "yearly_usage": result.yearly_usage,
        "yearly_charge": result.yearly_charge,
        "month_usage": result.month_usage,
        "month_charge": result.month_charge,
        "fetched_at": result.fetched_at,
    }
    return json.dumps(data, ensure_ascii=False)
