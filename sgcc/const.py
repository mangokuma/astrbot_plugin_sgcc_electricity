"""95598.cn 相关常量（URL 与关键选择器）。

本文件基于 ARC-MX/sgcc_electricity_new（Apache-2.0）修改：
https://github.com/ARC-MX/sgcc_electricity_new
修改内容：精简为插件所需常量，去除 HomeAssistant 相关定义。
"""

# 国网电力官网
LOGIN_URL = "https://www.95598.cn/osgweb/login"
ELECTRIC_USAGE_URL = "https://www.95598.cn/osgweb/electricityCharge"
BALANCE_URL = "https://www.95598.cn/osgweb/userAcc"

# 腾讯点选验证码选择器
TENCENT_SELECTORS = {
    "content": "#tCaptchaDyContent",
    "header_answer_img": ".tencent-captcha-dy__header-answer img",
    "point_area": ".tencent-captcha-dy__point-area",
    "click_type_wrap": ".tencent-captcha-dy__click-type-wrap",
    "image_area": ".tencent-captcha-dy__verify-bg-img",
    "verify_bg_img": ".tencent-captcha-dy__verify-bg-img",
    "verify_bg": ".tencent-captcha-dy__verify-bg",
    "refresh_btn": ".tencent-captcha-dy__footer-icon--refresh",
    "confirm_btn": ".tencent-captcha-dy__verify-confirm-btn",
    "slider_area": ".tencent-captcha-dy__verify-slider-area",
    "slider_groove": ".tencent-captcha-dy__slider-groove",
    "slider_block": ".tencent-captcha-dy__slider-block",
    "slider_bg_img": ".tencent-captcha-dy__slider-bg-img",
}

CAPTCHA_WIDGET_SELECTORS = [
    ".tencent-captcha-dy__warp",
    ".tencent-captcha-dy__wrapper",
    ".tencent-captcha__wrapper",
    ".tencent-captcha-dy__body-wrap",
    "#tCaptchaDyContent",
]

# 登录页「账号密码登录」tab  xpath（参考项目使用）
PASSWORD_TAB_XPATH = '//*[@id="login_box"]/div[1]/div[1]/div[2]/span'
# 登录页「我已阅读并同意」勾选 xpath
AGREE_XPATH = '//*[@id="login_box"]/div[2]/div[1]/form/div[1]/div[3]/div/span[2]'
# 登录按钮
LOGIN_BUTTON_SELECTOR = ".el-button.el-button--primary"
# 账号 / 密码输入框（Element UI）
LOGIN_INPUT_SELECTOR = ".el-input__inner"
