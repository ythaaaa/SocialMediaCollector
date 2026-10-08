# SocialMediaCollector / 社媒素材采集器

Windows 桌面端多平台图片、视频采集工具，面向 Pinterest、X/Twitter 和 Instagram。

## 版本状态

| 版本 | 状态 | 说明 |
| --- | --- | --- |
| `v2.0.0` | 稳定版 | 面向普通用户的稳定版本，下载请前往 [GitHub Releases](https://github.com/ythaaaa/SocialMediaCollector/releases) |
| `v2.1.0-dev` | 开发版 / Pre-release | 当前 `v2.1-dev` 分支，用于开发测试，不代表正式稳定版 |

## 平台支持

| 平台 | 模式 | 图片 | 视频 | 当前采集方式 |
| --- | --- | --- | --- | --- |
| Pinterest | Search | 支持 | 支持 | gallery-dl packaged helper |
| Pinterest | Single Pin | 支持 | 支持 | gallery-dl packaged helper |
| Pinterest | User | 支持 | 支持 | gallery-dl packaged helper |
| Pinterest | Board | 支持 | 支持 | gallery-dl packaged helper |
| Pinterest | Home | 支持 | 支持 | Browser / Playwright |
| Pinterest | Current Page | 支持 | 支持 | Browser / Playwright |
| X / Twitter | Author | 支持 | 支持 | gallery-dl packaged helper |
| X / Twitter | Search | 支持 | 支持 | gallery-dl packaged helper |
| X / Twitter | Home | 支持 | 支持 | Browser / Playwright |
| Instagram | Single Post、Reel、Author、Hashtag | 支持 | 支持 | gallery-dl packaged helper |
| YouTube | 开发中 | — | — | 尚未完成 |

Instagram 当前支持的是 Hashtag / 标签采集，不是普通关键词全文搜索。

## Instagram v2.1

当前开发版已完成：

- gallery-dl 1.32.14 packaged helper
- Single Post、Reel、Author、Hashtag
- Carousel
- Image / Video / Mixed
- Cookie Bridge
- Direct MP4 / DASH 视频与 FFmpeg 合并
- target count
- Pause / Resume / Stop
- 当前任务去重与可选历史去重
- Unicode / Emoji metadata
- Task History

正常 Instagram 采集阶段不启动 Collection Chrome。首次登录或 Cookie 失效时，仍可能需要用户打开浏览器并人工完成 Instagram 登录。

## Pinterest v2.1

当前开发版已完成：

- Search、Single Pin、User / Profile、Board
- Pinterest Cookie Bridge
- Image / Video / Mixed
- gallery-dl packaged helper
- Search / User / Board 使用登录 Cookie；Public Single Pin 可匿名
- Direct MP4、HLS、fMP4 / CMAF、`EXT-X-MAP`、`EXT-X-BYTERANGE`
- 视频完整性验证
- Pause / Resume / Stop
- 当前任务去重与可选历史去重

Search、Single Pin、User 与 Board 的正常采集阶段不启动 Collection Chrome。Pinterest Home 与 Current Page 继续使用 Browser / Playwright。

User 模式可输入用户名、用户主页、`/pins/`、`/_created/` 或 `/_saved/` 链接，统一按 All Pins 语义处理。

Board 模式输入具体 Board / 画板链接，只采集该 Board 的 Pins；当前不会自动展开 Section。

## X / Twitter v2.1

当前开发版已完成：

- 独立 X Cookie Bridge
- 独立 X browser profile
- DPAPI encrypted cookie store
- Author / Media Timeline
- Search
- Media / Top scope
- Image / Video / Mixed
- gallery-dl 1.32.14 packaged helper
- 原图优先
- Direct MP4 / HLS
- Animated GIF → video
- Prompt Detection 保留
- target count
- 当前任务去重与可选历史去重
- Pause / Resume / Stop
- Queue / History compatible

X Author 支持输入用户名、`@username`、x.com 用户主页或 twitter.com 用户主页，内部使用作者 Media Timeline。X Search 支持 Media / Top 范围，以及 Image / Video / Mixed 类型。

X Author 与 Search 的正常采集阶段不启动 Collection Chrome；X Home 的图片和视频支持继续使用 legacy Browser / Playwright。

## 去重语义

- 当前任务内重复素材始终避免。
- 默认允许重新下载历史任务已经下载过的素材。
- 开启“跳过历史已下载素材”后，才会启用历史去重。

## Windows 安装

普通用户推荐使用 [GitHub Releases](https://github.com/ythaaaa/SocialMediaCollector/releases) 中的 Installer。

Installer 已包含：

- SocialMediaCollector 主程序及运行时
- FFmpeg
- Instagram / Pinterest gallery-dl packaged helper

用户不需要另外安装 Python、gallery-dl 或 FFmpeg。Pinterest Home / Current Page 与 X 的浏览器采集和登录仍按软件提示使用本机浏览器；Instagram 与已支持的 Pinterest gallery-dl 模式在正常 Collection 阶段不需要打开 Collection Chrome。

### Instagram 登录

1. 选择 Instagram 及采集模式。
2. 如提示登录，点击“打开浏览器 / 登录”。
3. 在浏览器中人工完成登录、验证码或 2FA。
4. Cookie Bridge 保存登录状态。
5. 之后正常采集不需要启动 Collection Chrome。

README 不包含 Cookie、账号凭据或内部安全实现细节。

## 任务能力

- Batch Queue
- Task History
- Pause / Resume / Stop
- target count
- Image / Video / Mixed
- 最小尺寸过滤
- 下载间隔
- 当前任务去重
- 可选历史去重
- `metadata.csv`、`failed.csv` 和 `task.json`

## 开发者说明

当前开发分支：`v2.1-dev`

运行测试：

```powershell
.\.venv\Scripts\python.exe -m pytest
```

当前开发状态已验证 `338 passed`；该数字不是固定版本徽章。

gallery-dl 说明：

- Version `1.32.14`
- 使用未修改的 upstream 源码
- 作为独立 packaged helper process 分发
- 通过 JSONL IPC 与主程序通信

## Third-party components / 第三方组件

### gallery-dl

- Version: `1.32.14`
- License: GPL-2.0
- Upstream: [mikf/gallery-dl](https://github.com/mikf/gallery-dl)
- 发行包中的完整材料：`licenses/gallery-dl/`

### FFmpeg

Installer 包含 FFmpeg runtime 及对应许可证、来源和构建信息。完整材料见发行包中的 `licenses/ffmpeg/` 与项目第三方声明文件。

本项目不在 README 中对 GPL 或其他第三方许可证与主项目之间的法律适用范围作结论，请以随包许可证和法律意见为准。

## Roadmap

以下是计划或调查方向，不代表当前已经完成：

- Pinterest Home / Current Page 保留 Browser / Playwright
- X Home 保留 Browser / Playwright
- X Single Tweet：future candidate
- X Likes / Lists / Hashtag：future evaluation
- YouTube support
- Instagram 普通 Keyword Search：research / not implemented

当前 X 的 Author 与 Search 已使用 gallery-dl packaged helper；X Home 继续使用 Browser / Playwright。Pinterest 的 Search、Single Pin、User 与 Board 已使用 gallery-dl packaged helper。

## 源码运行（开发者）

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\playwright.exe install chromium
.\.venv\Scripts\python.exe app.py
```

如仅使用普通用户 Installer，不需要执行以上开发环境步骤。
