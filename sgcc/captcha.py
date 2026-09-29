"""腾讯点选/滑块验证码的 LLM 解算与浏览器内自动处理。

本文件基于 ARC-MX/sgcc_electricity_new（Apache-2.0）修改：
https://github.com/ARC-MX/sgcc_electricity_new
修改内容：合并原 click_captcha_solver.py 与 captcha_playwright.py，
requests/PIL 改为函数内懒加载，Solver 参数改为构造注入。
"""

from __future__ import annotations

import base64
import io
import json
import logging
import random
import re
import time
from typing import List, Optional, Tuple

from .const import CAPTCHA_WIDGET_SELECTORS, TENCENT_SELECTORS

logger = logging.getLogger(__name__)


class ClickCaptchaSolver:
    """基于多模态大模型的图标点击验证码解算器。

    策略：
    1. 下载参考图标条，按三等分裁剪为 3 个独立图标
    2. 下载主图，全部以 data URI 形式随请求发送
    3. 单次 API 调用一次找到所有 3 个图标坐标
    """

    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self._client = None

    @property
    def client(self):
        if self._client is None:
            if not self.api_key:
                raise RuntimeError("LLM_API_KEY 未设置，验证码解算将失败")
            from openai import OpenAI

            self._client = OpenAI(base_url=self.base_url, api_key=self.api_key)
        return self._client

    def solve(self, ref_url: str, main_url: str,
              main_width: int, main_height: int) -> List[Tuple[int, int]]:
        """下载图片 → 拆分参考条 → 单次 LLM 调用找到所有 3 个图标。"""
        ref_raw = self._download(ref_url)
        if not ref_raw:
            return []
        icon_uris = self._split_strip(ref_raw)
        if len(icon_uris) < 3:
            return []

        main_raw = self._download(main_url)
        if not main_raw:
            return []
        main_uri = "data:image/png;base64," + base64.b64encode(main_raw).decode("ascii")

        coords = self._find_all_icons(icon_uris, main_uri, main_width, main_height)
        if len(coords) < 2:
            return []

        return [
            (max(0, min(x, main_width - 1)), max(0, min(y, main_height - 1)))
            for x, y in coords
        ]

    def solve_slider(self, bg_url: str) -> Optional[float]:
        """滑块验证码：返回缺口左边沿的 X 比例（0~1），失败返回 None。"""
        from PIL import Image

        raw = self._download(bg_url)
        if not raw:
            return None
        uri = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
        try:
            img = Image.open(io.BytesIO(raw))
            bg_w, bg_h = img.size
        except Exception:
            bg_w, bg_h = 0, 0

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": uri}},
                        {"type": "text", "text": (
                            f"This is a slider captcha background ({bg_w}x{bg_h} px).\n"
                            "Find the gap and return the X ratio (0~1) of its left edge.\n"
                            "Output format (single number): 0.XX"
                        )},
                    ],
                }],
                max_tokens=50,
            )
            output = response.choices[0].message.content or ""
            logger.info(f"滑块 LLM 响应: {output[:100]}")
            nums = re.findall(r"(\d+\.?\d*)", output)
            if not nums:
                return None
            ratio = float(nums[0])
            if ratio > 1.5 and bg_w:
                ratio = ratio / bg_w
            return max(0.0, min(1.0, ratio))
        except Exception as e:
            logger.error(f"滑块 LLM 调用失败: {e}")
            raise RuntimeError(f"LLM 调用失败: {e}") from e

    # ------------------------------------------------------------------

    def _download(self, url: str) -> Optional[bytes]:
        """下载图片，支持 http(s) 和 data URI。"""
        import requests

        try:
            if url.startswith("data:"):
                _, encoded = url.split(",", 1)
                try:
                    return base64.b64decode(encoded)
                except Exception:
                    import urllib.parse

                    return base64.b64decode(urllib.parse.unquote(encoded))
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200:
                return resp.content
            logger.error(f"验证码图片下载失败: HTTP {resp.status_code}")
            return None
        except Exception as e:
            logger.error(f"验证码图片下载错误: {e}")
            return None

    def _split_strip(self, raw: bytes) -> List[str]:
        """将参考图标条三等分为独立图标的 data URI。"""
        from PIL import Image

        try:
            img = Image.open(io.BytesIO(raw))
            w, h = img.size
            part_w = w // 3
            uris = []
            for i in range(3):
                left = i * part_w
                right = (i + 1) * part_w if i < 2 else w
                icon = img.crop((left, 0, right, h))
                # 放大图标以便 LLM 看清细节
                icon = icon.resize((icon.width * 3, icon.height * 3), Image.LANCZOS)
                buf = io.BytesIO()
                icon.save(buf, format="PNG")
                uris.append("data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii"))
            return uris
        except Exception as e:
            logger.error(f"参考图标条拆分错误: {e}")
            return []

    def _find_all_icons(self, icon_uris: List[str], main_uri: str,
                        main_width: int, main_height: int) -> List[Tuple[int, int]]:
        """单次 API 调用，让 LLM 找到所有 3 个图标。"""
        prompt = (
            f"大图（{main_width}×{main_height}像素）是一个图标网格。\n"
            "找到3个参考图标(A, B, C)各自在大图网格中的位置。\n"
            "匹配规则：形状和颜色必须一致，空心/实心、线条粗细是关键区分点，允许旋转。\n\n"
            '输出JSON：{"coords":[[xA,yA],[xB,yB],[xC,yC]]}\n'
            "其中x、y为图标中心的比例坐标（0~1）。"
        )

        content = []
        labels = ["A", "B", "C"]
        for i, uri in enumerate(icon_uris[:3]):
            content.append({"type": "image_url", "image_url": {"url": uri}})
            content.append({"type": "text", "text": f"参考图标{labels[i]}"})

        content.append({"type": "image_url", "image_url": {"url": main_uri}})
        content.append({"type": "text", "text": (
            "Use only the visible captcha image above. Return center coordinates "
            "for A, B, C as JSON. Do not use page or hidden DOM coordinates."
        )})
        content.append({"type": "text", "text": prompt})

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "Output valid JSON only. No markdown, no explanation."},
                    {"role": "user", "content": content},
                ],
                max_tokens=4096,
                response_format={"type": "json_object"},
            )
            output = response.choices[0].message.content or ""
            logger.info(f"大模型响应: {output[:400]}")
            return self._parse_coordinates(output, main_width, main_height)
        except Exception as e:
            logger.error(f"大模型调用失败: {e}")
            raise RuntimeError(f"LLM 调用失败: {e}") from e

    def _parse_coordinates(self, text: str,
                           main_width: int, main_height: int) -> List[Tuple[int, int]]:
        """从 LLM 返回文本中提取 JSON 坐标并转为像素。"""

        def to_pixel(x: float, y: float) -> Tuple[int, int]:
            if max(x, y) <= 1.5:
                return round(x * main_width), round(y * main_height)
            return round(x), round(y)

        match = re.search(r'\{.*"coords"\s*:\s*\[.*?\]\s*\}', text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
                return [to_pixel(float(x), float(y)) for x, y in data["coords"]]
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                pass

        coords = [
            (float(x), float(y))
            for x, y in re.findall(r'\(\s*(\d+\.?\d*)\s*[,，]\s*(\d+\.?\d*)\s*\)', text)
        ]
        if not coords:
            nums = re.findall(r"(\d+\.?\d+)", text)
            coords = [(float(nums[i]), float(nums[i + 1])) for i in range(0, len(nums) - 1, 2)]
        return [to_pixel(x, y) for x, y in coords[:3]]


# ======================================================================
# 浏览器内验证码处理
# ======================================================================

def solve_captcha_in_browser(page, solver: ClickCaptchaSolver,
                             timeout: int = 15,
                             max_retries: int = 3,
                             selectors: Optional[dict] = None) -> bool:
    """在浏览器页面内自动完成腾讯验证码，成功返回 True。"""
    selectors = selectors or TENCENT_SELECTORS

    for attempt in range(max_retries):
        logger.info(f"验证码尝试 {attempt + 1}/{max_retries}")

        if _has_rk001(page):
            logger.error("检测到 RK001 风控响应，停止验证码处理以保护账号。")
            return False

        if not _wait_for_captcha(page, timeout):
            logger.warning("未出现验证码控件")
            continue

        captcha_type = _detect_captcha_type_js(page)
        logger.info(f"验证码类型: {captcha_type}")

        if captcha_type == "slider":
            if _solve_slider(page, selectors, solver):
                logger.info("滑块验证码已通过")
                return True
            _refresh_captcha(page, selectors)
            time.sleep(2)
            continue

        if captcha_type != "click":
            # 未知类型尝试刷新成点选
            for refresh_i in range(5):
                logger.info(f"验证码类型为 {captcha_type}，刷新 ({refresh_i + 1}/5)...")
                _refresh_captcha(page, selectors)
                time.sleep(2)
                captcha_type = _detect_captcha_type_js(page)
                if captcha_type == "click":
                    break
                if captcha_type == "slider":
                    if _solve_slider(page, selectors, solver):
                        return True
            if captcha_type != "click":
                continue

        ref_url = _extract_ref_url(page, selectors)
        main_url, main_size = _extract_main_url(page, selectors)

        if not ref_url or not main_url or not main_size:
            logger.warning("提取验证码图片 URL 失败，刷新重试")
            _refresh_captcha(page, selectors)
            time.sleep(1)
            continue

        logger.info(f"主图尺寸={main_size}")

        try:
            coords = solver.solve(ref_url, main_url, main_size[0], main_size[1])
        except RuntimeError:
            raise  # LLM 配置错误等不可恢复错误直接上抛
        if not coords or len(coords) < 2:
            logger.warning(f"LLM 仅返回 {len(coords)} 个坐标，刷新重试")
            _refresh_captcha(page, selectors)
            time.sleep(1)
            continue
        logger.info(f"LLM 坐标: {coords}")

        image_el = _find_main_image_element(page, selectors, main_size[0] / main_size[1])
        if image_el is None:
            logger.error("无法定位主图元素")
            continue

        box = image_el.bounding_box()
        if box is None:
            continue
        scale_x = box["width"] / main_size[0]
        scale_y = box["height"] / main_size[1]
        logger.info(f"主图区域: {box}, 缩放=({scale_x:.3f}, {scale_y:.3f})")

        for i, (cx, cy) in enumerate(coords[:3]):
            px = box["x"] + cx * scale_x
            py = box["y"] + cy * scale_y
            logger.info(f"点击 #{i + 1}: 像素({cx},{cy}) -> 屏幕({px:.1f},{py:.1f})")
            page.mouse.click(px, py)
            time.sleep(random.uniform(0.25, 0.55))

        time.sleep(1)

        confirm_sel = selectors.get("confirm_btn")
        if confirm_sel:
            try:
                cfm = page.wait_for_selector(confirm_sel, state="visible", timeout=3000)
                time.sleep(0.5)
                cfm.click()
                logger.info("已点击确认按钮")
                time.sleep(2)
            except Exception:
                logger.info("确认按钮不可点击")

        time.sleep(2)
        if _check_passed(page):
            logger.info("验证码已通过")
            return True

        logger.info("未通过，刷新重试")
        if _has_rk001(page):
            logger.error("验证码尝试后检测到 RK001，停止。")
            return False
        _refresh_captcha(page, selectors)
        time.sleep(1)

    logger.error("验证码所有重试均失败")
    return False


def _solve_slider(page, selectors: dict, solver: ClickCaptchaSolver) -> bool:
    """使用 LLM 识别缺口并模拟拖拽完成滑块验证码。"""
    bg_el = page.query_selector(selectors["slider_bg_img"]) or page.query_selector(selectors["verify_bg_img"])
    if bg_el is None:
        return False

    bg_url = None
    tag = (bg_el.evaluate("el => el.tagName") or "").lower() if bg_el else ""
    if tag == "img":
        bg_url = bg_el.get_attribute("src") or ""
    if not bg_url or not bg_url.startswith("http"):
        style = (bg_el.get_attribute("style") or "") if bg_el else ""
        m = re.search(r'url\(["\']?(https?://[^"\')\s]+)["\']?\)', style)
        if m:
            bg_url = m.group(1)
    if not bg_url:
        try:
            bg_bytes = bg_el.screenshot()
            bg_url = "data:image/png;base64," + base64.b64encode(bg_bytes).decode()
        except Exception:
            return False

    groove = page.query_selector(selectors["slider_groove"])
    slider_block = page.query_selector(selectors["slider_block"])
    if groove is None or slider_block is None:
        return False

    groove_box = groove.bounding_box()
    block_box = slider_block.bounding_box()
    if groove_box is None or block_box is None:
        return False

    try:
        ratio = solver.solve_slider(bg_url)
        if ratio is None:
            return False

        track_width = groove_box["width"] - block_box["width"]
        drag_distance = max(10, min(int(ratio * track_width), int(track_width)))

        start_x = block_box["x"] + block_box["width"] / 2
        start_y = block_box["y"] + block_box["height"] / 2
        page.mouse.move(start_x, start_y)
        page.mouse.down()

        # 分段拖拽模拟人类操作
        segments = random.randint(3, 5)
        remaining = drag_distance
        for _ in range(segments - 1):
            step = random.randint(int(remaining * 0.2), int(remaining * 0.5))
            remaining -= step
            page.mouse.move(start_x + drag_distance - remaining,
                            start_y + random.randint(-1, 1), steps=1)
            time.sleep(random.uniform(0.02, 0.08))

        page.mouse.move(start_x + drag_distance, start_y + random.randint(-1, 1), steps=1)
        time.sleep(random.uniform(0.1, 0.2))
        page.mouse.up()

        time.sleep(2)
        return _check_passed(page)
    except RuntimeError:
        raise
    except Exception as e:
        logger.error(f"滑块解算错误: {e}")
        return False


def _extract_ref_url(page, selectors: dict) -> Optional[str]:
    el = page.query_selector(selectors["header_answer_img"])
    if el is None:
        return None
    return el.get_attribute("src") or None


def _extract_main_url(page, selectors: dict) -> Tuple[Optional[str], Optional[Tuple[int, int]]]:
    for sel in ("verify_bg_img", "point_area", "click_type_wrap", "verify_bg"):
        el = page.query_selector(selectors[sel])
        if el is None:
            continue

        tag = (el.evaluate("el => el.tagName") or "").lower()
        if tag == "img":
            src = el.get_attribute("src") or ""
            if src:
                return src, _get_image_size_from_url(src)

        style = el.get_attribute("style") or ""
        url_match = re.search(r'url\(["\']?(https?://[^"\')\s]+)["\']?\)', style)
        if url_match:
            url = url_match.group(1)
            return url, _get_image_size_from_url(url)

    return None, None


def _get_image_size_from_url(url: str) -> Optional[Tuple[int, int]]:
    if not url or not url.startswith("http"):
        return None
    import requests
    from PIL import Image

    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            img = Image.open(io.BytesIO(resp.content))
            return img.size
    except Exception:
        pass
    return None


def _detect_captcha_type_js(page) -> str:
    """仅检测可见的腾讯验证码控件，返回 click / slider / unknown。"""
    try:
        result = page.evaluate("""() => {
            function isVisible(el) {
                if (!el) return false;
                var style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden') return false;
                if (Number(style.opacity) === 0) return false;
                var r = el.getBoundingClientRect();
                if (r.width < 20 || r.height < 20) return false;
                if (r.bottom <= 0 || r.right <= 0) return false;
                if (r.top < -500 || r.left < -500) return false;
                if (r.top > window.innerHeight + 500) return false;
                return true;
            }
            function visibleEls(sel) {
                return Array.from(document.querySelectorAll(sel)).filter(isVisible);
            }
            function textOf(sel) {
                var els = visibleEls(sel);
                for (var i = 0; i < els.length; i++) {
                    var text = (els[i].textContent || els[i].innerText || '').trim();
                    if (text) return text;
                }
                return '';
            }
            function exists(sel) {
                return visibleEls(sel).length > 0;
            }
            function hasRenderableImage(sel) {
                var els = visibleEls(sel);
                for (var i = 0; i < els.length; i++) {
                    var r = els[i].getBoundingClientRect();
                    var src = els[i].getAttribute('src') || '';
                    var style = els[i].getAttribute('style') || '';
                    if (r.width >= 80 && r.height >= 60 && (src || /url\\(/.test(style))) {
                        if (els[i].tagName === 'IMG') {
                            if (els[i].naturalWidth > 0 && els[i].naturalHeight > 0) return true;
                            if (src && src.startsWith('http')) return true;
                        } else {
                            return true;
                        }
                    }
                }
                return false;
            }

            var prompt = textOf('.tencent-captcha-dy__header-text') ||
                         textOf('.tencent-captcha-dy__question') ||
                         textOf('.tencent-captcha-dy__title') || '';

            var hasClickImage = hasRenderableImage('.tencent-captcha-dy__verify-bg-img') ||
                                hasRenderableImage('.tencent-captcha-dy__point-area') ||
                                hasRenderableImage('.tencent-captcha-dy__click-type-wrap') ||
                                exists('.tencent-captcha-dy__header-answer img');
            var hasClickPrompt = /依次点击|顺序点击|点击下图|文字点选|请点击|click/i.test(prompt);
            if (hasClickImage && (hasClickPrompt ||
                exists('.tencent-captcha-dy__click-word') ||
                exists('.tencent-captcha-dy__point-area') ||
                exists('.tencent-captcha-dy__header-answer'))) {
                return 'click';
            }

            var hasSlider = exists('.tencent-captcha-dy__slider-groove') &&
                            exists('.tencent-captcha-dy__slider-block') &&
                            (hasRenderableImage('.tencent-captcha-dy__slider-bg-img') ||
                             hasRenderableImage('.tencent-captcha-dy__verify-bg-img'));
            if (hasSlider && /拖动|拼图|滑块|slide/i.test(prompt)) return 'slider';

            if (hasClickImage) return 'click';
            if (hasSlider) return 'slider';
            return 'unknown';
        }""")
        return result or "unknown"
    except Exception:
        return "unknown"


def _wait_for_captcha(page, timeout: int) -> bool:
    """等待可见的验证码控件出现。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _has_visible_captcha_widget(page):
            return True
        time.sleep(0.5)
    return False


def _has_visible_captcha_widget(page) -> bool:
    try:
        return page.evaluate("""(selectors) => {
            function isVisible(el) {
                if (!el) return false;
                var style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden') return false;
                if (Number(style.opacity) === 0) return false;
                var r = el.getBoundingClientRect();
                if (r.width < 80 || r.height < 80) return false;
                if (r.bottom <= 0 || r.right <= 0) return false;
                if (r.top < -500 || r.left < -500) return false;
                if (r.top > window.innerHeight + 500) return false;
                return true;
            }
            for (var i = 0; i < selectors.length; i++) {
                var els = document.querySelectorAll(selectors[i]);
                for (var j = 0; j < els.length; j++) {
                    if (isVisible(els[j])) return true;
                }
            }
            return false;
        }""", CAPTCHA_WIDGET_SELECTORS)
    except Exception:
        return False


def _has_rk001(page) -> bool:
    """检查页面内容中是否有 RK001 风控响应。"""
    try:
        body_text = page.text_content("body") or ""
        page_source = page.content() or ""
        for keyword in ("RK001", "risk_control", "riskControl"):
            if keyword in body_text or keyword in page_source:
                return True
    except Exception:
        pass
    return False


def _find_main_image_element(page, selectors: dict, expected_aspect: Optional[float] = None):
    """找到用于坐标换算的主图元素。"""
    best = None
    best_aspect_diff = float("inf")
    for sel in ("verify_bg_img", "image_area", "point_area", "click_type_wrap", "verify_bg"):
        sel_name = selectors.get(sel)
        if not sel_name:
            continue
        for el in page.query_selector_all(sel_name):
            try:
                box = el.bounding_box()
                if box is None or box["width"] < 80 or box["height"] < 80:
                    continue
                if expected_aspect:
                    aspect = box["width"] / box["height"]
                    diff = abs(aspect - expected_aspect) / expected_aspect
                    if diff < best_aspect_diff:
                        best_aspect_diff = diff
                        best = el
                else:
                    return el
            except Exception:
                pass
    return best


def _check_passed(page) -> bool:
    """检查验证码是否已通过（页面已跳离登录页）。"""
    from urllib.parse import urlparse

    if "/login" not in urlparse(page.url).path:
        return True
    try:
        body_text = page.text_content("body") or ""
        if any(kw in body_text for kw in ("登录成功", "验证成功", "success")):
            return True
    except Exception:
        pass
    el = page.query_selector("#tCaptchaDyContent")
    if el is None or not el.is_visible():
        time.sleep(1)
        el2 = page.query_selector("#tCaptchaDyContent")
        if el2 is None or not el2.is_visible():
            return True
    return False


def _refresh_captcha(page, selectors: dict):
    """刷新验证码。"""
    btn = page.query_selector(selectors["refresh_btn"])
    if btn and btn.is_visible():
        btn.click()
        return
    try:
        page.evaluate("""() => {
            var els = document.querySelectorAll('[class*="refresh"], [class*="footer-icon"]');
            for (var i = 0; i < els.length; i++) {
                if (els[i].offsetParent !== null && els[i].getBoundingClientRect().width > 5) {
                    els[i].click();
                    return;
                }
            }
        }""")
    except Exception:
        pass
