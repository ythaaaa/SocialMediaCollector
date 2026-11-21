# SocialMediaCollector（社媒素材采集器）

SocialMediaCollector 是一款 Windows 桌面应用，用于按平台和任务模式整理 Pinterest、YouTube、X（Twitter）与 Instagram 的图片、视频或音频素材。应用支持任务队列、暂停/继续、去重和任务历史，下载内容保存在用户指定的本地目录。

当前公开开发分支为 `v2.1-dev`。该分支持续开发中；请从 [GitHub Releases](https://github.com/ythaaaa/SocialMediaCollector/releases) 选择可用安装包，或从下方源码步骤运行开发版。

## 功能清单

| 平台 | 支持的模式 | 内容类型 | 采集方式 |
| --- | --- | --- | --- |
| Pinterest | 关键词、单条 Pin、用户、Board、首页推荐、当前页面 | 图片、视频、混合 | gallery-dl helper 或浏览器 / Playwright |
| YouTube | 自动识别链接、单条视频、播放列表、频道、关键词搜索 | 视频、音频、封面 | yt-dlp、yt-dlp-ejs；Shorts 可选浏览器辅助搜索 |
| X（Twitter） | 作者、搜索、首页推荐 | 图片、视频、混合 | gallery-dl helper 或浏览器 / Playwright |
| Instagram | 单条帖子 / Reel、作者、Hashtag | 图片、视频、混合 | gallery-dl helper |

Instagram 的 Hashtag 模式是标签采集，不是普通关键词全文搜索。各平台能否访问特定内容，仍取决于平台页面、账号登录状态、地区和平台策略。

通用任务功能包括目标数量、媒体类型选择、尺寸筛选、下载间隔、暂停 / 继续 / 停止、任务队列、任务历史、当前任务去重和可选历史去重。任务目录可包含 `metadata.csv`、`failed.csv` 与 `task.json`。

## Windows 环境要求

- Windows 10 或 Windows 11，64 位。
- 使用安装包时无需单独安装 Python、FFmpeg 或 gallery-dl；Pinterest / X / Instagram 登录相关模式会按界面提示使用浏览器。
- 从源码运行时需要 64 位 Python 3.12。Pinterest 首页推荐、当前页面以及浏览器辅助的 YouTube Shorts 搜索需要可用的 Google Chrome。
- YouTube 下载需要可用的 Deno JavaScript runtime 和 `yt-dlp-ejs`；源码运行时需自行安装 Deno 并确保 `deno` 可从 `PATH` 找到。打包版会把 Deno 放入应用运行目录。
- 从源码构建 EXE 还需要项目中的 FFmpeg runtime、Windows SDK 所需的 ICU 文件，以及本仓库列出的 Python 依赖。构建安装程序另需 Inno Setup 6。

## Windows 安装

### 使用 GitHub Releases 安装包

1. 打开 [GitHub Releases](https://github.com/ythaaaa/SocialMediaCollector/releases)。
2. 下载对应版本的 `SocialMediaCollector-Setup-*.exe` 并运行。
3. 安装器默认仅为当前 Windows 用户安装；安装完成后从开始菜单或桌面快捷方式启动。

开发分支的源码功能不一定已经包含在某个 Release 安装包中；需要体验 `v2.1-dev` 时请按“源码安装和启动”操作。

### 源码安装和启动

在 PowerShell 中执行：

```powershell
git clone --branch v2.1-dev https://github.com/ythaaaa/SocialMediaCollector.git
Set-Location SocialMediaCollector
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe app.py
```

应用窗口打开后选择平台、采集模式、保存目录和目标数量，再开始任务。源代码运行 YouTube 时需另外安装 Deno；若只运行已打包的安装版，不需要单独安装 Deno。

Pinterest、X、Instagram 的 gallery-dl 模式使用单独构建的 helper，而不是直接依赖 `pip install gallery-dl`。如需从源码运行这些模式，请按“EXE 与安装包构建”中的步骤构建并传入 helper。

## Pinterest 使用说明

### 首页推荐

1. 选择 Pinterest 和“首页推荐”。
2. 点击“打开浏览器 / 登录”，在采集器托管浏览器中登录 Pinterest，并打开首页推荐流。
3. 设置图片、视频或混合类型，以及目标数量和尺寸筛选。
4. 开始任务。采集器读取首页内容并下载符合筛选条件的素材。

### 当前页面下载

1. 先点击“打开浏览器 / 登录”，在托管浏览器中登录并导航到要采集的 Pinterest 页面。
2. 选择“当前页面”，确认界面显示的当前页地址正确。
3. 设置目标数量和筛选后开始任务。采集器从当前页面读取素材，不会替用户导航到另一页面。

### 其他 Pinterest 模式

- **关键词：** 输入搜索词。
- **单条 Pin：** 输入 Pinterest Pin 链接；公开 Pin 可匿名读取，受限内容可能需要登录。
- **用户：** 输入用户名或用户主页地址，按用户 Pins 采集。
- **Board：** 输入具体 Board 地址；当前版本不会自动展开 Board Sections。

Search、Single Pin、User 和 Board 使用 gallery-dl helper。需要登录时，先在应用浏览器中登录；应用会按功能需要读取 Cookie。请勿把浏览器 Profile、Cookie 文件或运行日志提交到仓库或公开分享。

## YouTube 使用说明

1. 选择 YouTube。
2. 选择自动识别链接、单条视频、播放列表、频道或关键词搜索。
3. 链接模式输入 YouTube 视频、Shorts、播放列表或频道地址；关键词模式每行填写一个搜索词。
4. 按界面选择视频、音频或封面，以及视频质量、搜索内容类型、时长和日期范围。
5. 如公开访问受限，可选择 Chrome / Firefox Cookie 来源，或本地 `cookies.txt` 文件；Cookie 只用于本机请求，请勿上传。
6. 设置目标数量和保存目录后开始任务。

YouTube JavaScript challenge 求解依赖 Deno 和 `yt-dlp-ejs`。源码运行时如果提示缺少 JavaScript runtime，请安装 Deno 并确认 `deno --version` 可在 PowerShell 中执行。浏览器辅助 Shorts 搜索需要 Chrome；其他 YouTube 搜索和下载通过 yt-dlp 处理。播放列表或频道会持续处理条目，直到达到成功数量或条目耗尽。

## 配置、数据与日志

- 采集任务的输出目录由界面中的保存位置决定。
- 浏览器 Profile、Cookie Bridge 数据库、应用数据库和设置保存在 Windows 用户本地应用数据目录，不在源码目录中。
- 程序不会要求用户把账号密码写进源码。不要把真实 `.env`、Cookie 文件、Token、浏览器 Profile、数据库、日志或下载文件提交到 Git。
- 源码运行时日志默认保存在 `%LOCALAPPDATA%\PinterestLocalDownloader\diagnostics`。诊断版启动脚本使用项目根目录下的 `diagnostics-output`；该目录已加入 `.gitignore`。
- 日志会记录启动环境、任务阶段和未处理 / 已捕获异常。分享日志前仍应检查是否含有不应公开的本机信息。

## 常见问题

### Pinterest 没有采集到内容

确认托管浏览器已经登录、当前页面属于预期 Pinterest 页面，且页面内容已加载。首页推荐和当前页面只读取浏览器当前可访问的内容；尺寸筛选也可能跳过低于设定大小的素材。

### 素材被跳过

检查任务目录中的 `failed.csv` 和应用任务记录中的跳过原因。常见情况包括尺寸低于筛选值、当前任务内重复、内容类型不符合选择。跳过和下载失败是不同状态。

### YouTube 提示验证或 JavaScript challenge 错误

确认 yt-dlp 依赖、Deno 和 `yt-dlp-ejs` 可用。需要账号访问时，在界面选择本地 Chrome / Firefox Cookie 来源或 Cookie 文件。平台验证页面也可能要求稍后重试。

### HTTPS / CA 证书错误

当前 httpx 使用 `SSL_CERT_FILE`、`SSL_CERT_DIR` 环境变量指定的证书位置；若均未配置，则使用随 httpx 安装的 certifi CA bundle。`REQUESTS_CA_BUNDLE` 会被诊断日志记录，但不是 httpx 的 CA 选择变量。请先检查 `environment.log` 中的实际 CA 来源、路径和可读状态，并保留证书验证，不要使用 `verify=False`。

在 2026-11-21 的两次 Pinterest 实际任务中，首页推荐和当前页面均完成下载，运行日志显示 httpx CA 初始化阶段通过，未出现 TLS 错误。此前报告的 TLS 问题在这两次任务中没有复现；目前没有足够证据确定此前问题的根因，也不应将这两次成功描述为对旧根因的修复。

### 启动或运行异常

查看诊断目录中的 `startup.log`、`environment.log`、`task-events.jsonl` 和 `runtime-crash.log`。错误报告请同时说明平台、模式和操作步骤；分享前请检查日志中的本机路径及其他隐私内容。

## EXE 与安装包构建

### 构建主程序

在满足前述 Windows 和 Python 环境要求、已准备好仓库内 FFmpeg runtime 与 Deno 后，执行：

```powershell
.\scripts\build_exe.ps1
```

成功后主程序位于 `dist\SocialMediaCollector\SocialMediaCollector.exe`。脚本会把应用资源、yt-dlp / yt-dlp-ejs、FFmpeg 和 Deno 放入 onedir 输出目录。构建前会检查 Python SSL 模块；若检测到本机专用 CA policy 修改，脚本会在清理旧构建输出前停止。请使用未修改的官方 CPython 3.12 构建，避免把构建机的信任策略嵌入应用。需要指定 Python 时可传 `-PythonPath C:\Path\To\python.exe`。

### 构建 gallery-dl helper（可选）

Pinterest Search / Single Pin / User / Board、X Author / Search 及 Instagram 模式需要对应 helper。构建脚本要求一个匹配项目指定 commit 的 gallery-dl Git checkout，并会安装 helper 自己的锁定依赖：

```powershell
git clone https://github.com/mikf/gallery-dl.git ..\gallery-dl
git -C ..\gallery-dl checkout 8b9a56d6b1a53bc13a80894625fd58b24de3063a
.\scripts\build_gallerydl_helper.ps1 -GalleryDlSource ..\gallery-dl
.\scripts\build_exe.ps1 -GalleryDlHelperRelease .\dist-gallerydl-helper\instagram-gallerydl-helper
```

### 构建安装程序

主程序构建完成后，安装 Inno Setup 6 并执行：

```powershell
.\scripts\build_installer.ps1
```

安装程序输出到 `release`。构建目录和输出目录不应提交到 Git。

## 测试

在项目根目录执行完整测试：

```powershell
.\.venv\Scripts\python.exe -m pytest
```

YouTube 自动化测试验证解析、配置、候选处理和下载逻辑；自动化测试不等同于每次发布都进行真实 YouTube 网络下载。

## 第三方组件与许可证

- **gallery-dl 1.32.14：** GPL-2.0，作为独立 helper 进程分发。上游源代码、版本与许可证信息见 [gallery-dl](https://github.com/mikf/gallery-dl) 和构建输出中的 `licenses/gallery-dl/`。
- **FFmpeg：** 使用未修改的 BtbN Windows x64 LGPL shared 构建。来源、构建信息和许可证见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) 与 `third_party/ffmpeg/`。
- 其他 Python 依赖的许可证由各上游项目提供；版本约束见 [`requirements.txt`](requirements.txt) 与 [`scripts/gallerydl_helper_runtime_requirements.txt`](scripts/gallerydl_helper_runtime_requirements.txt)。

仓库目前没有新增主项目开源许可证文件；本说明不会替项目选择或宣告新的许可证。
