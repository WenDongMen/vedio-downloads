# 视频下载器 v5.0

一个面向 Windows 的桌面视频下载工具：粘贴网页 / 视频地址，嗅探页面上真实播放的视频，挑清晰度下载，支持批量、HLS 分片、B 站多 P、抖音多档清晰度。

界面基于 PyQt6 + PyQt6-Fluent-Widgets（Fluent 风格，圆角卡片 + 柔光渐变背景），下载核心复用上一代 v4.5 的后端实现（yt-dlp / Playwright / FFmpeg）。

## 功能

- **嗅探下载**：Playwright 捕获页面真实媒体请求，自动过滤占位动画与广告资源，按清晰度和体积排序
- **抖音**：分享短链自动跳转，调用作品详情接口补齐 1080P / 720P / 576P 等档位；音视频分离流自动配对
- **B 站**：yt-dlp 解析，多 P 视频在批量页自动展开成多集
- **直链 / HLS**：mp4 直链交给 FFmpeg 远程拉取；m3u8 主清单可列多档，分片并发下载后合并为 MP4
- **仅音频**：FFmpeg 提取音轨，支持 mp3 / m4a
- **批量下载**：每行一个地址，可设保存目录与画质（最佳 / 1080P / 720P / 480P / 360P / 仅音频），已存在文件自动跳过
- **代理抓包**：内置 mitmproxy，监听 127.0.0.1:8888，浏览器或手机走代理即可把视频抓回资源列表
- **历史记录**：本地 JSON 记录文件名、大小、时间、原链接，可打开文件、打开所在文件夹、复制链接、删除
- **辅助能力**：剪贴板监听自动填地址、系统托盘、导出资源 JSON、导出 HAR、导出 M3U8 清单、导出运行日志
- **加入群聊**：QQ 群与 Telegram 频道二维码、群号复制、打开链接

## 运行

环境要求：

- Windows 10 / 11
- Python 3.13 或更高（开发环境为 3.14）
- FFmpeg（`ffmpeg.exe`），可在设置页指定路径，或放到程序目录

安装依赖：

```bash
pip install PyQt6 PyQt6-Fluent-Widgets yt-dlp playwright requests pillow pycryptodome mitmproxy
python -m playwright install chromium
```

启动：

```bash
python app.py
```

或双击 `启动.bat`（使用绝对路径的 `pythonw.exe`，无控制台窗口）。

首次启动会弹使用说明，之后可在设置页关闭。

## 目录与配置

- 程序目录：`app.py`（界面层）、`core.py`（引擎封装，通过 importlib 加载 v4.5 后端）
- 数据目录：`F:\MenWenDong\视频下载器`（与 v4.5 共用）
  - `downloader_config.json`：设置
  - `download_history.json`：下载历史
  - `video_downloader.log`：运行日志

核心后端默认从 `F:\MenWenDong\视频下载器\V4.5.py` 加载，可用环境变量 `VD_CORE` 指定其他路径；找不到时会尝试程序目录下的 `V4.5.py`。

## 已知限制

- 微信视频号解析依赖登录态，未在无登录环境实测
- 代理抓包抓 HTTPS 需要先在浏览器里安装 mitmproxy 证书（访问 `http://mitm.it`）
- 需要登录才能观看的内容，请先在设置里关闭「无头浏览器」，在弹出窗口手动登录后再嗅探

## 许可

GPLv3，见 `LICENSE`。免费使用，不收费。
