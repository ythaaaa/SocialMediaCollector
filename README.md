# Pinterest Local Downloader

Windows 本地 Pinterest 图片下载工具。它通过可见的 Pinterest 搜索页面收集公开图片资源，并将经验证的图片、元数据和失败记录保存到用户选定的本地目录。项目不连接 MUSE、数据库、云存储或任何服务器。

## 环境与安装

本机可用 `py.exe` 时，在项目目录执行：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
py -3 -m pip install -r requirements.txt
py -3 -m playwright install chromium
```

如遇 PowerShell 执行策略限制，运行 `Set-ExecutionPolicy -Scope Process Bypass` 后重新激活虚拟环境。

## 启动

图形界面：

```powershell
py -3 app.py
```

命令行核心（用于小规模验证）：

```powershell
py -3 -m src.downloader.cli "东方古风美女" --count 20 --output "D:\Pinterest素材"
```

首次任务会打开可见浏览器。请自行登录 Pinterest；登录状态只保存在项目的 `browser-data/`，不会收集账号密码。出现验证码或访问限制时，请在浏览器内处理后再继续；软件不会绕过限制。

## 输出

每项任务建立独立目录：`关键词_时间/images/`、`metadata.csv`、`failed.csv`、`task.json`。下载后使用 Pillow 验证图片、检查实际尺寸、按 SHA-256 内容哈希去重，并依照真实图片格式确定扩展名。

## 测试

```powershell
py -3 -m pytest -q
```

## 打包 EXE

依赖安装完成后执行：

```powershell
.\scripts\build_exe.ps1
```

输出文件为 `dist\Pinterest Local Downloader\Pinterest Local Downloader.exe`。在未安装 Python 的 Windows 机器上，仍需随 EXE 一起分发 Playwright Chromium 浏览器（或首次在该机器安装浏览器）；这是 Playwright 的运行时依赖。

## MVP 范围

已具备单任务、后台线程、暂停/继续/停止、可见持久化浏览器、滚动收集、有限重试、验证/尺寸过滤/哈希去重、CSV 元数据与 GUI。Pinterest 页面结构或访问策略若变化，可能需要调整 DOM 选择逻辑；本版本不提供登录绕过、验证码绕过或网页接口破解。
