# Pinterest Local Downloader

Windows 本地社交媒体图片采集工具。它通过可见的 Pinterest / X 页面收集公开图片资源，并将经验证的图片、元数据和失败记录保存到用户选定的本地目录。项目不连接 MUSE、数据库、云存储或任何服务器。

平台由 `platform` 字段区分：旧任务缺少该字段时默认按 `pinterest` 解释。

## 采集模式

Pinterest 的 GUI 和 CLI 共用三种采集模式：

* `search` / 关键词搜索：按关键词打开搜索结果，保持原有工作流。
* `home` / 首页推荐：从 Pinterest 首页推荐 Feed 开始，不需要关键词。
* `current_page` / 当前页面：使用 Playwright 当前 Pinterest tab，不自动跳转到搜索页；如果浏览器尚未启动，会打开 Pinterest 并提示用户导航到目标页面。

三种模式共用「下载数量」，它表示最终成功保存的合格图片数量。尺寸不足、格式错误、下载失败和哈希重复不会占用目标数量；采集器会继续滚动，直到达到目标或触发最大滚动/连续无新增保护。

X 支持 `collection_mode=author`（作者主页）和 `collection_mode=home`（首页“为你推荐”）。X 下载内容类型为 `image`、`video`、`mixed`，统一使用最终成功媒体数量作为目标；作者主页默认仍为仅图片。

仅图片会跳过视频、视频封面、纯文字、广告、头像、banner 和 UI 图片，并继续执行 Pillow 尺寸过滤、URL/hash 去重。仅视频和 mixed 会从页面网络响应捕获 HLS m3u8，下载视频/音频分片后使用本地 FFmpeg 封装为 MP4；缺少 FFmpeg 时只记录视频失败，不影响图片模式。

## 环境与安装

在项目目录执行（若 `py -3.12` 未识别，改用 Python 安装目录中的 `python.exe`）：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\playwright.exe install chromium
```

如遇 PowerShell 执行策略限制，运行 `Set-ExecutionPolicy -Scope Process Bypass` 后重新激活虚拟环境。

## 启动

图形界面：

```powershell
.\.venv\Scripts\python.exe app.py
```

命令行核心（用于小规模验证）：

```powershell
.\.venv\Scripts\python.exe -m src.downloader.cli "东方古风美女" --count 20 --output "D:\Pinterest素材"

# 首页推荐
.\.venv\Scripts\python.exe -m src.downloader.cli --mode home --count 20 --output "D:\Pinterest素材"

# 当前页面
.\.venv\Scripts\python.exe -m src.downloader.cli --mode current_page --count 20 --output "D:\Pinterest素材"

# X 作者主页
.\.venv\Scripts\python.exe -m src.downloader.cli --platform x --mode author "@NASA" --count 3 --output "D:\X素材"

# X 首页仅图片
.\.venv\Scripts\python.exe -m src.downloader.cli --platform x --mode home --media-type image --count 5 --output "D:\X素材"

# X 首页仅视频 / 图片+视频
.\.venv\Scripts\python.exe -m src.downloader.cli --platform x --mode home --media-type video --count 3 --output "D:\X视频"
.\.venv\Scripts\python.exe -m src.downloader.cli --platform x --mode home --media-type mixed --count 5 --output "D:\X混合素材"
```

GUI 的“打开浏览器 / 登录”入口会直接启动本机普通 `chrome.exe`，只传递专用 `--user-data-dir` 和目标登录页面，不使用 Playwright、自动化参数、remote debugging、webdriver、headless 或 `--no-sandbox`。用户可在普通 Chrome 中人工登录 Pinterest / X，完成验证码和 2FA 后关闭浏览器；登录状态只保存在固定的采集器专用目录 `%LOCALAPPDATA%\\PinterestLocalDownloader\\browser-data`，不会收集账号密码。

正式采集仍使用 Playwright `launch_persistent_context` 复用同一个 Profile，采集开始前若普通 Chrome 仍占用 Profile，会提示“请先关闭登录浏览器，再开始采集。”采集任务结束会关闭 Playwright context，但专用 Profile 数据保留。

采集器不会自动复制日常 Chrome Profile，也不会读取、上传或注入 Cookie。日常 Chrome 正在运行时不应复制其 Profile；Chrome Cookie 受 Windows/Chrome 加密和 Profile lock 保护。出现验证码、访问限制或 Google OAuth 的“不安全浏览器”提示时，请在普通 Chrome 中使用合法人工登录方式处理；软件不会绕过限制。

若 Playwright Chromium 尚未安装但 Windows 已有 Chrome，正式采集会使用 Chrome 作为 Playwright 的 executable fallback；这与人工登录入口的普通 Chrome 进程是两个明确分开的生命周期。

## 输出

每项任务建立独立目录：`关键词_时间/images/`、必要时的 `videos/`、`metadata.csv`、`failed.csv`、`task.json`。X 作者任务目录使用 `x_username_时间`，首页任务使用 `x_首页推荐流_时间`。mixed 模式以全局成功顺序编号，例如 `0001.jpg`、`videos/0002.mp4`。CSV 新增 `media_type`、`media_url`、`duration`、`video_width`、`video_height`、`bitrate` 等字段；旧 CSV 和旧 task.json 仍可读取。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## 打包 EXE

依赖安装完成后执行：

```powershell
.\scripts\build_exe.ps1
```

输出文件为 `dist\Social Media Asset Collector\Social Media Asset Collector.exe`。本轮仅准备了打包脚本和图标资源，未重新打包 EXE。在未安装 Python 的 Windows 机器上，仍需随 EXE 一起分发 Playwright Chromium 浏览器（或首次在该机器安装浏览器）；这是 Playwright 的运行时依赖。

## 当前进度记录

* Phase：Bundled FFmpeg + Windows Release；基线 HEAD：`68a9451958cdff6e82603afc10832e4acb25b7a2`
* FFmpeg：内置 `vendor/ffmpeg/ffmpeg.exe`，当前为 Gyan Essentials build 8.1.2，配置含 `--enable-gpl --enable-version3`，按 GPLv3-or-later 分发并保留 `vendor/ffmpeg/LICENSE.txt`
* PyInstaller：稳定 onedir；发布目录 `dist/SocialMediaCollector/`；EXE：`SocialMediaCollector.exe`；构建脚本会清理并重建 `build/`、`dist/`，不触碰 Profile、源码、`.git` 或 `.venv`
* FFmpeg resolver：打包 `_MEIPASS/vendor/ffmpeg/ffmpeg.exe` → 源码 `vendor/ffmpeg/ffmpeg.exe` → 可选 system PATH；发布版不要求系统 PATH，不含开发机绝对路径
* 发布包资源：包含 GUI/Qt、Playwright Python driver、图标和 FFmpeg 许可证；不包含 browser Profile、Cookie、用户下载目录、tests、`.venv` 或 `.git`
* EXE 基础回归 PASS：打包 GUI 正常启动/关闭；窗口标题和资源加载正常；普通 Chrome 登录入口使用固定 Profile，未使用 Playwright、automation、remote debugging、headless 或 `--no-sandbox`
* Pinterest EXE 回归 PASS：search 3 张（发现 17、成功 3、跳过 1、失败 0）；home 3 张（发现 17、成功 3、跳过 1、失败 0）；current_page 3 张（发现 30、成功 3、失败 0），未自动导航
* X EXE 回归 PASS：作者图片 3 张；首页仅图片 3 张；首页仅视频 2 个真实 MP4；视频含 H.264/AAC 音画流
* Bundled FFmpeg Gate PASS：清空打包 EXE 子进程 PATH 且不提供 `D:\Tools\FFmpeg` 时，首页仅视频仍成功 2 个 MP4；发布运行时未读取开发机 FFmpeg
* Release Gate：人工验收 PASS；Pinterest EXE、X 作者、X 首页图片、X 首页视频、X 首页 mixed、Bundled FFmpeg、Persistent Profile、Windows EXE 全部 PASS；mixed 已实际确认图片与视频共同采集
* 浏览器会话：人工登录仍使用普通 Chrome，正式采集仍使用 Playwright `launch_persistent_context`；Pinterest / X 共用 `%LOCALAPPDATA%\\PinterestLocalDownloader\\browser-data`，不迁移、不打包 Profile
* 图标：`assets/app_icon.png`、`assets/app_icon.ico` 已随 EXE 接入；PyInstaller 构建日志确认图标已嵌入，GUI 窗口图标资源加载正常
* 自动回归：pytest 17 passed；compile/import、PySide6、Playwright、FFmpeg bundled resolver、GUI 基础启动、打包 GUI 启动、`git diff --check` 均 PASS
* 已知风险：X DOM/selector、动态 Feed、广告节点和 HLS 结构可能变化；平台可能出现登录、验证码和访问频率限制；HLS 加密播放列表暂不支持；视频停止通常在当前 FFmpeg 媒体完成后生效；Pinterest CDN 偶发 403；当前 FFmpeg build 为 GPL，未来商业分发需另行评估 LGPL-compatible build
* Git：本地 Release checkpoint 待提交，未 push；`.venv`、browser-data、Cookie、登录数据、测试媒体和临时 `.part` 不进入 Git
* 下一步：Release 固化完成后停止新增功能；后续仅按需处理发布维护、签名或 LGPL-compatible FFmpeg 评估

## MVP 范围

已具备单任务、后台线程、暂停/继续/停止、可见持久化浏览器、滚动收集、有限重试、验证/尺寸过滤/哈希去重、CSV 元数据与 GUI。Pinterest 页面结构或访问策略若变化，可能需要调整 DOM 选择逻辑；本版本不提供登录绕过、验证码绕过或网页接口破解。
