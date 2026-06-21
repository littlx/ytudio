# ytudio

YouTube → 中文音频 的本地 Web 小工具。输入 YouTube 链接，二选一：

- **直接提取音频**：用 `yt-dlp` 下载音频，保留原始语音，速度最快。
- **字幕翻译 → 中文语音**：提取字幕 → DeepSeek 翻译成通顺忠于原意的中文 → `edge-tts` 合成中文音频。

两种模式生成的音频都可在浏览器内置播放器直接播放，并支持下载。

## 依赖

需要系统已安装：

- **Python** ≥ 3.10
- **ffmpeg**（音频转码，模式 B 需要）
- **yt-dlp**（命令行即可，程序内也用 Python 库）
- **deno 或 node**（YouTube 的 n-challenge 求解需要 JavaScript 运行时）

macOS 安装示例：

```bash
brew install ffmpeg yt-dlp deno
```

## 安装

```bash
cd ytudio
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 配置 DeepSeek API Key

字幕翻译模式需要 DeepSeek API Key：

1. 复制 `.env.example` 为 `.env`
2. 在 `.env` 中填入你的 key：

   ```
   DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxx
   ```

> 获取地址：https://platform.deepseek.com/
> 也可直接设置环境变量 `export DEEPSEEK_API_KEY=...`。
> 直接提取音频模式无需 key。

## 运行

```bash
python -m app
# 或开发模式（自动重载）
python -m uvicorn app.main:app --port 8200 --reload
```

启动后会自动打开浏览器访问 http://127.0.0.1:8200 。

> 默认端口 8200（`PORT`）、监听 `127.0.0.1`（`HOST`），可通过环境变量修改。

### 手机 / 局域网访问（PWA 装到手机听）

默认仅监听本机回环，手机无法连。如需手机访问：

1. 在 `.env` 设置 `HOST=0.0.0.0` 并**务必**设置 `AUTH_TOKEN`（随机字符串）：
   ```bash
   python -c "import secrets;print(secrets.token_urlsafe(24))"  # 生成 token
   ```
2. 重启服务。手机与电脑同一局域网，访问 `http://<电脑局域网IP>:8200/?token=<你的token>`。
3. 首次访问带 token 后，后续请求会自动携带；建议在手机浏览器「添加到主屏」作为 PWA 安装。

> ⚠️ **安全**：未配 `AUTH_TOKEN` 就绑定 `0.0.0.0`，会把 YouTube cookies（含登录凭证）等端点暴露给局域网所有人。启动时会打印警告。

## YouTube 登录 Cookies

YouTube 会对未登录请求做机器人检测，下载时报 `Sign in to confirm you're not a bot`。本工具提供**网页端 cookies 管理**，无需服务器装浏览器：

1. 在已登录 YouTube 的 Chrome 中安装扩展 **Get cookies.txt LOCALLY**（[Chrome 商店](https://chromewebstore.google.com/detail/get-cookiestxt-locally/ccpbcjjkcajmhkehiedhlbpppikboccm)）。
2. 打开 youtube.com → 点扩展图标 → `Export` → 下载 `cookies.txt`。
3. 在本工具页面打开「🍪 登录 Cookies」面板 → 点「上传文件」选择 `cookies.txt`（或用文本编辑器打开后复制粘贴到输入框）→ 点「保存」。
4. 状态变为「已配置」即可正常下载。cookies 保存到服务器 `data/cookies.txt`，重启不丢失，可随时在页面点「清除」删除。

> 服务器上有浏览器时，也可在 `.env` 配 `COOKIES_FROM_BROWSER=chrome` 自动读取，二者优先级：环境变量文件 > 页面上传 > 浏览器读取。
> cookies 含登录凭证，请勿泄露给他人。

## 使用

1. 粘贴 YouTube 链接
2. 选择模式（直接提取音频 / 字幕翻译→中文语音）
3. 点击「开始处理」，实时查看进度
4. 完成后用页面内播放器播放，或下载

## 可选配置（.env）

```
DEEPSEEK_API_KEY=          # 必填（仅字幕翻译模式）
DEEPSEEK_MODEL=            # 默认 deepseek-chat，推荐 deepseek-v4-flash（1M tokens 上下文）
DEEPSEEK_BASE_URL=         # 默认 https://api.deepseek.com，可填代理
WHOLE_TRANSLATE_LIMIT=     # 默认 800000，整篇翻译字符上限，超过才分批
TRANSLATE_CHUNK_SIZE=      # 默认 4000，超长时分批每批字符数
TTS_VOICE=                 # 默认 zh-CN-XiaoxiaoNeural
PORT=                      # 默认 8200
HOST=                      # 默认 127.0.0.1；局域网访问设 0.0.0.0（需配 AUTH_TOKEN）
AUTH_TOKEN=                # 局域网访问的访问令牌，本地访问无需配置

# yt-dlp 登录（遇到 "Sign in to confirm you're not a bot" 时开启）
COOKIES_FROM_BROWSER=      # 如 chrome / safari / firefox / edge / brave
COOKIES_FILE=              # 或 cookies 文件绝对路径
REMOTE_COMPONENTS=         # 默认 ejs:github，解决 YouTube n-challenge（需 deno/node）
```

> 直接提取音频模式默认保留 YouTube 的原始音频格式（m4a/webm），浏览器均可直接播放，不做强制转码。

## 目录说明

```
app/                   后端代码
  main.py              FastAPI 路由与启动
  pipeline.py          任务调度（信号量串行 + 异常兜底）
  steps.py             处理步骤链（按权重归一化进度，可组合）
  assets.py            资产包：output/{video_id}/ 统一管理音频/字幕/译文/元数据
  yt.py                yt-dlp 封装（信息获取/音频下载/字幕提取）
  translate.py         DeepSeek 翻译（整篇优先，超长分批）
  tts.py               edge-tts 逐段合成 + ffmpeg 拼接
  tasks.py             任务生命周期 + SSE 推送 + 状态持久化
  history_store.py     历史索引（data/history.json）
  cookies.py           cookies 校验与来源管理
  voices.py            可用音色列表
  config.py            配置常量（.env 读取）
templates/             前端页面
  index.html
  static/css/          样式
  static/js/           ES Module 前端
    app.js             入口（初始化编排）
    state.js           唯一状态源（subscribe/setState + localStorage 持久化）
    api.js             API 封装（token 携带 + 统一错误处理）
    player.js          播放器（播放/进度/Media Session/快捷键）
    task.js            任务提交 + SSE 订阅
    pwa.js             Service Worker + Tab 切换
    views/             视图模块（history/voices/cookies/progress）
  sw.js                Service Worker（分桶缓存）
tests/                 单元测试（73 个用例）
pyproject.toml         pytest 配置
output/                资产包目录（运行时创建，已 git 忽略）
data/                  cookies/历史/任务状态等运行时数据（已 git 忽略）
.env                   你的密钥配置（已 git 忽略）
```

## 开发与测试

### 架构概览

- **资产包**：每个视频的全部产物收敛到 `output/{video_id}/`，元数据集中在 `meta.json`，删除即 `rmdir`，不再依赖文件名约定。
- **步骤链**：两种处理模式（直接提取音频 / 字幕翻译→TTS）拆为可组合步骤，进度按权重自动归一化，公共步骤复用。
- **状态中心化前端**：原生 ES Module，`state.js` 为唯一状态源，各视图订阅重渲染，不引入构建链。
- **任务持久化**：进程重启后进行中任务标记为中断，SSE 重连可补发终态。

### 运行测试

```bash
pip install -r requirements-dev.txt
pytest                    # 全部 73 个测试
pytest tests/test_assets.py  # 单个模块
```

测试用隔离的临时目录，不触碰真实的 `output/` 和 `data/`。

## 说明

- 本工具仅用于个人学习，请遵守 YouTube 服务条款与版权法律。
- 字幕翻译模式依赖视频存在可用字幕；无字幕的视频请改用直接提取音频。
- **密钥安全**：`.env` 含 `DEEPSEEK_API_KEY` 等敏感信息（已 git 忽略）。若怀疑泄露，请到 DeepSeek 控制台轮换 Key。局域网部署务必配置 `AUTH_TOKEN`。
