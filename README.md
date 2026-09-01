# Pinterest Local Downloader

Windows 本地社交媒体图片采集工具。它通过可见的 Pinterest / X 页面收集公开图片资源，并将经验证的图片、元数据和失败记录保存到用户选定的本地目录。项目不连接 MUSE、数据库、云存储或任何服务器。

当前发布版本：`Social Media Collector v1.0.0`

当前内部开发版本：`2.0.0-dev`（V2 Phase 1，尚未创建新的正式发布版本）

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

输出文件为 `dist\SocialMediaCollector\SocialMediaCollector.exe`。当前 Phase 会重新构建 LGPL candidate onedir 包；在未安装 Python 的 Windows 机器上，仍需随 EXE 一起分发 Playwright Chromium 浏览器（或首次在该机器安装浏览器）；这是 Playwright 的运行时依赖。

## 当前进度记录

* 产品版本：`Social Media Collector v1.0.0`；本 Phase 基线 HEAD：`6e098f8135dd5d22d55933bfeec7df3cde1f284f`
* Phase：商业发布准备 / FFmpeg LGPL 替换；Release Gate：PASS
* FFmpeg：内置 BtbN `n8.1.2-44-g7c533d0f86-20260823`，Windows x86_64 `LGPL shared` build；FFmpeg source revision `g7c533d0f86`；不含 `--enable-gpl` 或 `--enable-nonfree`
* FFmpeg 来源：`autobuild-2026-08-23-13-03`；归档 SHA-256：`7B32983C242DD73D43C20836582572E9927986F8CB112AAFD62AA9978F4BE645`；完整许可证与来源记录见 `third_party/ffmpeg/`、`THIRD_PARTY_NOTICES.md`
* PyInstaller：稳定 onedir；发布目录 `dist/SocialMediaCollector/`；EXE：`SocialMediaCollector.exe`；构建脚本会清理并重建 `build/`、`dist/`，不触碰 Profile、源码、`.git` 或 `.venv`
* FFmpeg resolver：显式测试覆盖路径 → 打包/源码 `vendor/ffmpeg/ffmpeg.exe`（要求 7 个 shared DLL）→ 可选 system PATH；正式发布版不要求系统 PATH，不含开发机绝对路径
* 发布包资源：包含 GUI/Qt、Playwright Python driver、图标、FFmpeg LGPL runtime 和许可证；不包含 browser Profile、Cookie、用户下载目录、tests、`.venv` 或 `.git`
* EXE 基础回归 PASS：打包 GUI 正常启动/关闭；窗口标题和资源加载正常；普通 Chrome 登录入口使用固定 Profile，未使用 Playwright、automation、remote debugging、headless 或 `--no-sandbox`
* Pinterest EXE 回归 PASS：search 3 张（发现 17、成功 3、跳过 1、失败 0）；home 3 张（发现 17、成功 3、跳过 1、失败 0）；current_page 3 张（发现 30、成功 3、失败 0），未自动导航
* X EXE 回归 PASS：作者图片 3 张；首页仅图片 3 张；首页仅视频 2 个真实 MP4；视频含 H.264/AAC 音画流
* Bundled FFmpeg Gate PASS（上一 GPL Release）：清空打包 EXE 子进程 PATH 且不提供 `D:\Tools\FFmpeg` 时，首页仅视频成功 2 个 MP4；本 Phase 已用 LGPL candidate 重复验证
* LGPL 候选隔离回归 PASS：清空子进程 PATH 且不提供 `D:\Tools\FFmpeg` 时，X 首页仅视频目标 3 成功 3 个真实 MP4；mixed 目标 5 成功 5 个（4 图片 + 1 视频）；候选 FFmpeg 解码/封装正常
* LGPL 候选 EXE 回归 PASS：GUI 正常启动；无系统 FFmpeg PATH 时，X 首页仅视频目标 2 成功 2 个真实 MP4 且包内 FFmpeg 解码返回 0；mixed 目标 3 成功 3 个；Pinterest 首页推荐目标 3 成功 3 个
* Release Gate：人工验收 PASS；X 首页仅视频、音画、mixed、Persistent Profile、GUI/图标/任务栏和 Windows EXE 均已确认正常
* 浏览器会话：人工登录仍使用普通 Chrome，正式采集仍使用 Playwright `launch_persistent_context`；Pinterest / X 共用 `%LOCALAPPDATA%\\PinterestLocalDownloader\\browser-data`，不迁移、不打包 Profile
* 图标：`assets/app_icon.png`、`assets/app_icon.ico` 已随 EXE 接入；PyInstaller 构建日志确认图标已嵌入，GUI 窗口图标资源加载正常
* 自动回归：pytest 19 passed；compile/import、PySide6、Playwright、FFmpeg resolver、LGPL 共享 DLL 完整性、GUI 基础启动、隔离 EXE 视频/mixed、`git diff --check` 均 PASS
* 已知风险：X DOM/selector、动态 Feed、广告节点和 HLS 结构可能变化；平台可能出现登录、验证码和访问频率限制；HLS 加密播放列表暂不支持；视频停止通常在当前 FFmpeg 媒体完成后生效；Pinterest CDN 偶发 403；LGPL 选择不包含 GPL-only 的 libx264/libx265，未来若引入其他 FFmpeg 编码能力需重新审查许可证与运行时依赖
* Git：Release 固化为本地 commit 和 annotated tag `v1.0.0`；不 push、不创建 GitHub Release；`.venv`、browser-data、Cookie、登录数据、测试媒体和临时 `.part` 不进入 Git
* V2 Phase 1：内部开发版本 `2.0.0-dev`；GUI 按 `platform + collection_mode` 隔离并恢复输入状态；Pinterest 搜索 URL 输入会被拦截；X 作者输入在进入采集逻辑前统一为 username；现有 `TaskConfig` 扩展了 task_id、source_value、download_interval、save_dir、only_author_posts 和 status 兼容接口，为后续任务队列预留结构
* 下一步：仅处理必要的发布维护、签名或远程发布策略；不新增采集功能、不迁移 Profile、不内置 FFmpeg 源码

## v1.0.0 发布内容

* Pinterest：关键词搜索、首页推荐、当前页面、图片采集。
* X：作者主页、首页推荐、仅图片、仅视频、图片 + 视频 mixed。
* Runtime：Windows PyInstaller onedir EXE、持久化 Chrome Profile、普通 Chrome 人工登录、Playwright 正式采集、bundled FFmpeg LGPL shared。
* 发布使用：不要求 Python，不要求用户手动安装 FFmpeg；需要本机 Google Chrome，登录状态保存在 `%LOCALAPPDATA%\\PinterestLocalDownloader\\browser-data`。
* 已知限制：X/Pinterest DOM 未来可能变化；登录、验证码和平台频率限制；Pinterest CDN 偶发 403；加密 HLS 暂不支持；视频停止可能等待当前 FFmpeg 任务完成；当前 Profile 路径保留历史 `PinterestLocalDownloader` 名称。

## Windows Installer Phase

* Installer：Inno Setup `6.7.3`；源文件：`installer/SocialMediaCollector.iss`；构建脚本：`scripts/build_installer.ps1`
* Installer AppId：`{8FA84253-509A-47BF-8A33-FB2C4357EFC5}`；AppVersion：`1.0.0`；产品名：`Social Media Collector`
* 安装方式：per-user，无需管理员权限；默认目录：`%LOCALAPPDATA%\\Programs\\SocialMediaCollector`
* 安装包：`release/SocialMediaCollector-Setup-v1.0.0.exe`；`release/` 仅为本地构建输出，不进入 Git
* 安装内容：完整 PyInstaller onedir、Qt/Python/Playwright runtime、bundled LGPL FFmpeg、7 个 shared DLL、图标和 `licenses/` 第三方材料；不下载或注册 Chrome/FFmpeg
* 快捷方式：可选桌面快捷方式；开始菜单包含主程序和卸载入口；快捷方式均指向安装目录
* 卸载/重装：卸载删除安装目录、快捷方式和卸载注册信息，但保留 `%LOCALAPPDATA%\\PinterestLocalDownloader\\browser-data` 与用户下载目录；覆盖安装不创建第二套应用目录
* 安装版回归 PASS：Pinterest search/home/current_page 各 3/3；X 作者 3/3；X 图片 3/3；X 视频 2/2；X mixed 3/3；安装目录 bundled FFmpeg 解码 MP4 返回 0
* 安装版无外部依赖回归：测试进程 PATH 仅为 `C:\\Windows\\System32`，未提供 `D:\\Tools\\FFmpeg`，视频仍由安装目录 LGPL FFmpeg 完成
* 安装版 Release Gate：自动安装、启动、快捷方式、Installed Apps、卸载、Profile 保留、重装恢复均 PASS；等待用户人工验收
* 安全状态：Setup 和 App EXE 当前均 unsigned；本机安装/启动测试未观察到 SmartScreen 或 Defender 提示，其他机器可能因未签名而显示系统警告；未配置代码签名证书

## MVP 范围

已具备单任务、后台线程、暂停/继续/停止、可见持久化浏览器、滚动收集、有限重试、验证/尺寸过滤/哈希去重、CSV 元数据与 GUI。Pinterest 页面结构或访问策略若变化，可能需要调整 DOM 选择逻辑；本版本不提供登录绕过、验证码绕过或网页接口破解。
