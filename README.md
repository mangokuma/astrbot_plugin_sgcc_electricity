# astrbot_plugin_sgcc_electricity — 国网电费查询

自动查询国家电网（95598.cn）电费与用电量的 AstrBot 插件：

- 💰 **电费查询**：余额 / 应交金额 / 本年 / 本月用电与电费
- 📊 **用电统计**：近 7 天 / 30 天每日用电量柱状图
- ⏰ **缴费提醒**：余额低于阈值自动推送到指定会话（私聊/群均可）
- 🔐 **智能登录**：Cookie 复用优先，密码登录 + 大模型验证码识别，失败可扫码兜底

数据抓取逻辑基于 [ARC-MX/sgcc_electricity_new](https://github.com/ARC-MX/sgcc_electricity_new)（**Apache License 2.0**）修改，已按协议要求保留署名、标注修改并附 LICENSE 副本；插件其余部分为原创。

## 安装

### 1. 安装插件依赖

插件目录放入 AstrBot `data/plugins/` 后，安装 Python 依赖：

```bash
pip install playwright openai requests matplotlib Pillow
playwright install chromium
```

> Chromium 体积约 300MB，首次安装需要耐心等待。

#### Docker 部署注意：安装浏览器系统依赖

如果日志报错 `error while loading shared libraries: libnspr4.so`（或 `libnss3.so`、`libgbm.so` 等），说明容器内缺少 Chromium 运行库。进入 AstrBot 容器执行：

```bash
python -m playwright install-deps chromium
```

`install-deps` 需要 apt 源可用；如果失败，可手动安装：

```bash
apt-get update && apt-get install -y libnspr4 libnss3 libatk1.0-0 libatk-bridge2.0-0 \
  libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 \
  libgbm1 libasound2 libpango-1.0-0 libpangocairo-1.0-0 libcairo2
```

安装完成后重启 AstrBot 或重载插件。

### 2. 启用插件

在 AstrBot Web 管理页面「插件」中启用本插件。

### 3. 配置插件

在插件配置页面填写（所有配置均可在 Web 页面编辑）：

| 配置项 | 说明 |
|---|---|
| `phone_number` / `password` | 国网 95598 账号密码（https://www.95598.cn/osgweb/login 可验证） |
| `llm_api_key` | 多模态大模型 API Key，**推荐硅基流动**（注册送代金券）。注意：DeepSeek 等纯文本模型无法识别验证码图片 |
| `llm_base_url` / `llm_model` | 默认 `https://api.siliconflow.cn/v1` + `Qwen/Qwen3.5-35B-A3B`，可换任意 OpenAI 兼容多模态模型 |
| `admin_qq` | 管理员 QQ（逗号分隔），**仅管理员可使用指令** |
| `remind_sessions` | 提醒推送会话列表（unified_msg_origin），在 AstrBot 管理页「会话」列表中复制，支持私聊和群 |
| `remind_threshold` | 余额提醒阈值（元），默认 5 |
| `query_time` | 每日自动抓取时间，默认 `07:00` |
| `retention_days` | 每日用电量保留 7 或 30 天（未签约智能交费的账号国网仅提供 7 天明细） |
| `login_fallback_qrcode` | 密码登录失败时推送二维码给管理员扫码兜底（默认开启） |
| `browser_headless` | 调试时可设为 `false` 观察浏览器操作 |

## 指令（仅管理员可用）

| 指令 | 说明 |
|---|---|
| `/电费` | 查看最近一次抓取的电费摘要 |
| `/用电统计` | 近 7 天每日用电量图表（等同 `/用电统计 7`） |
| `/用电统计 30` | 近 30 天每日用电量图表（数据不足时自动降级并提示） |
| `/电费更新` | 手动触发抓取（30 分钟冷却，防风控） |

## 工作原理与注意事项

1. **定时抓取**：每天 `query_time` 自动打开 95598.cn 抓取数据，浏览器 Profile 持久化在插件数据目录，Cookie 有效期内复用会话，**大幅减少登录次数**（国网有每日登录风控，登录频繁会出现 RK001 错误）。
2. **验证码**：出现腾讯点选/滑块验证码时，自动截图发送给多模态大模型识别并点击。
3. **扫码兜底**：密码登录失败（如当日登录次数超限）且开启兜底时，二维码自动私聊推送给管理员，扫码即登录。
4. **缴费提醒**：每日抓取后检查余额，低于阈值且当天未提醒过时，推送到 `remind_sessions` 中的所有会话。
5. **数据存储**：SQLite 保存在 AstrBot 数据目录 `plugin_data/astrbot_plugin_sgcc_electricity/`，保留 180 天日数据用于历史图表。
6. **网页改版**：95598.cn 页面结构变化时需要更新 `sgcc/const.py` 中的选择器（参考上游仓库更新）。

## 文件结构

```
astrbot_plugin_sgcc_electricity/
├── main.py            # 插件入口：指令注册、定时任务、消息推送
├── config.py          # 配置读取与校验
├── storage.py         # SQLite 存储
├── chart.py           # 用电量图表生成
├── reminder.py        # 缴费提醒逻辑
├── sgcc/
│   ├── client.py      # Playwright 抓取核心（登录 + 数据提取）
│   ├── captcha.py     # LLM 验证码识别（点选/滑块）
│   ├── qrcode_login.py# 扫码登录兜底
│   ├── vue_state.py   # Vue 状态注入数据提取
│   └── const.py       # URL 与页面选择器
├── metadata.yaml      # 插件元数据 + 配置 schema
├── requirements.txt
├── LICENSE            # Apache-2.0 协议全文（含原项目版权声明要求）
└── README.md
```

## 免责声明

本插件仅供个人学习使用，请合理使用查询频率。因使用本插件导致的账号风控等问题由使用者自行承担。
