import sys
import os
import re
import time
import random
import threading
import tempfile
import shutil
import atexit
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, scrolledtext
from urllib.parse import urljoin, urlparse, unquote, parse_qsl, urlencode, urlunparse
from pathlib import Path
from typing import Dict, List, Optional, Union, Tuple
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
from logging.handlers import RotatingFileHandler
import subprocess
import json
import traceback
import queue
import hashlib
import xml.etree.ElementTree as ET
from collections import OrderedDict, deque
from datetime import datetime

# Windows 高 DPI 适配（必须在创建 Tk 窗口前调用）
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def _app_data_dir() -> str:
    """配置/历史/日志的存放目录。

    源码运行时放在脚本同目录（方便查看）；PyInstaller 单文件打包后 __file__ 位于
    临时解包目录（退出即删除），设置和历史会丢失，因此改为写入 %APPDATA%。
    """
    if getattr(sys, 'frozen', False):
        base = os.path.join(os.environ.get('APPDATA') or os.path.expanduser('~'),
                            'VideoDownloader')
        try:
            os.makedirs(base, exist_ok=True)
            return base
        except Exception:
            return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


# 日志文件滚动：5MB 单文件，保留 3 个备份
_log_file = os.path.join(_app_data_dir(), "video_downloader.log")
try:
    _file_handler = RotatingFileHandler(_log_file, maxBytes=5*1024*1024, backupCount=3, encoding='utf-8')
    _file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logger.addHandler(_file_handler)
    # 界面只显示 INFO 及以上；DEBUG（导航、档位补齐等技术细节）只落盘，方便事后排查
    logger.setLevel(logging.DEBUG)
except Exception:
    pass

APP_VERSION = "v4.5"
# 正式发布版：暂未部署在线更新服务，留空表示不发起联网检查（避免无效请求与超时等待）
UPDATE_CHECK_URL = ""

# 加入群聊入口：number=群号（QQ 才有），url=邀请链接，qr=二维码图片文件名（放程序目录即可）
# 留空就显示"待补充"，不影响弹窗打开；图片缺失时自动退化成只显示文字和按钮
GROUP_LINKS = [
    {"key": "qq", "name": "QQ 群", "number": "", "url": "",
     "qr": "group_qq.jpg", "hint": "扫码进群，也可以复制群号在 QQ 里搜索"},
    {"key": "tg", "name": "Telegram", "number": "", "url": "",
     "qr": "group_telegram.jpg", "hint": "需要自备网络工具才能打开 t.me 链接"},
    {"key": "gh", "name": "GitHub", "number": "", "url": "",
     "qr": "group_github.jpg", "hint": "项目源码、更新日志与版本发布"},
]


def _short_file(path_str: str, limit: int = 48) -> str:
    """日志用短路径：目录保留，超长文件名截断（抖音标题经常长到一屏放不下）"""
    try:
        directory, name = os.path.split(path_str)
        if len(name) <= limit:
            return path_str
        return os.path.join(directory, name[:limit] + '…')
    except Exception:
        return str(path_str)

# ===================== 可选依赖检测 =====================
# mitmproxy 代理抓包
try:
    import importlib.util as _importlib_util
    # 仅检测是否安装；具体类在 ProxySniffer.start() 内按需导入
    MITMPROXY_AVAILABLE = _importlib_util.find_spec("mitmproxy") is not None
except Exception:
    MITMPROXY_AVAILABLE = False

# pystray 系统托盘 + PIL 图标
PYSTRAY_AVAILABLE = False
try:
    import pystray
    from PIL import Image, ImageDraw
    PYSTRAY_AVAILABLE = True
except ImportError:
    pass

# ===================== Playwright 浏览器嗅探 =====================
PLAYWRIGHT_AVAILABLE = False
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    logger.warning("Playwright 未安装，浏览器嗅探功能不可用")

# ===================== 请求和解密库 =====================
REQUESTS_AVAILABLE = False
try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    logger.warning("requests 未安装，网页/分片下载功能不可用")

# AES 解密单独判定：只缺 pycryptodome 时不应影响普通下载
CRYPTO_AVAILABLE = False
AES = None
try:
    from Crypto.Cipher import AES
    CRYPTO_AVAILABLE = True
except ImportError:
    try:
        from Cryptodome.Cipher import AES
        CRYPTO_AVAILABLE = True
    except ImportError:
        logger.warning("pycryptodome 未安装，AES-128 加密的 M3U8 无法本地解密")

# ===================== yt-dlp 引擎（整合自 yt-dlp-master） =====================
# 支持 YouTube / Bilibili / TikTok 等 1000+ 站点的解析与下载
# import yt_dlp 实测约 1.9 秒，放在启动路径上会让窗口迟迟不出现（看起来像卡死），
# 所以这里只用 find_spec 探测是否安装，真正导入推迟到第一次解析/下载时。
YTDLP_AVAILABLE = False
yt_dlp = None
_YTDLP_CANCELLED = None
try:
    import importlib.util as _importlib_util
    YTDLP_AVAILABLE = _importlib_util.find_spec("yt_dlp") is not None
except Exception:
    YTDLP_AVAILABLE = False


def _load_ytdlp():
    """首次使用时导入 yt_dlp（返回模块对象），避免拖慢启动"""
    global yt_dlp, _YTDLP_CANCELLED, YTDLP_AVAILABLE
    if yt_dlp is not None:
        return yt_dlp
    try:
        import yt_dlp as _mod
        from yt_dlp.utils import DownloadCancelled as _cancel
    except ImportError:
        YTDLP_AVAILABLE = False
        logger.warning("yt-dlp 未安装，yt-dlp 解析下载功能不可用。请安装: pip install yt-dlp")
        raise
    yt_dlp = _mod
    _YTDLP_CANCELLED = _cancel
    YTDLP_AVAILABLE = True
    return yt_dlp


class _YTDLPLogger:
    """把 yt-dlp 日志转发到应用日志回调（带级别）"""

    def __init__(self, cb):
        self._cb = cb

    def debug(self, msg):
        # yt-dlp 的调试输出非常啰嗦（下载进度、格式筛选等），界面不再转发
        return

    def info(self, msg):
        self._cb(str(msg), "INFO")

    def warning(self, msg):
        self._cb(str(msg), "WARNING")

    def error(self, msg):
        self._cb(str(msg), "ERROR")


class YTDLPEngine:
    """yt-dlp 引擎封装：站点信息解析 + 带进度/可取消的下载（整合自 yt-dlp-master）"""

    def __init__(self, ffmpeg_location="ffmpeg", proxy="", cookiefile="",
                 log_callback=None, progress_callback=None):
        self.ffmpeg_location = ffmpeg_location
        self.proxy = proxy
        self.cookiefile = cookiefile
        self.log_callback = log_callback or (lambda m, l="INFO": None)
        self.progress_callback = progress_callback or (lambda v, m="": None)
        self.cancel_event = threading.Event()
        self._ydl = None
        self._last_file = ""

    def _log(self, message, level="INFO"):
        self.log_callback(message, level)

    def _base_opts(self, **extra):
        opts = {
            'quiet': True,
            'no_warnings': False,
            'noplaylist': True,          # 只解析/下载当前视频
            'socket_timeout': 30,
            'retries': 5,
            'fragment_retries': 5,
            'ignoreerrors': False,
            'logger': _YTDLPLogger(self._log),
            'progress_hooks': [self._progress_hook],
            'postprocessor_hooks': [self._postprocess_hook],
        }
        if self.ffmpeg_location:
            opts['ffmpeg_location'] = self.ffmpeg_location
        if self.proxy:
            opts['proxy'] = self.proxy
        if self.cookiefile and os.path.isfile(self.cookiefile):
            opts['cookiefile'] = self.cookiefile
        opts.update(extra)
        return opts

    def _progress_hook(self, d):
        if self.cancel_event.is_set():
            _load_ytdlp()
            raise _YTDLP_CANCELLED()
        if d.get('status') == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            down = d.get('downloaded_bytes') or 0
            if total:
                self.progress_callback(min(100.0, down * 100.0 / total), "")
        elif d.get('status') == 'finished':
            fn = d.get('filename') or ''
            if fn and os.path.isfile(fn) and not fn.endswith(('.part', '.ytdl')):
                self._last_file = fn
            self._log(f"下载完成：{os.path.basename(fn)}", "INFO")

    def _postprocess_hook(self, d):
        if self.cancel_event.is_set():
            _load_ytdlp()
            raise _YTDLP_CANCELLED()
        if d.get('status') != 'finished':
            return
        # yt-dlp 的 postprocessor 钩子把路径放在 info_dict 里，顶层 filepath 取不到
        info = d.get('info_dict') or {}
        fn = (info.get('filepath') or info.get('_filename') or
              d.get('filepath') or d.get('filename') or '')
        if fn and os.path.isfile(fn) and not fn.lower().endswith(('.part', '.ytdl')):
            # 字幕/元数据文件不能当成视频输出路径
            if os.path.splitext(fn)[1].lower() not in (
                    '.vtt', '.srt', '.ass', '.lrc', '.json', '.description'):
                self._last_file = fn
        if d.get('postprocessor'):
            self._log(f"后处理完成：{os.path.basename(fn)}", "INFO")

    @staticmethod
    def _find_output_file(out_base: str) -> str:
        """在 out_base 同目录查找最终成品文件（合并/转码后中间文件已被删除）"""
        folder = os.path.dirname(os.path.abspath(out_base))
        prefix = os.path.basename(out_base) + "."
        media_exts = ('.mp4', '.mkv', '.webm', '.flv', '.mov', '.ts',
                      '.m4a', '.mp3', '.opus', '.ogg', '.aac', '.wav')
        best, best_mtime = "", -1.0
        try:
            for name in os.listdir(folder):
                if not name.startswith(prefix) or not name.lower().endswith(media_exts):
                    continue
                path = os.path.join(folder, name)
                if not os.path.isfile(path):
                    continue
                mtime = os.path.getmtime(path)
                if mtime >= best_mtime:
                    best, best_mtime = path, mtime
        except Exception:
            return ""
        return best

    # ---------------- 解析 ----------------
    def extract_info(self, url):
        """解析 URL，返回 yt-dlp info dict（含 formats / entries）"""
        with _load_ytdlp().YoutubeDL(self._base_opts(extract_flat=False)) as ydl:
            return ydl.extract_info(url, download=False)

    def extract_playlist(self, url, playlistend=0):
        """批量下载用：只抓列表结构（flat），不解析每个视频的清晰度/直链

        合集/多P/播放列表会返回 entries；单个视频则返回该视频自身的 info。
        """
        extra = {
            'noplaylist': False,
            'extract_flat': 'in_playlist',
            'ignoreerrors': True,
        }
        if playlistend and int(playlistend) > 0:
            extra['playlistend'] = int(playlistend)
        with _load_ytdlp().YoutubeDL(self._base_opts(**extra)) as ydl:
            return ydl.extract_info(url, download=False)

    @staticmethod
    def playlist_entries(info):
        """把播放列表/单视频的解析结果统一成条目列表"""
        if not info:
            return []
        entries = info.get('entries')
        if isinstance(entries, list):
            return [e for e in entries if e]
        if info.get('_type') == 'playlist':
            return []
        return [info]

    @staticmethod
    def get_video_entry(info):
        """从解析结果中取单个视频条目（兼容单视频/播放列表）"""
        if not info:
            return None
        if info.get('_type') == 'playlist' or info.get('entries'):
            entries = info.get('entries') or []
            for e in entries:
                if e and e.get('id'):
                    return e
            return None
        return info

    @staticmethod
    def _guess_height(f: dict) -> int:
        """从 resolution / format_note / format_id 推断画面高度

        部分站点（抖音 bit_rate 档位）只给码率不给 height，旧逻辑会把这些档位
        当成「无法识别清晰度」直接丢弃，导致列表里只剩一条低清直链。
        """
        def _ok(n: int) -> int:
            # 144p~4320p 之外的数字不是清晰度（如 yt-dlp 的格式编号 137）
            return n if 144 <= n <= 4320 else 0

        for text in (f.get('resolution'), f.get('format_note')):
            if not text:
                continue
            m = re.search(r'(\d{3,4})\s*[xX×*]\s*(\d{3,4})', str(text))
            if m:
                n = _ok(min(int(m.group(1)), int(m.group(2))))
                if n:
                    return n
            m = re.search(r'(\d{3,4})\s*[pP]\b', str(text))
            if m:
                n = _ok(int(m.group(1)))
                if n:
                    return n
        # gear_name 形如 normal_1080_0 / adapt_lowest_720_1（必须带下划线才判定，
        # 纯数字 format_id 是 yt-dlp 的格式序号，不能当清晰度）
        fid = str(f.get('format_id') or '')
        if '_' in fid:
            m = re.search(r'(?:^|_)(\d{3,4})(?:_|$)', fid)
            if m:
                return _ok(int(m.group(1)))
        return 0

    @staticmethod
    def build_video_resources(info, page_url):
        """把 yt-dlp 解析结果整理成可直接展示/下载的资源行

        只保留「自动最佳」+ 每个清晰度最优的一条 + 一条仅音频，
        避免把几十条 formats 全部塞进列表。
        """
        entry = YTDLPEngine.get_video_entry(info)
        if not entry:
            return "", []
        title = (entry.get('title') or entry.get('id') or '未命名视频').strip()
        webpage = entry.get('webpage_url') or page_url
        formats = [f for f in (entry.get('formats') or []) if f.get('format_id')]
        videos = []
        audios = []
        for f in formats:
            ext = str(f.get('ext') or '').lower()
            proto = str(f.get('protocol') or '').lower()
            if ext in ('mhtml', 'none') or proto == 'mhtml':
                continue
            if f.get('vcodec') and f.get('vcodec') != 'none':
                height = f.get('height')
                if not height:
                    height = YTDLPEngine._guess_height(f)
                    if height:
                        f = dict(f)
                        f['height'] = height
                if height:
                    videos.append(f)
            elif f.get('acodec') and f.get('acodec') != 'none':
                audios.append(f)

        def _score(f):
            return (f.get('tbr') or 0, f.get('filesize') or f.get('filesize_approx') or 0)

        def _short_side(f):
            """清晰度按短边计：竖屏 1080×1920 应记为 1080P，而不是 1920P"""
            try:
                w = int(f.get('width') or 0)
                h = int(f.get('height') or 0)
            except (TypeError, ValueError):
                return 0
            if w > 0 and h > 0:
                return min(w, h)
            return h

        best_by_height = {}
        for f in videos:
            h = _short_side(f)
            if h <= 0:
                continue
            old = best_by_height.get(h)
            if old is None or _score(f) > _score(old):
                best_by_height[h] = f

        if not videos and not audios:
            return title, []

        best_audio = max(audios, key=_score) if audios else None
        resources = []

        # 1) 自动最佳
        auto = VideoResource(
            url=webpage,
            resource_type='ytdlp',
            title=title,
            page_title=title,
            resolution='最佳',
            source='yt-dlp',
            engine='ytdlp',
            format_spec='bv*+ba/b',
            ext='mp4',
            display_format='自动',
            note='自动选择最佳视频+音频并合并',
            key=f'{webpage}#auto',
            page_url=webpage,
        )
        resources.append(auto)

        # 2) 每个清晰度一条（B 站视频轨为 MP4/m4s，音频轨为 M4A，下载时自动合并）
        for h in sorted(best_by_height, reverse=True)[:8]:
            f = best_by_height[h]
            fid = str(f.get('format_id'))
            acodec = str(f.get('acodec') or 'none')
            vcodec = str(f.get('vcodec') or '')
            # 已含音轨的整段格式：档位失效时（站点重新下发不同的 gear 名）退回最佳，
            # 否则 yt-dlp 会直接报 "Requested format is not available" 让整次下载失败
            fmt = f'{fid}/bv*+ba/b' if acodec != 'none' else f'{fid}+bestaudio/best'
            ext = str(f.get('ext') or 'mp4')
            size = f.get('filesize') or f.get('filesize_approx') or 0
            codec_label = vcodec.split('.')[0] if vcodec else ''
            if acodec == 'none' and best_audio:
                audio_ext = str(best_audio.get('ext') or 'm4a').upper()
                note_parts = [f'{h}p', f'{ext.upper()}视频']
                if codec_label:
                    note_parts.append(codec_label)
                note_parts.append(f'+{audio_ext}音频')
            elif acodec == 'none':
                note_parts = [f'{h}p', f'{ext.upper()}视频']
                if codec_label:
                    note_parts.append(codec_label)
            else:
                note_parts = [f'{h}p', ext.upper()]
                if codec_label:
                    note_parts.append(f'{codec_label}/{acodec}')
            if f.get('format_note'):
                note_parts.append(str(f.get('format_note')))
            resources.append(VideoResource(
                url=webpage,
                resource_type='ytdlp',
                title=title,
                page_title=title,
                resolution=f'{h}p',
                bandwidth=int((f.get('tbr') or 0) * 1000),
                size=int(size or 0),
                source='yt-dlp',
                engine='ytdlp',
                format_spec=fmt,
                ext=ext,
                display_format=ext.upper(),
                note=' · '.join(note_parts),
                key=f'{webpage}#fmt={fid}',
                page_url=webpage,
            ))

        # 3) 仅音频
        if best_audio:
            fid = str(best_audio.get('format_id'))
            ext = str(best_audio.get('ext') or 'm4a')
            acodec = str(best_audio.get('acodec') or '')
            codec_label = 'AAC' if 'mp4a' in acodec else (acodec.split('.')[0] if acodec else '')
            note = f'{ext.upper()}'
            if codec_label:
                note += f' · {codec_label}'
            note += ' · 仅音频'
            resources.append(VideoResource(
                url=webpage,
                resource_type='ytdlp',
                title=title,
                page_title=title,
                resolution='仅音频',
                bandwidth=int((best_audio.get('tbr') or 0) * 1000),
                size=int(best_audio.get('filesize') or best_audio.get('filesize_approx') or 0),
                source='yt-dlp',
                engine='ytdlp',
                format_spec=fid,
                ext=ext,
                display_format=ext.upper(),
                note=note,
                key=f'{webpage}#audio={fid}',
                page_url=webpage,
            ))
        return title, resources

    # ---------------- 下载 ----------------
    def download(self, url, out_base, format_spec="bv*+ba/b",
                 audio_only=False, subtitles=False):
        """下载到 out_base（不带扩展名的目标路径），返回 True/False"""
        # 说明：engine 每次任务新建，这里不再 clear()，否则用户在本方法被调用前
        # 点「停止」设置的取消信号会被抹掉（v4.2 与 UniversalDownloader 保持一致）
        self._last_file = ""
        outtmpl = out_base + ".%(ext)s"
        extra = {'format': format_spec, 'outtmpl': outtmpl, 'noprogress': True}
        if audio_only:
            extra.update({
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
            })
        if subtitles:
            extra.update({
                'writesubtitles': True,
                'writeautomaticsub': True,
                'subtitleslangs': ['zh-Hans', 'zh', 'en'],
            })
        with _load_ytdlp().YoutubeDL(self._base_opts(**extra)) as ydl:
            self._ydl = ydl
            try:
                ydl.download([url])
            except _YTDLP_CANCELLED:
                self._log("已取消下载", "WARNING")
                return False
            except Exception as e:
                self._log(f"下载失败：{e}", "ERROR")
                return False
            finally:
                self._ydl = None
        # 合并/转码后钩子里记录的中间文件可能已被删除，以磁盘上最新的成品文件为准
        self._last_file = self._find_output_file(out_base) or self._last_file
        return True

    def stop(self):
        self.cancel_event.set()


def _human_bytes(n):
    """字节数转人类可读（yt-dlp 格式列表用）"""
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return ''
    if n <= 0:
        return ''
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            if unit == 'B':
                return f'{int(n)} {unit}'
            return f'{n:.1f} {unit}'
        n /= 1024
    return ''

# ===================== 数据类 =====================

@dataclass
class VideoResource:
    """视频资源统一数据类"""
    url: str
    resource_type: str  # 'm3u8', 'mp4', 'webm', 'flv', 'ts', 'dash', 'other'
    title: str = ""
    resolution: str = ""
    bandwidth: int = 0
    size: int = 0
    headers: Dict[str, str] = field(default_factory=dict)
    source: str = "network"  # 'network', 'dom', 'manual', 'playinfo'
    quality_score: int = 0
    is_ad: bool = False
    companion_url: str = ""       # DASH 音视频分离时的配对 URL
    init_url: str = ""            # fMP4/DASH 初始化分片 URL
    companion_init_url: str = ""  # 配对轨道的初始化分片 URL
    cookies: Optional[List] = None  # 浏览器 Cookie
    # 新增：多音轨/字幕/多码率支持
    language: str = ""           # 音轨语言标识
    codecs: str = ""             # 编码格式
    # 新增：master playlist 中的可选流列表
    alt_streams: List['M3U8Stream'] = field(default_factory=list)
    # 新增：DASH 多音轨列表（每项: {url, init_url, bandwidth, codec, language}）
    alt_audio_tracks: List[dict] = field(default_factory=list)
    # v4.0 新增：字幕轨道 URL 列表
    subtitle_urls: List[str] = field(default_factory=list)
    # v4.0 新增：DRM 标记
    is_drm: bool = False
    # v4.1 新增：列表展示用的补充说明（不影响下载文件名）
    note: str = ""
    # v4.3 新增：抖音等站点的 DASH 轨道角色（'video' / 'audio'），用于音视频配对
    media_role: str = ""
    # v4.2 新增：统一列表 / yt-dlp 下载支持
    engine: str = "http"          # http=内置下载器，ytdlp=yt-dlp 引擎
    format_spec: str = ""         # yt-dlp 的 format 选择串（如 137+bestaudio/best）
    ext: str = ""                 # 首选扩展名（yt-dlp 格式的容器）
    display_format: str = ""      # 表格「格式」列展示值（如 M4A / MP4 / AUTO）
    page_url: str = ""            # yt-dlp 资源对应的原始网页地址
    page_title: str = ""          # 网页/视频标题，用于另存为默认文件名
    key: str = ""                 # 资源唯一键（同一网页的多个 yt-dlp 格式需要区分）

    def __hash__(self):
        return hash(self.url)

    def __eq__(self, other):
        if isinstance(other, VideoResource):
            return self.url == other.url
        return False


@dataclass
class BatchItem:
    """批量下载条目（B站合集/多P/播放列表中的单个视频）"""
    index: int
    url: str
    title: str = ""
    duration: float = 0.0
    checked: bool = True
    status: str = "待下载"       # 待下载 / 下载中 / 已完成 / 失败 / 已存在 / 已停止
    progress: float = 0.0
    file_path: str = ""
    error: str = ""

@dataclass
class M3U8Segment:
    """M3U8 分片"""
    url: str
    duration: float = 0.0
    iv: Optional[bytes] = None
    key_uri: Optional[str] = None
    key_data: Optional[bytes] = None
    media_sequence: int = 0       # 媒体序列号（用于 AES IV 计算）
    init_url: Optional[str] = None  # fMP4 初始化分片 URL
    discontinuity: bool = False   # 流间断标记
    key_method: str = "NONE"      # 分片当时生效的加密方式（EXT-X-KEY 可中途切换）

@dataclass
class M3U8Stream:
    """M3U8 流（密钥信息独立持有，新增 EXT-X-MEDIA 多音轨/字幕支持）"""
    url: str
    bandwidth: int = 0
    resolution: str = ""
    is_master: bool = False
    key_method: str = "NONE"
    key_uri: Optional[str] = None
    iv: Optional[bytes] = None
    segments: List[M3U8Segment] = field(default_factory=list)
    init_url: Optional[str] = None       # 流级初始化分片
    media_sequence_start: int = 0       # 起始序列号
    # 新增：EXT-X-MEDIA 多音轨/字幕
    media_type: str = ""        # 'AUDIO', 'SUBTITLES', 'VIDEO'
    group_id: str = ""          # EXT-X-MEDIA GROUP-ID
    language: str = ""          # 语言标识
    name: str = ""              # 显示名称
    default: bool = False       # 是否默认轨道
    auto_select: bool = False   # AUTOSELECT 标记
    # 新增：字幕 URL（EXT-X-MEDIA URI 可指向 vtt/srt 字幕清单）
    subtitle_url: str = ""

# ===================== 智能视频嗅探引擎 =====================
class SmartSniffer:
    """智能视频嗅探引擎 - 全能嗅探 + 智能过滤"""

    VIDEO_EXTENSIONS = {
        '.mp4', '.m3u8', '.webm', '.flv', '.mkv', '.mov',
        '.avi', '.wmv', '.m4v', '.f4v', '.m4s', '.mpd', '.ts'
    }

    VIDEO_MIME_TYPES = {
        'video/mp4', 'video/webm', 'video/x-flv', 'video/mp2t',
        'application/x-mpegURL', 'application/vnd.apple.mpegurl',
        'video/MP2T', 'video/x-matroska', 'video/quicktime',
        'video/x-ms-wmv', 'video/x-msvideo', 'video/m4v'
    }

    # 广告关键词：仅用长词匹配域名+路径，短词如 'ad' 误杀率过高已移除
    AD_LONG_KEYWORDS = {
        'advertisement', 'banner', 'preload', 'preview',
        'tracking', 'analytics', 'pixel', 'beacon', 'impression',
        'preroll', 'midroll', 'postroll', 'overlay',
        'googlesyndication', 'doubleclick', 'adsystem', 'adserver',
        'google-analytics', 'googletagmanager',
        'scorecardresearch', 'moatads', 'outbrain', 'taboola',
        'popads', 'propellerads', 'exoclick', 'juicyads',
        'trafficjunky', 'eroadvertising', 'adsterra', 'adxxx',
        'popunder', 'sponsored',
    }

    # 页面自带的占位动画/特效预览，不是用户要下载的视频
    # 典型例子：抖音 PC 网页加载时的白兔动画 uuu_265.mp4（约 200KB/2 秒），
    # 以及抖音特效预览（byteeffecttos CDN）。它们会抢先出现在资源列表最上方，
    # 用户点下载就会得到一个“广告”短片，这里直接不纳入资源列表。
    PAGE_ASSET_HOSTS = (
        'byteeffecttos.com',
    )
    PAGE_ASSET_URL_KEYWORDS = (
        '/obj/douyin-pc-web/uuu_',
    )

    # 视频 CDN 白名单（跳过广告检测）
    VIDEO_CDN_WHITELIST = (
        'bilivideo.com', 'bilivideo.cn', 'bilivideo.org',
        'akamaized.net', 'cloudfront.net', 'fastly.net',
        'mcdn.video', 'vodcdn', 'video-cdn', 'streamcdn',
        'hlscdn', 'mp4cdn', 'movcdn', 'akamaized',
        'finder.video.qq.com',
    )

    RESOLUTION_SCORES = {
        '8k': 10, '4320': 10, '4k': 9, '2160': 9,
        '1440': 8, '2k': 8,
        '1080': 7, '1080p': 7, 'fhd': 7,
        '720': 6, '720p': 6, 'hd': 6,
        '480': 5, '480p': 5, 'sd': 5,
        '360': 4, '360p': 4,
        '240': 3, '240p': 3,
        '144': 2, '144p': 2
    }

    # 缓存破坏/时间戳参数：这些参数变化不代表资源内容不同
    _CACHE_BUSTING_PARAMS = frozenset({
        't', 'ts', 'timestamp', '_t', '_ts', '_', 'nonce', 'rand', 'random',
        'r', 'v', 'cb', 'cache', 'cachebust', 'bust', 'seq', 'jitter',
        # v4.1: 鉴权/签名参数每次请求都会变化，仅用于去重归一化（保留原始 URL 下载）
        'sign', 'signature', 'sig', 'token', 'expires', 'expire', 'auth',
        'auth_key', 'deadline', 'policy', 'hdntl', 'hdnts', 'key-pair-id',
    })

    def __init__(self):
        self.captured_resources: OrderedDict[str, VideoResource] = OrderedDict()
        self._norm_to_orig: Dict[str, str] = {}  # 归一化URL -> 原始URL（去重映射）
        self._ts_hosts: set = set()  # v4.1: 已保留过 TS 直链的主机（其余视为 HLS 分片）
        self._suppressed: set = set()  # v4.1: 已被合并/折叠的资源，后续回调不要再加回来
        self.ad_filter_enabled = True
        self.dedup_enabled = True
        self.lock = threading.Lock()  # 线程安全锁

    @classmethod
    def _normalize_url(cls, url: str) -> str:
        """归一化URL用于去重：移除缓存破坏参数和片段标识符"""
        try:
            parsed = urlparse(url)
            query = parsed.query
            if not query:
                return url.split('#')[0]
            params = parse_qsl(query)
            filtered = [(k, v) for k, v in params if k.lower() not in cls._CACHE_BUSTING_PARAMS]
            new_query = urlencode(filtered)
            return urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', new_query, ''))
        except Exception:
            return url.split('#')[0]

    @classmethod
    def _is_page_asset(cls, url: str) -> bool:
        """判断是否为网页自带的占位动画/特效预览（不是真正的视频内容）

        抖音网页在没有加载出正片时会先播放 uuu_265.mp4 这类占位动画；
        它同样是 video/mp4，若不排除，嗅探会把“广告”当成唯一视频。
        """
        try:
            parsed = urlparse(url)
        except Exception:
            return False
        host = (parsed.netloc or '').lower()
        path = unquote(parsed.path or '').lower()
        if host.endswith(cls.PAGE_ASSET_HOSTS):
            return True
        # 抖音静态资源站里的 PC 网页素材（uuu_xxx.mp4 等），正片在 douyinvod.com
        if host.endswith('douyinstatic.com') and (
                '/douyin-pc-web/' in path
                or path.rsplit('/', 1)[-1].startswith('uuu_')):
            return True
        if any(kw in (host + path) for kw in cls.PAGE_ASSET_URL_KEYWORDS):
            return True
        return False

    @classmethod
    def _douyin_track_role(cls, url: str) -> str:
        """识别抖音的音轨/视频轨（两者都是 video/mp4，靠 URL 路径区分）"""
        try:
            parsed = urlparse(url)
        except Exception:
            return ''
        host = (parsed.netloc or '').lower()
        if 'douyinvod.com' not in host and 'douyin' not in host:
            return ''
        path = unquote(parsed.path or '').lower()
        if 'media-audio' in path:
            return 'audio'
        if 'media-video' in path:
            return 'video'
        return ''

    def add_resource(self, resource: VideoResource) -> bool:
        """添加资源，自动去重和过滤（线程安全）"""
        if self._is_page_asset(resource.url):
            logger.info(f"忽略页面占位/动效资源: {resource.url[:80]}")
            return False
        role = self._douyin_track_role(resource.url)
        if role:
            resource.media_role = role
        resource.quality_score = self._calculate_quality_score(resource)
        norm = self._normalize_url(resource.url)

        with self.lock:
            # v4.1: 已被合并/折叠的资源，后续网络回调不要再加回来
            if norm in self._suppressed:
                return False

            # v4.1: HLS 分片折叠 —— 同一主机只保留一个 TS 直链
            # HLS 播放器会请求成百上千个 .ts 分片，逐个列出来会淹没资源列表
            ts_host = None
            if resource.resource_type == 'ts' and resource.source != 'manual':
                host = urlparse(resource.url).netloc.lower()
                if host in self._ts_hosts:
                    return False
                # 注意：等去重/广告过滤都通过后再登记，避免被拒绝的分片占住主机名额
                ts_host = host

            # 去重检查（基于归一化URL，避免缓存破坏参数导致的重复）
            if self.dedup_enabled and norm in self._norm_to_orig:
                orig_key = self._norm_to_orig[norm]
                existing = self.captured_resources.get(orig_key)
                if existing:
                    if resource.quality_score > existing.quality_score:
                        del self.captured_resources[orig_key]
                        self.captured_resources[resource.url] = resource
                        self._norm_to_orig[norm] = resource.url
                    elif resource.quality_score == existing.quality_score and orig_key != resource.url:
                        del self.captured_resources[orig_key]
                        self.captured_resources[resource.url] = resource
                        self._norm_to_orig[norm] = resource.url
                    return False

            # 广告过滤
            if self.ad_filter_enabled and self._is_ad_resource(resource):
                resource.is_ad = True
                logger.info(f"过滤广告资源: {resource.url[:80]}...")
                return False

            if ts_host:
                self._ts_hosts.add(ts_host)
            self._norm_to_orig[norm] = resource.url
            self.captured_resources[resource.url] = resource
            return True

    def force_update_resource(self, resource: VideoResource) -> bool:
        """强制更新或添加资源（总是覆盖已有资源，用于 playinfo 等高优先级数据源）"""
        if self._is_page_asset(resource.url):
            return False
        resource.quality_score = self._calculate_quality_score(resource)
        norm = self._normalize_url(resource.url)

        with self.lock:
            if self.ad_filter_enabled and self._is_ad_resource(resource):
                resource.is_ad = True
                return False
            if norm in self._norm_to_orig:
                old_key = self._norm_to_orig[norm]
                if old_key != resource.url and old_key in self.captured_resources:
                    del self.captured_resources[old_key]
            self._norm_to_orig[norm] = resource.url
            self.captured_resources[resource.url] = resource
            return True

    def _is_ad_resource(self, resource: VideoResource) -> bool:
        """智能判断是否是广告资源（多维度校验，降低误杀率）"""
        url = resource.url
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        path = unquote(parsed.path).lower()

        # 白名单域名直接跳过广告检测
        for cdn in self.VIDEO_CDN_WHITELIST:
            if cdn in domain:
                return False

        # v4.5: 抖音详情接口解析出的直链，标题就是作品文案，
        # 容易被 preview / sponsored 等关键词误杀，这里不做广告判定
        if getattr(resource, 'source', '') == 'douyin_api':
            return False

        # 域名黑名单直接拦截
        ad_domains = (
            'data.bilibili.com', 'metrics.bilibili.com',
            'trace.bilibili.com', 'hot.bilibili.com',
            'cm.bilibili.com', 'tp.amemori.com',
            'google-analytics.com', 'googletagmanager.com',
            'googlesyndication.com', 'doubleclick.net',
            'scorecardresearch.com', 'hm.baidu.com',
        )
        for ad_domain in ad_domains:
            if ad_domain in domain:
                return True

        # 长关键词匹配域名+路径（正则单词边界，避免 adapt 误判为含 ad）
        url_no_query = f"{domain}{path}"
        for kw in self.AD_LONG_KEYWORDS:
            pattern = r'(?<![a-zA-Z0-9])' + re.escape(kw) + r'(?![a-zA-Z0-9])'
            if re.search(pattern, url_no_query):
                return True

        # 检查标题
        if resource.title:
            title_lower = resource.title.lower()
            for kw in self.AD_LONG_KEYWORDS:
                pattern = r'(?<![a-zA-Z0-9])' + re.escape(kw) + r'(?![a-zA-Z0-9])'
                if re.search(pattern, title_lower):
                    return True

        # 文件大小辅助判断（广告通常很小）
        if resource.size > 0 and resource.size < 1024 * 100:
            if any(k in url_no_query for k in ('track', 'ping', 'log', 'stat')):
                return True

        return False

    def _calculate_quality_score(self, resource: VideoResource) -> int:
        """质量评分（分辨率 > 带宽 > 文件大小，格式仅作同分排序依据）"""
        score = 0

        # 基于带宽（最高可信度）
        if resource.bandwidth > 0:
            if resource.bandwidth > 8000000:
                score = max(score, 9)
            elif resource.bandwidth > 4000000:
                score = max(score, 7)
            elif resource.bandwidth > 2000000:
                score = max(score, 6)
            elif resource.bandwidth > 1000000:
                score = max(score, 5)
            elif resource.bandwidth > 500000:
                score = max(score, 4)

        # 基于分辨率字段
        if resource.resolution:
            res_lower = resource.resolution.lower()
            for res, val in self.RESOLUTION_SCORES.items():
                if res in res_lower:
                    score = max(score, val)
                    break

        # 基于 URL 中的分辨率关键词
        url_lower = resource.url.lower()
        for res, val in self.RESOLUTION_SCORES.items():
            if res in url_lower:
                score = max(score, val)
                break

        # 基于文件大小估算
        if resource.size > 0:
            size_mb = resource.size / (1024 * 1024)
            if size_mb > 500:
                score = max(score, 8)
            elif size_mb > 200:
                score = max(score, 6)
            elif size_mb > 50:
                score = max(score, 4)

        # 格式仅作同分次级排序（不影响主分数）
        # 不再给格式加分，而是在 get_sorted_resources 中作次级排序
        return score

    def get_sorted_resources(self) -> List[VideoResource]:
        """获取按质量排序的资源列表（线程安全，保留 M3U8/MP4/DASH/TS/WebM/FLV）"""
        with self.lock:
            resources = list(self.captured_resources.values())
        # 放行主流视频格式（TS 直链不再过滤）
        allowed_types = {'m3u8', 'mp4', 'dash', 'ts', 'webm', 'flv'}
        resources = [r for r in resources if r.resource_type in allowed_types]
        # 抖音音轨只用于混流，不单独展示（避免用户下到一条只有声音的文件）
        resources = [r for r in resources if getattr(r, 'media_role', '') != 'audio']
        # 主排序=质量分数降序，次级排序=格式优先级，三级=URL长度
        format_priority = {'mp4': 0, 'm3u8': 1, 'dash': 2, 'webm': 3, 'flv': 4, 'ts': 5}
        resources.sort(key=lambda r: (
            -r.quality_score,
            format_priority.get(r.resource_type, 9),
            len(r.url)
        ))
        return resources

    def detect_resource_type(self, url: str, mime_type: str = "", content: bytes = b"") -> str:
        """智能检测资源类型"""
        url_lower = url.lower()
        parsed = urlparse(url)
        path = unquote(parsed.path)
        ext = os.path.splitext(path)[1].lower()

        if ext == '.m3u8' or re.search(r'\.m3u8(?:[?#]|$)', url_lower):
            return 'm3u8'
        elif ext == '.mp4' or re.search(r'\.mp4(?:[?#]|$)', url_lower):
            return 'mp4'
        elif ext == '.webm' or re.search(r'\.webm(?:[?#]|$)', url_lower):
            return 'webm'
        elif ext == '.flv' or re.search(r'\.flv(?:[?#]|$)', url_lower):
            return 'flv'
        elif ext == '.ts' or (ext == '' and re.search(r'\.ts(?:[?#]|$)', url_lower)):
            return 'ts'
        elif ext in ('.m4s', '.mpd') or re.search(r'\.(m4s|mpd)(?:[?#]|$)', url_lower) or '/dash/' in url_lower:
            return 'dash'

        if mime_type:
            mime_lower = mime_type.lower()
            if 'mpegurl' in mime_lower or 'm3u8' in mime_lower:
                return 'm3u8'
            elif 'mp4' in mime_lower:
                return 'mp4'
            elif 'webm' in mime_lower:
                return 'webm'
            elif 'flv' in mime_lower:
                return 'flv'
            elif 'mp2t' in mime_lower:
                return 'ts'

        if content:
            if content.startswith(b'#EXTM3U'):
                return 'm3u8'
            elif content[:4] == b'\x66\x74\x79\x70':  # ftyp
                return 'mp4'
            elif content[:4] == b'\x1A\x45\xDF\xA3':  # EBML (WebM/MKV)
                return 'webm'
            elif content[:3] == b'FLV':
                return 'flv'
            elif content[:1] == b'G':  # MPEG-TS sync byte
                return 'ts'

        return 'other'

    def extract_resolution(self, url: str, text: str = "") -> str:
        """从URL或文本中提取分辨率"""
        combined = f"{url} {text}".lower()
        patterns = [
            r'(\d{3,4})[pP]',
            r'(\d{3,4})x(\d{3,4})',
            r'(\d{3,4})[_-](\d{3,4})',
            r'res[_-]?(\d{3,4})',
            r'[_/-](\d{3,4})[_/-]',
        ]
        for pattern in patterns:
            matches = re.findall(pattern, combined)
            for match in matches:
                if isinstance(match, tuple):
                    w, h = int(match[0]), int(match[1])
                    # 严格校验：宽高都必须在合理范围，且宽>高（横屏）或高>宽（竖屏）
                    if 240 <= w <= 8000 and 240 <= h <= 8000 and w != h:
                        ratio = max(w,h) / min(w,h)
                        if 1.2 < ratio < 3.0:
                            return f"{w}x{h}"
                else:
                    h = int(match)
                    if h in (144,240,360,480,540,720,1080,1440,2160,4320):
                        return f"{h}p"

        if '4k' in combined or '2160' in combined:
            return '4K'
        elif '8k' in combined or '4320' in combined:
            return '8K'
        return ""

    def clear(self):
        """清空所有捕获的资源（线程安全）"""
        with self.lock:
            self.captured_resources.clear()
            self._norm_to_orig.clear()
            self._ts_hosts.clear()
            self._suppressed.clear()

    def _rebuild_norm_index(self):
        """重建归一化URL索引（删除资源后必须调用，否则去重表会指向已删除项）"""
        self._norm_to_orig = {
            self._normalize_url(url): url for url in self.captured_resources
        }

    def remove_urls(self, urls, suppress: bool = False) -> int:
        """按 URL 批量删除资源（线程安全），返回实际删除数量

        suppress=True 时同时记入抑制表，避免后续网络回调把已合并的清单/分片又加回列表。
        """
        removed = 0
        with self.lock:
            for url in urls:
                if suppress:
                    self._suppressed.add(self._normalize_url(url))
                if self.captured_resources.pop(url, None) is not None:
                    removed += 1
            if removed:
                self._rebuild_norm_index()
        return removed

# ===================== M3U8 解析器 =====================
class M3U8Parser:
    """M3U8 文件解析器（支持 MEDIA-SEQUENCE / EXT-X-MAP / DISCONTINUITY，密钥按流独立）"""

    def __init__(self, base_url: str = ""):
        self.base_url = base_url

    def parse(self, content: str, base_url: str = "") -> List[M3U8Stream]:
        """解析 M3U8 内容"""
        if base_url:
            self.base_url = base_url

        streams = []
        lines = content.strip().split('\n')
        current_stream = M3U8Stream(url=self.base_url)

        # 媒体序列号
        media_sequence = 0
        # 流级初始化分片
        stream_init_url = None
        # 密钥信息按流独立维护（不再用解析器全局变量）
        stream_key_method = "NONE"
        stream_key_uri = None
        stream_iv = None
        # 流间断标记
        pending_discontinuity = False
        # 修复：标记是否遇到过 STREAM-INF（区分 master / media playlist）
        has_master = False

        i = 0

        def _uri_after(idx: int, max_skip: int = 8):
            """返回 (uri, 行号)：跳过空行/注释/分片附加标签，遇到结构标签视为缺少 URI

            说明：EXTINF / STREAM-INF 的 URI 在下一行，中间可能夹空行、注释或
            #EXT-X-BYTERANGE；旧实现直接把“下一行”当 URI，空行时会得到清单自身 URL。
            """
            j = idx + 1
            skipped = 0
            while j < len(lines) and skipped <= max_skip:
                nxt = lines[j].strip()
                if not nxt:
                    j += 1
                    skipped += 1
                    continue
                if nxt.startswith('#EXT'):
                    if nxt.startswith(('#EXT-X-BYTERANGE', '#EXT-X-DISCONTINUITY-SEQUENCE')):
                        j += 1
                        skipped += 1
                        continue
                    return None, idx
                if nxt.startswith('#'):
                    j += 1
                    skipped += 1
                    continue
                return nxt, j
            return None, idx

        while i < len(lines):
            line = lines[i].strip()

            if not line or line.startswith('#EXT'):
                if line.startswith('#EXT-X-MEDIA-SEQUENCE'):
                    # 解析起始序列号
                    seq_match = re.search(r'#EXT-X-MEDIA-SEQUENCE:(\d+)', line)
                    if seq_match:
                        media_sequence = int(seq_match.group(1))
                        current_stream.media_sequence_start = media_sequence

                elif line.startswith('#EXT-X-MAP'):
                    # 解析初始化分片
                    uri_match = re.search(r'URI="([^"]+)"', line)
                    if uri_match:
                        stream_init_url = urljoin(self.base_url, uri_match.group(1))
                        current_stream.init_url = stream_init_url

                elif line.startswith('#EXT-X-KEY'):
                    # 密钥信息存入流实例
                    key_info = self._parse_key(line)
                    stream_key_method = key_info.get('method', 'NONE')
                    stream_key_uri = key_info.get('uri')
                    stream_iv = key_info.get('iv')
                    current_stream.key_method = stream_key_method
                    current_stream.key_uri = stream_key_uri
                    current_stream.iv = stream_iv

                elif line.startswith('#EXT-X-DISCONTINUITY'):
                    # 标记流间断，下一个分片会被标记
                    pending_discontinuity = True

                elif line.startswith('#EXT-X-STREAM-INF'):
                    has_master = True
                    stream_info = self._parse_stream_inf(line)
                    stream_uri, uri_idx = _uri_after(i)
                    if not stream_uri:
                        i += 1
                        continue
                    current_stream = M3U8Stream(
                        url=urljoin(self.base_url, stream_uri),
                        bandwidth=stream_info.get('bandwidth', 0),
                        resolution=stream_info.get('resolution', ''),
                        is_master=True
                    )
                    streams.append(current_stream)
                    i = uri_idx

                elif line.startswith('#EXT-X-MEDIA'):
                    # 新增：解析 EXT-X-MEDIA 多音轨/字幕
                    media_info = self._parse_media(line)
                    if media_info.get('uri'):
                        media_url = urljoin(self.base_url, media_info['uri'])
                        media_stream = M3U8Stream(
                            url=media_url,
                            is_master=False,
                            media_type=media_info.get('type', ''),
                            group_id=media_info.get('group_id', ''),
                            language=media_info.get('language', ''),
                            name=media_info.get('name', ''),
                            default=media_info.get('default', False),
                            auto_select=media_info.get('autoselect', False),
                        )
                        if media_info.get('type') == 'SUBTITLES':
                            media_stream.subtitle_url = media_url
                        streams.append(media_stream)

                elif line.startswith('#EXTINF'):
                    segment_info = self._parse_extinf(line)
                    segment_uri, uri_idx = _uri_after(i)
                    if segment_uri:
                        segment_url = urljoin(self.base_url, segment_uri)
                        segment = M3U8Segment(
                            url=segment_url,
                            duration=segment_info['duration'],
                            iv=stream_iv,
                            key_uri=stream_key_uri,
                            media_sequence=media_sequence,
                            init_url=stream_init_url,
                            discontinuity=pending_discontinuity,
                            # 按分片创建时生效的 METHOD 记录，避免被后面的
                            # #EXT-X-KEY:METHOD=NONE 覆盖而漏解密前面的分片
                            key_method=stream_key_method,
                        )
                        current_stream.segments.append(segment)
                        media_sequence += 1  # 每个分片序列号递增
                        pending_discontinuity = False  # 重置间断标记
                        i = uri_idx

            i += 1

        if not has_master:
            # 修复：媒体清单（非 master）中，主分片流必须保留
            # 即使存在 EXT-X-MEDIA（字幕/音轨）条目，也不能丢失 current_stream
            if current_stream not in streams:
                streams.insert(0, current_stream)
        # master 清单若没有任何可用变体则返回空列表，由上层报错；
        # 不能把清单自身当成视频流（会导致反复下载同一个 m3u8）

        return streams

    def _parse_stream_inf(self, line: str) -> Dict:
        info = {}
        bandwidth_match = re.search(r'BANDWIDTH=(\d+)', line)
        if bandwidth_match:
            info['bandwidth'] = int(bandwidth_match.group(1))
        resolution_match = re.search(r'RESOLUTION=(\d+x\d+)', line)
        if resolution_match:
            info['resolution'] = resolution_match.group(1)
        return info

    def _parse_extinf(self, line: str) -> Dict:
        info = {'duration': 0.0, 'title': ''}
        match = re.search(r'#EXTINF:([\d.]+)(?:,(.*))?', line)
        if match:
            info['duration'] = float(match.group(1))
            if match.group(2):
                info['title'] = match.group(2).strip()
        return info

    def _parse_key(self, line: str) -> Dict:
        info = {'method': 'NONE', 'uri': None, 'iv': None, 'keyformat': None}
        method_match = re.search(r'METHOD=([A-Z0-9-]+)', line)
        if method_match:
            info['method'] = method_match.group(1)
        uri_match = re.search(r'URI="([^"]+)"', line)
        if uri_match:
            info['uri'] = urljoin(self.base_url, uri_match.group(1))
        iv_match = re.search(r'IV=0x([0-9a-fA-F]+)', line)
        if iv_match:
            info['iv'] = bytes.fromhex(iv_match.group(1))
        kf_match = re.search(r'KEYFORMAT="([^"]+)"', line)
        if kf_match:
            info['keyformat'] = kf_match.group(1)
        return info

    def _parse_media(self, line: str) -> Dict:
        """解析 EXT-X-MEDIA 标签（多音轨/字幕）"""
        info = {'type': '', 'group_id': '', 'language': '', 'name': '',
                'default': False, 'autoselect': False, 'uri': ''}
        type_match = re.search(r'TYPE=([A-Z]+)', line)
        if type_match:
            info['type'] = type_match.group(1)
        group_match = re.search(r'GROUP-ID="([^"]+)"', line)
        if group_match:
            info['group_id'] = group_match.group(1)
        lang_match = re.search(r'LANGUAGE="([^"]+)"', line)
        if lang_match:
            info['language'] = lang_match.group(1)
        name_match = re.search(r'NAME="([^"]+)"', line)
        if name_match:
            info['name'] = name_match.group(1)
        if re.search(r'DEFAULT=YES', line, re.IGNORECASE):
            info['default'] = True
        if re.search(r'AUTOSELECT=YES', line, re.IGNORECASE):
            info['autoselect'] = True
        uri_match = re.search(r'URI="([^"]+)"', line)
        if uri_match:
            info['uri'] = uri_match.group(1)
        return info


# ===================== DASH MPD 解析器 =====================
class MPDParser:
    """通用 DASH MPD 清单解析器（支持 SegmentTemplate/SegmentList/BaseURL）"""

    def __init__(self, base_url: str = ""):
        self.base_url = base_url

    def parse(self, content: str, base_url: str = "") -> List[VideoResource]:
        """解析 MPD 内容，返回 VideoResource 列表（最佳视频+音频配对）"""
        if base_url:
            self.base_url = base_url
        resources = []
        try:
            root = ET.fromstring(content)
        except ET.ParseError as e:
            logger.warning(f"MPD 解析失败: {e}")
            return resources

        ns = ''
        m = re.match(r'\{(.+?)\}', root.tag)
        if m:
            ns = f'{{{m.group(1)}}}'

        # 收集 BaseURL
        doc_base = self.base_url
        base_elem = root.find(f'{ns}BaseURL')
        if base_elem is not None and base_elem.text:
            doc_base = urljoin(doc_base, base_elem.text.strip())

        # 遍历 Period > AdaptationSet
        for period in root.findall(f'.//{ns}Period'):
            video_tracks = []
            audio_tracks = []
            for adapt in period.findall(f'{ns}AdaptationSet'):
                content_type = (adapt.get('contentType','') or '').lower()
                mime_type = (adapt.get('mimeType','') or '').lower()
                mime_type = mime_type or (adapt.get('mimetype','') or '').lower()
                is_video = 'video' in content_type or 'video' in mime_type
                is_audio = 'audio' in content_type or 'audio' in mime_type
                # 子 Representation
                for rep in adapt.findall(f'{ns}Representation'):
                    rep_mime = (rep.get('mimeType','') or mime_type).lower()
                    if not is_video and not is_audio:
                        is_video = 'video' in rep_mime
                        is_audio = 'audio' in rep_mime
                    bandwidth = int(rep.get('bandwidth', '0') or '0')
                    width = rep.get('width', '')
                    height = rep.get('height', '')
                    codecs = rep.get('codecs', '')
                    resolution = f"{width}x{height}" if width and height else ""
                    # 构造媒体 URL
                    media_url = self._build_media_url(rep, adapt, doc_base, ns)
                    if not media_url:
                        continue
                    track = {
                        'url': media_url,
                        'bandwidth': bandwidth,
                        'resolution': resolution,
                        'codecs': codecs,
                        'init_url': self._build_init_url(rep, adapt, doc_base, ns),
                    }
                    if is_video:
                        video_tracks.append(track)
                    elif is_audio:
                        audio_tracks.append(track)

            # 选最佳视频和音频
            best_video = max(video_tracks, key=lambda t: t['bandwidth']) if video_tracks else None
            best_audio = max(audio_tracks, key=lambda t: t['bandwidth']) if audio_tracks else None

            if best_video:
                companion = best_audio['url'] if best_audio else ''
                res = VideoResource(
                    url=best_video['url'],
                    resource_type='dash',
                    title=f"DASH {best_video['resolution']} {best_video['codecs']}".strip(),
                    resolution=best_video['resolution'],
                    bandwidth=best_video['bandwidth'],
                    codecs=best_video['codecs'],
                    source='network',
                    companion_url=companion,
                    init_url=best_video.get('init_url', ''),
                    companion_init_url=best_audio.get('init_url', '') if best_audio else '',
                )
                resources.append(res)
        return resources

    def _build_media_url(self, rep, adapt, doc_base: str, ns: str) -> str:
        """从 Representation/AdaptationSet 构造媒体 URL"""
        # 1. Representation 内的 BaseURL
        for elem in (rep, adapt):
            bu = elem.find(f'{ns}BaseURL')
            if bu is not None and bu.text:
                return urljoin(doc_base, bu.text.strip())
        # 2. SegmentTemplate media 属性
        for elem in (rep, adapt):
            st = elem.find(f'{ns}SegmentTemplate')
            if st is not None:
                media_tmpl = st.get('media', '')
                if media_tmpl:
                    # 简化：取第一个分片（$Number$ 从 startNumber 开始）
                    start = int(st.get('startNumber', '1'))
                    # 检查是否有 SegmentTimeline
                    timeline = st.find(f'{ns}SegmentTimeline')
                    if timeline is not None:
                        first_s = timeline.find(f'{ns}S')
                        if first_s is not None:
                            t = first_s.get('t', str(start))
                            media_url = media_tmpl.replace('$Number$', str(start)).replace('$Time$', t)
                            return urljoin(doc_base, media_url)
                    media_url = media_tmpl.replace('$Number$', str(start)).replace('$Time$', '0')
                    # 替换 RepresentationID/bandwidth 变量
                    media_url = media_url.replace('$RepresentationID$', rep.get('id', ''))
                    media_url = media_url.replace('$Bandwidth$', rep.get('bandwidth', ''))
                    return urljoin(doc_base, media_url)
        # 3. SegmentList
        for elem in (rep, adapt):
            sl = elem.find(f'{ns}SegmentList')
            if sl is not None:
                for surl in sl.findall(f'{ns}SegmentURL'):
                    media = surl.get('media') or surl.get('mediaRange')
                    if media and media.startswith('http'):
                        return media
                    elif media:
                        return urljoin(doc_base, media)
        return ''

    def _build_init_url(self, rep, adapt, doc_base: str, ns: str) -> str:
        """提取初始化分片 URL"""
        for elem in (rep, adapt):
            st = elem.find(f'{ns}SegmentTemplate')
            if st is not None:
                init = st.get('initialization', '')
                if init:
                    init = init.replace('$RepresentationID$', rep.get('id', ''))
                    init = init.replace('$Bandwidth$', rep.get('bandwidth', ''))
                    return urljoin(doc_base, init)
            sl = elem.find(f'{ns}SegmentList')
            if sl is not None:
                init_url = sl.find(f'{ns}Initialization')
                if init_url is not None:
                    src = init_url.get('sourceURL', '')
                    if src:
                        return urljoin(doc_base, src)
        return ''


# ===================== 通用视频下载器 =====================
# FFmpeg 允许的协议白名单。httpproxy 必须包含：系统代理开启时 FFmpeg 会把
# 输入改走代理协议，缺失会被判定 "Protocol 'httpproxy' not on whitelist"，
# 表现为所有直链下载报 Invalid argument。
FFMPEG_PROTOCOL_WHITELIST = 'file,http,https,tcp,tls,crypto,httpproxy'


class UniversalDownloader:
    """全能视频下载器 - 支持MP4/M3U8/WebM/FLV/TS/DASH等"""

    def __init__(self, ffmpeg_path: str = "ffmpeg"):
        self.ffmpeg_path = ffmpeg_path
        self.temp_dir = None
        self.stop_flag = False
        self.parser = M3U8Parser()
        self.session = requests.Session()
        _adapter = requests.adapters.HTTPAdapter(
            pool_connections=32, pool_maxsize=32, max_retries=0)
        self.session.mount('http://', _adapter)
        self.session.mount('https://', _adapter)
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Connection': 'keep-alive',
        })
        self.log_callback = None
        self.progress_callback = None
        self.retry_times = 3
        self.min_delay = 0.0
        self.max_delay = 0.02
        self.max_workers = 16
        # HLS 分片并发：默认 16，遇到 TLS/连接被重置时自动降到 4 重试
        self.segment_workers = 16
        self.max_segment_failures = 20
        self._segment_abort = False
        self._last_error = ""
        self._blocked_log_count = 0
        self._insecure_warned = False  # TLS 免校验降级只告警一次，避免分片日志刷屏
        self.skip_ad_segments = 0
        self._executor = None  # 线程池引用
        self._ffmpeg_procs: list = []  # 跟踪活跃的 FFmpeg 子进程
        # 新增：密钥本地缓存（相同 key-uri 不重复请求）
        self._key_cache: Dict[str, bytes] = {}
        # 新增：下载限速（字节/秒，0=不限速）
        self.speed_limit = 0
        self._rate_lock = threading.Lock()
        self._rate_next = 0.0
        # 新增：aria2 下载后端
        self.aria2_path = ""
        self.use_aria2 = False
        # 新增：断点续传支持
        self._resume_map: Dict[str, Path] = {}  # segment_url -> downloaded path
        self._resume_file: Optional[Path] = None
        # v4.0 新增：音频提取 / 直播录制 / 下载模式
        self.audio_only = False          # 仅提取音频
        self.live_mode = False           # 直播录制模式
        self.download_mode = "auto"      # auto / ffmpeg / local
        self.recording_start_time = 0.0  # 录制计时

    def _init_resume(self, m3u8_url: str, output_path: str):
        """初始化断点续传：加载已有的续传状态文件

        续传文件只由 m3u8_url 的 md5 决定，不绑定输出文件名，改保存名后仍可续传。
        """
        self._resume_map = {}
        resume_dir = Path(output_path).parent
        url_tag = hashlib.md5(m3u8_url.encode('utf-8')).hexdigest()[:8]
        resume_name = f".m3u8_{url_tag}.resume"
        self._resume_file = resume_dir / resume_name
        if self._resume_file.exists():
            try:
                with open(self._resume_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                for seg_url, seg_path in data.items():
                    p = Path(seg_path)
                    if p.exists():
                        self._resume_map[seg_url] = p
                if self._resume_map:
                    self.log(f"断点续传：已加载 {len(self._resume_map)} 个已完成分片", "INFO")
            except Exception:
                self._resume_map = {}

    def _save_resume_state(self):
        """保存续传状态到文件（快照 + 原子替换，避免并发写坏 JSON）"""
        if not self._resume_file or not self._resume_map:
            return
        tmp_path = self._resume_file.with_suffix('.tmp')
        try:
            # list() 先做快照：分片线程可能在遍历期间继续写入 _resume_map
            data = {url: str(p) for url, p in list(self._resume_map.items())}
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f)
            os.replace(tmp_path, self._resume_file)
        except Exception:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass

    def _cleanup_resume(self):
        """下载成功后清理续传文件"""
        self._resume_map = {}
        if self._resume_file and self._resume_file.exists():
            try:
                self._resume_file.unlink()
            except Exception:
                pass
        self._resume_file = None

    @staticmethod
    def check_ffmpeg(ffmpeg_path: str = "ffmpeg") -> bool:
        """检测 FFmpeg 是否可用（兼容系统安装和静态单文件）"""
        if not ffmpeg_path or not ffmpeg_path.strip():
            return False
        try:
            result = subprocess.run(
                [str(Path(ffmpeg_path)), '-version'],
                capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=10
            )
            return result.returncode == 0
        except FileNotFoundError:
            return False
        except Exception:
            return False

    @staticmethod
    def get_ffmpeg_version(ffmpeg_path: str = "ffmpeg") -> str:
        """获取 FFmpeg 版本号，返回版本字符串或空串"""
        if not ffmpeg_path or not ffmpeg_path.strip():
            return ""
        try:
            result = subprocess.run(
                [str(Path(ffmpeg_path)), '-version'],
                capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=10
            )
            if result.returncode == 0 and result.stdout:
                # 提取版本号，如 "ffmpeg version 6.0 Copyright"
                match = re.search(r'ffmpeg version\s+([\d.]+)', result.stdout)
                if match:
                    return match.group(1)
                return result.stdout.split('\n')[0]
            return ""
        except Exception:
            return ""

    @staticmethod
    def check_aria2(aria2_path: str = "aria2c") -> bool:
        """检测 aria2 是否可用"""
        try:
            result = subprocess.run(
                [aria2_path, '--version'],
                capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=10
            )
            return result.returncode == 0
        except Exception:
            return False

    def _aria2_download_segment(self, url: str, output_path: str,
                                headers: Dict[str, str], aria2_path: str = "aria2c") -> bool:
        """使用 aria2 下载单个分片（多连接加速）"""
        cmd = [aria2_path, '-x', '4', '-s', '4', '--allow-overwrite=true',
               '--console-log-level=error', '--summary-interval=0',
               '-o', str(Path(output_path).name),
               '-d', str(Path(output_path).parent)]
        if headers:
            for k, v in headers.items():
                cmd.extend(['--header', f'{k}: {v}'])
        cmd.append(url)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                   encoding='utf-8', errors='replace', timeout=120)
            return result.returncode == 0 and Path(output_path).exists()
        except Exception:
            return False

    def _apply_speed_limit(self, downloaded: int, start_time: float) -> None:
        """下载限速控制"""
        if self.speed_limit <= 0:
            return
        elapsed = time.time() - start_time
        if elapsed <= 0:
            return
        expected_time = downloaded / self.speed_limit
        if expected_time > elapsed:
            time.sleep(expected_time - elapsed)

    def _throttle(self, nbytes: int) -> None:
        """全局令牌桶限速：按累计字节数平滑休眠（分片下载路径调用）"""
        if self.speed_limit <= 0 or nbytes <= 0:
            return
        with self._rate_lock:
            now = time.time()
            if self._rate_next < now:
                self._rate_next = now
            self._rate_next += nbytes / float(self.speed_limit)
            delay = self._rate_next - now
        if delay > 0:
            time.sleep(min(delay, 2.0))

    def set_cookies(self, cookies: List) -> None:
        """批量同步浏览器 Cookie 到 session"""
        if not cookies:
            return
        for c in cookies:
            try:
                self.session.cookies.set(
                    c.get('name', ''),
                    c.get('value', ''),
                    domain=c.get('domain', ''),
                    path=c.get('path', '/'),
                )
            except Exception:
                pass

    def download(self, resource: VideoResource, output_path: str,
                 headers: Optional[Dict] = None) -> bool:
        """统一调度：根据资源类型和下载模式选择下载分支"""
        try:
            # v4.2: 不再在下载开始时清空 stop_flag；否则用户在任务刚启动时点停止会被覆盖
            # 同步浏览器 Cookie
            if resource.cookies:
                self.set_cookies(resource.cookies)

            # 所有下载分支均依赖 FFmpeg
            if not self.check_ffmpeg(self.ffmpeg_path):
                self.log("FFmpeg 不可用，请在设置中配置正确的 FFmpeg 路径", "ERROR")
                return False

            request_headers = self._prepare_headers(resource, headers)
            if request_headers:
                self.session.headers.update(request_headers)

            self.temp_dir = Path(tempfile.mkdtemp(prefix="video_dl_"))

            # 直播录制模式
            if self.live_mode and resource.resource_type in ('m3u8', 'ts', 'flv'):
                return self._download_live(resource, output_path, request_headers)

            # 统一调度分支
            if resource.resource_type == 'dash':
                return self._download_dash_ffmpeg(resource, output_path, request_headers)
            elif resource.resource_type == 'm3u8':
                # M3U8：auto/local 都优先走本地多线程分片（速度比 FFmpeg 单连接快很多）
                mode = (self.download_mode or 'auto').lower()
                if mode == 'ffmpeg':
                    self.log("M3U8 下载方式：FFmpeg 远程（单连接，速度取决于 CDN）", "INFO")
                    return self._download_m3u8_ffmpeg(resource, output_path, request_headers)

                self.log(f"M3U8 下载方式：本地分片优先（{min(self.max_workers, self.segment_workers)} 线程）", "INFO")
                ok = self._download_m3u8_local(resource, output_path, request_headers)
                if not ok and not self.stop_flag:
                    self.log("本地分片下载失败，回退 FFmpeg 远程下载", "WARNING")
                    ok = self._download_m3u8_ffmpeg(resource, output_path, request_headers)
                return ok
            elif resource.resource_type == 'ts':
                return self._download_ts_ffmpeg(resource, output_path, request_headers)
            elif resource.resource_type in ('mp4', 'webm', 'flv'):
                return self._download_mp4_ffmpeg(resource, output_path, request_headers)
            else:
                self.log(f"不支持的资源类型：{resource.resource_type}", "ERROR")
                return False

        except Exception as e:
            self.log(f"下载过程出错：{str(e)}", "ERROR")
            self.log(traceback.format_exc(), "ERROR")
            return False
        finally:
            self._cleanup_temp()

    def _build_ffmpeg_headers(self, resource: VideoResource,
                             headers: Dict[str, str]) -> str:
        """构建 FFmpeg -headers 参数字符串（携带 UA/Referer/Origin/Cookie）"""
        lines = []
        ua = headers.get('User-Agent', '') or (resource.headers or {}).get('User-Agent', '')
        if ua:
            lines.append(f"User-Agent: {ua}")
        referer = headers.get('Referer', '') or (resource.headers or {}).get('Referer', '')
        if referer:
            lines.append(f"Referer: {referer}")
        origin = headers.get('Origin', '') or (resource.headers or {}).get('Origin', '')
        if origin:
            lines.append(f"Origin: {origin}")
        if resource.cookies:
            cookie_str = '; '.join(
                f"{c.get('name', '')}={c.get('value', '')}" for c in resource.cookies
                if c.get('name') and c.get('value')
            )
            if cookie_str:
                lines.append(f"Cookie: {cookie_str}")
        return ("\r\n".join(lines) + "\r\n") if lines else ""

    def _prepare_headers(self, resource: VideoResource,
                        override_headers: Optional[Dict] = None) -> Dict[str, str]:
        headers = {}
        if resource.headers:
            headers.update(resource.headers)
        parsed = urlparse(resource.url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if 'Referer' not in headers and 'referer' not in headers:
            headers['Referer'] = origin
        if 'Origin' not in headers and 'origin' not in headers:
            headers['Origin'] = origin
        if override_headers:
            headers.update(override_headers)
        return headers

    def _download_m3u8_ffmpeg(self, resource: VideoResource, output_path: str,
                              headers: Dict[str, str]) -> bool:
        """FFmpeg 远程处理 M3U8 下载（不下载本地分片，AES key 交由 FFmpeg 自动处理）

        FFmpeg 携带 UA、Referer、Cookie、重连参数、协议白名单，直接读取 m3u8 网络链接。
        返回码≠0 代表下载失败，不输出假成功文件。
        """
        m3u8_url = resource.url
        header_str = self._build_ffmpeg_headers(resource, headers)
        ua = headers.get('User-Agent', '')
        referer = headers.get('Referer', '')
        started = time.time()

        self.log("正在下载 M3U8 视频流...", "INFO")

        cmd = [
            self.ffmpeg_path, '-y',
            '-headers', header_str,
            '-user_agent', ua,
            '-referer', referer,
            '-reconnect', '1',
            '-reconnect_streamed', '1',
            '-reconnect_delay_max', '5',
            '-rw_timeout', '300000',
            '-protocol_whitelist', FFMPEG_PROTOCOL_WHITELIST,
            '-i', m3u8_url,
            '-c', 'copy',
            '-movflags', '+faststart',
            output_path
        ]

        rc = self._run_ffmpeg_with_progress(cmd, 0, "M3U8下载")
        if rc == 0 and not self.stop_flag:
            self.log(f"视频保存成功：{_short_file(output_path)}（{time.time() - started:.1f} 秒）", "INFO")
            return True
        else:
            if self.stop_flag:
                self.log("下载已停止", "WARNING")
            else:
                signed_rc = rc - (1 << 32) if rc >= (1 << 31) else rc
                self.log(
                    f"FFmpeg 下载失败（返回码={rc} / {signed_rc}），输出文件可能损坏",
                    "ERROR")
            try:
                if os.path.exists(output_path):
                    os.remove(output_path)
            except Exception:
                pass
            return False

    def _download_mp4_ffmpeg(self, resource: VideoResource, output_path: str,
                             headers: Dict[str, str]) -> bool:
        """直链下载：FFmpeg 远程拉取，根据格式自动适配容器和编码"""
        url = resource.url
        header_str = self._build_ffmpeg_headers(resource, headers)
        ua = headers.get('User-Agent', '')
        referer = headers.get('Referer', '')
        res_type = resource.resource_type

        # 只报 CDN 主机名，完整直链写进日志文件就够了（抖音直链一屏都放不下）
        try:
            host = urlparse(url).netloc
        except Exception:
            host = ''
        self.log(f"FFmpeg 远程直链下载 {res_type.upper()}" + (f"（{host}）" if host else ""), "INFO")

        # 根据资源类型调整输出参数
        is_webm = res_type == 'webm'
        out_ext = os.path.splitext(output_path)[1].lower()

        base_cmd = [
            self.ffmpeg_path, '-y',
            '-headers', header_str,
            '-user_agent', ua,
            '-referer', referer,
            '-reconnect', '1',
            '-reconnect_streamed', '1',
            '-reconnect_delay_max', '5',
            '-rw_timeout', '300000',
            '-protocol_whitelist', FFMPEG_PROTOCOL_WHITELIST,
            '-i', url,
        ]

        # WebM 输出不加 movflags；MP4/MKV 加 faststart
        if is_webm or out_ext in ('.webm', '.mkv'):
            copy_cmd = base_cmd + ['-c', 'copy', output_path]
        else:
            copy_cmd = base_cmd + ['-c', 'copy', '-movflags', '+faststart', output_path]

        rc = self._run_ffmpeg_with_progress(copy_cmd, 0, f"{res_type.upper()}下载")
        if rc == 0 and not self.stop_flag:
            self.log(f"视频保存成功：{_short_file(output_path)}", "INFO")
            return True

        # copy 失败：尝试重编码（仅非停止状态）
        if not self.stop_flag:
            self.log(f"直接拷贝失败（返回码={rc}），尝试重编码...", "WARNING")
            if is_webm or out_ext in ('.webm', '.mkv'):
                recode_cmd = base_cmd + ['-c:v', 'libvpx-vp9' if out_ext == '.webm' else 'libx264',
                                         '-c:a', 'libopus' if out_ext == '.webm' else 'aac',
                                         output_path]
            else:
                recode_cmd = base_cmd + ['-c:v', 'libx264', '-c:a', 'aac',
                                         '-movflags', '+faststart', output_path]
            rc = self._run_ffmpeg_with_progress(recode_cmd, 0, "重编码")
            if rc == 0 and not self.stop_flag:
                self.log(f"视频保存成功（重编码）：{_short_file(output_path)}", "INFO")
                return True

        if self.stop_flag:
            self.log("下载已停止", "WARNING")
        else:
            self.log(f"FFmpeg 下载失败（返回码={rc}）", "ERROR")
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
        except Exception:
            pass
        return False

    def _download_dash_ffmpeg(self, resource: VideoResource, output_path: str,
                               headers: Dict[str, str]) -> bool:
        """B站 DASH 音视频混流：FFmpeg 远程读取 m4s 链接（禁止本地下载分片）"""
        video_url = resource.url
        audio_url = resource.companion_url
        header_str = self._build_ffmpeg_headers(resource, headers)
        ua = headers.get('User-Agent', '')
        referer = headers.get('Referer', '')

        self.log("正在混流 DASH 音视频...", "INFO")

        input_opts = [
            '-headers', header_str,
            '-user_agent', ua,
            '-referer', referer,
            '-reconnect', '1',
            '-reconnect_streamed', '1',
            '-reconnect_delay_max', '5',
            '-rw_timeout', '300000',
            '-protocol_whitelist', FFMPEG_PROTOCOL_WHITELIST,
        ]

        if audio_url:
            cmd = [
                self.ffmpeg_path, '-y',
                *input_opts, '-i', video_url,
                *input_opts, '-i', audio_url,
                '-c', 'copy',
                '-map', '0:v:0', '-map', '1:a:0',
                '-movflags', '+faststart',
                output_path
            ]
        else:
            cmd = [
                self.ffmpeg_path, '-y',
                *input_opts, '-i', video_url,
                '-c', 'copy',
                '-movflags', '+faststart',
                output_path
            ]

        rc = self._run_ffmpeg_with_progress(cmd, 0, "DASH混流")
        if rc == 0 and not self.stop_flag:
            self.log(f"视频保存成功：{_short_file(output_path)}", "INFO")
            return True
        else:
            if self.stop_flag:
                self.log("下载已停止", "WARNING")
            else:
                self.log(f"FFmpeg 混流失败（返回码={rc}）", "ERROR")
            try:
                if os.path.exists(output_path):
                    os.remove(output_path)
            except Exception:
                pass
            return False

    # ==================== v4.0 新增下载方法 ====================

    def _download_ts_ffmpeg(self, resource: VideoResource, output_path: str,
                            headers: Dict[str, str]) -> bool:
        """TS 直链下载：FFmpeg 拉取并 remux 为 mp4"""
        url = resource.url
        header_str = self._build_ffmpeg_headers(resource, headers)
        ua = headers.get('User-Agent', '')
        referer = headers.get('Referer', '')
        self.log(f"FFmpeg 下载 TS 直链：{url[:80]}", "INFO")
        # TS 流传 mp4 容器需要 aac_adtstoasc bitstream filter
        cmd = [
            self.ffmpeg_path, '-y',
            '-headers', header_str,
            '-user_agent', ua,
            '-referer', referer,
            '-reconnect', '1', '-reconnect_streamed', '1',
            '-reconnect_delay_max', '5', '-rw_timeout', '300000',
            '-protocol_whitelist', FFMPEG_PROTOCOL_WHITELIST,
            '-i', url,
            '-c', 'copy',
            '-bsf:a', 'aac_adtstoasc',
            '-movflags', '+faststart',
            output_path
        ]
        rc = self._run_ffmpeg_with_progress(cmd, 0, "TS下载")
        if rc == 0 and not self.stop_flag:
            self.log(f"视频保存成功：{_short_file(output_path)}", "INFO")
            return True
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
        except Exception:
            pass
        return False

    def _download_live(self, resource: VideoResource, output_path: str,
                       headers: Dict[str, str]) -> bool:
        """直播录制：FFmpeg 持续拉流，直到用户停止"""
        url = resource.url
        header_str = self._build_ffmpeg_headers(resource, headers)
        ua = headers.get('User-Agent', '')
        referer = headers.get('Referer', '')
        self.log(f"开始直播录制：{url[:80]}", "INFO")
        self.log("点击停止按钮结束录制", "INFO")
        self.recording_start_time = time.time()
        cmd = [
            self.ffmpeg_path, '-y',
            '-headers', header_str,
            '-user_agent', ua,
            '-referer', referer,
            '-reconnect', '1', '-reconnect_streamed', '1',
            '-reconnect_delay_max', '5',
            '-protocol_whitelist', FFMPEG_PROTOCOL_WHITELIST,
            '-i', url,
            '-c', 'copy',
            '-movflags', '+faststart',
            output_path
        ]
        # 录制模式：进度用已录制时间显示
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', errors='replace'
        )
        self._ffmpeg_procs.append(proc)
        try:
            stderr_q: queue.Queue = queue.Queue()
            def _drain():
                try:
                    for line in proc.stderr:
                        stderr_q.put(line)
                except Exception:
                    pass
                finally:
                    stderr_q.put(None)
            def _drain_out():
                try:
                    for _ in proc.stdout:
                        pass
                except Exception:
                    pass
            t1 = threading.Thread(target=_drain, daemon=True)
            t2 = threading.Thread(target=_drain_out, daemon=True)
            t1.start(); t2.start()
            while True:
                if self.stop_flag:
                    # 先给 FFmpeg 发 q 让其正常收尾（MP4 的 moov 才能写入），超时再强杀
                    try:
                        if proc.stdin:
                            proc.stdin.write('q')
                            proc.stdin.flush()
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=8)
                    except Exception:
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except Exception:
                            proc.kill()
                    elapsed = int(time.time() - self.recording_start_time)
                    self.log(f"录制已停止，时长 {elapsed//60}分{elapsed%60}秒", "INFO")
                    if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                        if proc.returncode not in (0, None):
                            self.log(f"注意：FFmpeg 返回码 {proc.returncode}，录制文件可能不完整", "WARNING")
                        self.log(f"录制文件已保存：{output_path}", "INFO")
                        return True
                    return False
                try:
                    line = stderr_q.get(timeout=0.5)
                except queue.Empty:
                    if proc.poll() is not None and not t1.is_alive():
                        break
                    elapsed = int(time.time() - self.recording_start_time)
                    self.progress(50, f"录制中... {elapsed//60:02d}:{elapsed%60:02d}")
                    continue
                if line is None:
                    break
                m = re.search(r'time=(\d+):(\d+):(\d+\.?\d*)', line)
                if m:
                    h, mi, s = m.groups()
                    cur = int(h)*3600 + int(mi)*60 + float(s)
                    self.progress(50, f"录制中... {int(cur)//60:02d}:{int(cur)%60:02d}")
            proc.wait(timeout=10)
            has_data = os.path.exists(output_path) and os.path.getsize(output_path) > 0
            if proc.returncode == 0 and has_data:
                self.log(f"录制文件已保存：{output_path}", "INFO")
                return True
            if has_data:
                self.log(f"录制结束但 FFmpeg 返回码 {proc.returncode}，文件可能不完整", "WARNING")
            return False
        finally:
            try:
                self._ffmpeg_procs.remove(proc)
            except ValueError:
                pass

    def _fetch_aes_key(self, key_uri: str, headers: Dict[str, str]) -> Optional[bytes]:
        """下载 AES-128 密钥（带缓存）"""
        if not key_uri:
            return None
        if key_uri in self._key_cache:
            return self._key_cache[key_uri]
        key_data = self._download_content(key_uri, binary=True, headers=headers)
        if key_data and len(key_data) == 16:
            self._key_cache[key_uri] = key_data
            return key_data
        self.log(f"密钥下载失败或长度异常({len(key_data) if key_data else 0})", "WARNING")
        return None

    def _resolve_m3u8_master(self, streams: List[M3U8Stream],
                             headers: Dict[str, str],
                             max_depth: int = 5) -> Optional[M3U8Stream]:
        """递归解析 master playlist，选择最高码率的子清单并下载分片列表

        max_depth 限制多级 master 的递归层数，防止恶意/错误清单循环引用导致无限递归。
        """
        if not streams:
            return None
        if max_depth <= 0:
            self.log("master playlist 嵌套层级超过限制(5)，已停止递归解析", "ERROR")
            return None
        # 找 master 流（有 variant streams）
        master_streams = [s for s in streams if s.is_master and s.bandwidth > 0]
        if not master_streams:
            # 非 master：返回有分片的流
            for s in streams:
                if s.segments:
                    return s
            return streams[0]
        # 选最高码率
        best = max(master_streams, key=lambda s: s.bandwidth)
        content = self._download_content(best.url, headers=headers)
        if not content:
            self.log("子清单下载失败", "ERROR")
            return None
        sub_streams = self.parser.parse(content, best.url)
        # 递归（某些网站有多级 master）
        return self._resolve_m3u8_master(sub_streams, headers, max_depth - 1)

    def _download_m3u8_local(self, resource: VideoResource, output_path: str,
                             headers: Dict[str, str]) -> bool:
        """M3U8 本地分片下载（支持 AES-128 解密、断点续传、master 递归、本地文件）"""
        m3u8_url = resource.url
        # 本地导入的 m3u8：统一转成 file:// URI，保证相对分片路径能正确拼接
        if not re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*://', m3u8_url) and os.path.isfile(m3u8_url):
            m3u8_url = Path(m3u8_url).resolve().as_uri()

        # 初始化断点续传
        self._init_resume(m3u8_url, output_path)

        # v4.0: 使用持久化分片目录（在输出文件旁），确保重启后分片仍在
        # 目录名只由 m3u8_url 的 md5 决定，改名/另存不会产生无法清理的孤儿目录
        old_temp_dir = self.temp_dir
        url_tag = hashlib.md5(m3u8_url.encode('utf-8')).hexdigest()[:8]
        seg_dir = Path(output_path).parent / f".m3u8_{url_tag}.segments"
        try:
            seg_dir.mkdir(parents=True, exist_ok=True)
            self.temp_dir = seg_dir

            # 1. 下载清单
            content = self._download_content(m3u8_url, headers=headers)
            if not content:
                self.log("M3U8 清单下载失败", "ERROR")
                return False

            # 2. 解析清单
            self.parser.base_url = m3u8_url
            streams = self.parser.parse(content, m3u8_url)
            if not streams:
                self.log("M3U8 解析结果为空", "ERROR")
                return False

            # 3. 递归解析 master，拿到最终分片流
            stream = self._resolve_m3u8_master(streams, headers)
            if not stream or not stream.segments:
                self.log("未找到可用分片", "ERROR")
                return False

            segments = stream.segments
            has_aes = any(seg.key_method == 'AES-128' for seg in segments)
            enc = "加密(AES-128)" if has_aes else "未加密"
            workers = max(1, min(self.max_workers, self.segment_workers))
            self.log(f"本地分片下载：{len(segments)} 片（{enc}，{workers} 线程）", "INFO")

            # 4. 跳过片头广告分片
            if self.skip_ad_segments > 0:
                if self.skip_ad_segments >= len(segments):
                    self.log(f"跳过片头分片数({self.skip_ad_segments})不小于总分片数({len(segments)})，"
                             f"已忽略该设置", "WARNING")
                else:
                    segments = segments[self.skip_ad_segments:]
                    self.log(f"跳过前 {self.skip_ad_segments} 个片头分片", "INFO")
            if not segments:
                self.log("没有可下载的分片", "ERROR")
                return False

            # 5. 按分片实际加密方式获取密钥（EXT-X-KEY 可在清单中途切换）：
            #    不能再用 stream.key_method 统一判断，否则中途出现 METHOD=NONE 时
            #    前面的 AES 分片会被当成明文，密钥也永远不会被下载。
            unsupported = sorted({seg.key_method for seg in segments
                                  if seg.key_method and seg.key_method not in ('NONE', 'AES-128')})
            if unsupported:
                self.log(f"不支持的加密方式: {', '.join(unsupported)}，无法在本地解密分片", "ERROR")
                return False
            aes_segments = [seg for seg in segments if seg.key_method == 'AES-128']
            if aes_segments:
                for seg in aes_segments:
                    if not seg.key_uri:
                        self.log(f"分片序号 {seg.media_sequence} 标记为 AES-128 但缺少密钥 URI，无法解密", "ERROR")
                        return False
                    key_data = self._fetch_aes_key(seg.key_uri, headers)
                    if not key_data:
                        self.log(f"AES 密钥获取失败（{seg.key_uri}），无法解密", "ERROR")
                        return False
                    seg.key_data = key_data

            # 6. 多线程下载分片
            downloaded = self._download_segments(segments, headers)
            if self.stop_flag:
                self._save_resume_state()
                self.log("下载已停止，已完成分片的续传信息已保存", "WARNING")
                return False
            if len(downloaded) < len(segments):
                self.log(f"分片下载不完整: {len(downloaded)}/{len(segments)}", "ERROR")
                return False

            # 7. 合并分片
            ok = self._merge_segments(downloaded, output_path)
            if ok and (not os.path.exists(output_path) or os.path.getsize(output_path) == 0):
                # 空输出不能算成功，否则会清理续传数据并误报“下载完成”
                self.log("合并后的输出文件为空，下载失败", "ERROR")
                ok = False
            if ok:
                self._cleanup_resume()
                # 清理持久化分片目录
                try:
                    shutil.rmtree(seg_dir, ignore_errors=True)
                except Exception:
                    pass
                self.log("下载完成", "INFO")
            return ok
        finally:
            # 无论成功失败都恢复临时目录指向，避免 _cleanup_temp 误删断点续传分片
            self.temp_dir = old_temp_dir

    def download_subtitle(self, sub_url: str, output_path: str,
                          headers: Optional[Dict] = None) -> bool:
        """下载 HLS 字幕（WebVTT 分片合并为 SRT）"""
        try:
            content = self._download_content(sub_url, headers=headers)
            if not content:
                return False
            parser = M3U8Parser(sub_url)
            streams = parser.parse(content, sub_url)
            stream = self._resolve_m3u8_master(streams, headers or {})
            if not stream or not stream.segments:
                # 可能直接是 VTT 文件
                with open(output_path, 'w', encoding='utf-8') as f:
                    f.write(content if content.startswith('WEBVTT') else 'WEBVTT\n\n' + content)
                return True
            # 下载所有 VTT 分片并合并
            vtt_parts = []
            for i, seg in enumerate(stream.segments):
                if self.stop_flag:
                    return False
                part = self._download_content(seg.url, headers=headers)
                if part:
                    vtt_parts.append(part)
                self.progress((i+1)/len(stream.segments)*100, f"字幕下载 {i+1}/{len(stream.segments)}")
            vtt_content = '\n\n'.join(vtt_parts)
            # 简单 VTT → SRT 转换
            srt_content = self._vtt_to_srt(vtt_content)
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(srt_content)
            self.log(f"字幕已保存：{output_path}", "INFO")
            return True
        except Exception as e:
            self.log(f"字幕下载失败: {e}", "ERROR")
            return False

    @staticmethod
    def _vtt_to_srt(vtt: str) -> str:
        """简单 WebVTT 转 SRT"""
        lines = vtt.replace('\r\n', '\n').split('\n')
        out = []
        idx = 1
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if '-->' in line:
                # 时间格式转换 00:00:00.000 --> 00:00:05.000
                time_line = line.replace('.', ',')
                out.append(str(idx))
                out.append(time_line)
                idx += 1
                i += 1
                while i < len(lines) and lines[i].strip():
                    out.append(lines[i])
                    i += 1
                out.append('')
            i += 1
        return '\n'.join(out)

    def extract_audio(self, resource: VideoResource, output_path: str,
                      headers: Optional[Dict] = None) -> bool:
        """从视频资源提取音频（FFmpeg -vn）"""
        try:
            request_headers = self._prepare_headers(resource, headers)
            header_str = self._build_ffmpeg_headers(resource, request_headers)
            ua = request_headers.get('User-Agent', '')
            referer = request_headers.get('Referer', '')
            self.log(f"提取音频: {resource.url[:80]}", "INFO")
            # 根据输出扩展名选择编码器
            ext = os.path.splitext(output_path)[1].lower()
            if ext == '.mp3':
                acodec = ['-c:a', 'libmp3lame', '-q:a', '2']
            elif ext in ('.aac', '.m4a'):
                acodec = ['-c:a', 'aac', '-b:a', '192k']
            else:
                acodec = ['-c:a', 'copy']
            cmd = [
                self.ffmpeg_path, '-y',
                '-headers', header_str,
                '-user_agent', ua,
                '-referer', referer,
                '-reconnect', '1', '-reconnect_streamed', '1',
                '-reconnect_delay_max', '5', '-rw_timeout', '300000',
                '-protocol_whitelist', FFMPEG_PROTOCOL_WHITELIST,
                '-i', resource.url,
                '-vn', *acodec,
                output_path
            ]
            rc = self._run_ffmpeg_with_progress(cmd, 0, "音频提取")
            if rc == 0 and not self.stop_flag:
                self.log(f"音频已保存：{output_path}", "INFO")
                return True
            try:
                if os.path.exists(output_path):
                    os.remove(output_path)
            except Exception:
                pass
            return False
        except Exception as e:
            self.log(f"音频提取失败: {e}", "ERROR")
            return False

    @staticmethod
    def _local_path_from_url(url: str) -> Optional[str]:
        """把 file:// URL 还原成本地路径（Windows 兼容 /C:/dir/file）"""
        try:
            parsed = urlparse(url)
            if parsed.scheme.lower() != 'file':
                return None
            path = unquote(parsed.path)
            if sys.platform == 'win32' and re.match(r'^/[A-Za-z]:', path):
                path = path[1:]
            return os.path.normpath(path)
        except Exception:
            return None

    def _request_url(self, url: str, headers: Optional[Dict],
                     try_alt: bool = False):
        """请求 URL；分片模式下遇到 SSL 异常时尝试免校验/HTTP 回退"""
        if not try_alt:
            return self.session.get(url, timeout=30, headers=headers)

        candidates = [(url, True)]
        if url.lower().startswith('https://'):
            candidates.append((url, False))
            candidates.append(('http://' + url[8:], True))
        last_exc = None
        for target, verify in candidates:
            try:
                if not verify:
                    # 静默关闭证书校验存在中间人风险，至少记录一次 WARNING
                    if not self._insecure_warned:
                        self._insecure_warned = True
                        self.log(
                            f"TLS 证书校验失败，已降级为免校验请求（verify=False，存在中间人风险）：{target}",
                            "WARNING")
                    try:
                        import urllib3
                        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                    except Exception:
                        pass
                resp = self.session.get(target, timeout=30, headers=headers, verify=verify)
                resp.raise_for_status()
                return resp
            except Exception as e:
                last_exc = e
                continue
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("请求失败")

    def _download_content(self, url: str, binary: bool = False,
                          headers: Optional[Dict] = None,
                          quiet: bool = False, try_alt: bool = False,
                          attempts: Optional[int] = None) -> Optional[Union[str, bytes]]:
        """下载内容（支持 file:// 本地文件；分片模式静默失败并尝试 SSL/HTTP 回退）"""
        # 本地 m3u8 导入后走这里读取清单，requests 不支持 file:// 协议
        local_path = self._local_path_from_url(url)
        if local_path is not None:
            try:
                if binary:
                    with open(local_path, 'rb') as f:
                        return f.read()
                with open(local_path, 'r', encoding='utf-8', errors='replace') as f:
                    return f.read()
            except Exception as e:
                self._last_error = f"读取本地文件失败: {e}"
                if not quiet:
                    self.log(f"读取本地文件失败 {local_path}: {e}", "ERROR")
                return None

        max_attempts = attempts if attempts is not None else self.retry_times
        for attempt in range(max(1, max_attempts)):
            if self.stop_flag or self._segment_abort:
                return None
            try:
                response = self._request_url(url, headers, try_alt=try_alt)
                if not try_alt:
                    response.raise_for_status()
                if binary:
                    return response.content
                return response.text
            except requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response is not None else 0
                self._last_error = f"HTTP {status}"
                # 4xx 客户端错误（除 429 限流外）不重试
                if 400 <= status < 500 and status != 429:
                    if not quiet:
                        self.log(f"下载 {url} 失败(HTTP {status})，不重试", "WARNING")
                    return None
                if attempt == max_attempts - 1:
                    if not quiet:
                        self.log(f"下载 {url} 失败: {e}", "WARNING")
                    return None
                time.sleep(random.uniform(0.5, 1.5) if quiet else random.uniform(1, 3))
            except Exception as e:
                self._last_error = f"{type(e).__name__}: {e}"
                if attempt == max_attempts - 1:
                    if not quiet:
                        self.log(f"下载 {url} 失败: {e}", "WARNING")
                    return None
                time.sleep(random.uniform(0.5, 1.5) if quiet else random.uniform(1, 3))
        return None

    def _download_segments(self, segments: List[M3U8Segment],
                           headers: Optional[Dict[str, str]] = None,
                           workers_override: Optional[int] = None) -> List[Path]:
        """多线程下载分片（失败聚合、连续失败中止、TLS 失败自动降并发重试）"""
        downloaded = []
        total = len(segments)
        completed = 0
        failed = 0
        resume_skipped = 0
        self._segment_abort = False
        self._last_error = ""
        self._blocked_log_count = 0
        requested_workers = workers_override or self.segment_workers
        workers = max(1, min(self.max_workers, requested_workers))
        started = time.time()

        self._executor = ThreadPoolExecutor(max_workers=workers)
        futures = {}
        for idx, seg in enumerate(segments):
            # 断点续传：跳过已下载的分片
            if seg.url in self._resume_map and self._resume_map[seg.url].exists():
                downloaded.append((idx, self._resume_map[seg.url]))
                resume_skipped += 1
                completed += 1
                continue
            futures[self._executor.submit(self._download_segment, seg, idx, headers)] = idx

        save_counter = 0
        for future in as_completed(futures):
            if self.stop_flag or self._segment_abort:
                break
            idx = futures[future]
            try:
                result = future.result()
                if result:
                    downloaded.append((idx, result))
                    save_counter += 1
                    # 每 10 个分片保存一次续传状态
                    if save_counter % 10 == 0:
                        self._save_resume_state()
                else:
                    failed += 1
                    if failed == 1:
                        self.log(f"分片下载失败，正在重试；首个错误：{self._last_error[:180]}", "WARNING")
                    if failed >= self.max_segment_failures:
                        self._segment_abort = True
                        self.log(
                            f"连续失败 {failed} 个分片，当前批次中止。"
                            f"通常是 CDN 限制并发/TLS 或链接已过期。",
                            "ERROR")
                        break
            except Exception as e:
                failed += 1
                self._last_error = f"{type(e).__name__}: {e}"
            completed += 1
            if completed == total or completed % 5 == 0:
                self.progress((completed / total) * 100, f"已下载 {completed}/{total}")

        # 停止/中止时关闭线程池，取消待执行任务
        if self._executor is not None:
            try:
                self._executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                self._executor.shutdown(wait=False)
        self._executor = None

        # 连接被 CDN 重置时，自动从高并发降到 4 线程重试一次
        err_lower = (self._last_error or '').lower()
        network_error = any(k in err_lower for k in
                            ('ssl', 'eof', 'connection', 'timeout', 'reset', 'tls', 'broken pipe'))
        if self._segment_abort and not self.stop_flag and workers > 4 and network_error:
            self._save_resume_state()
            self.log("检测到 CDN 断开连接，自动降低并发到 4 线程重试已下载进度...", "WARNING")
            self._segment_abort = False
            self._last_error = ""
            self._blocked_log_count = 0
            return self._download_segments(segments, headers, workers_override=4)

        if failed:
            self.log(
                f"分片下载统计：成功 {len(downloaded)} 个，失败 {failed} 个，总计 {total} 个"
                + (f"（最近错误：{self._last_error[:140]}）" if self._last_error else ""),
                "WARNING")
        elif resume_skipped:
            self.log(f"断点续传：跳过 {resume_skipped} 个已完成分片", "INFO")

        if downloaded:
            elapsed = max(0.01, time.time() - started)
            total_bytes = sum(p.stat().st_size for _, p in downloaded if p.exists())
            speed = total_bytes / elapsed / (1024 * 1024)
            self.log(
                f"分片下载速度：{speed:.2f} MB/s（{len(downloaded)}/{total} 片，{elapsed:.1f} 秒）",
                "INFO" if not failed else "WARNING")

        downloaded.sort(key=lambda x: x[0])
        return [path for _, path in downloaded]

    def _download_segment(self, segment: M3U8Segment, index: int,
                          headers: Optional[Dict[str, str]] = None) -> Optional[Path]:
        """下载单个分片（支持 aria2 加速后端）"""
        # 用分片 URL 的哈希命名，保证同一分片在 skip_ad_segments 变化、
        # 或重试/降并发重新调度时落到同一个文件，续传映射不会错位。
        url_tag = hashlib.md5(segment.url.encode('utf-8', errors='replace')).hexdigest()[:16]
        segment_path = self.temp_dir / f"segment_{url_tag}.ts"
        seg_headers = headers or {}
        for attempt in range(self.retry_times):
            if self.stop_flag or self._segment_abort:
                return None
            try:
                delay = random.uniform(self.min_delay, self.max_delay)
                time.sleep(delay)

                # aria2 后端：直接下载到文件（本地 file:// 分片不支持 aria2）
                if self.use_aria2 and self.aria2_path and not segment.url.lower().startswith('file://'):
                    if self._aria2_download_segment(segment.url, str(segment_path), seg_headers, self.aria2_path):
                        with open(segment_path, 'rb') as f:
                            data = f.read()
                        self._throttle(len(data))
                        if not self._validate_segment(data, segment.url):
                            continue
                        if segment.key_method == 'AES-128' and segment.key_data and segment.key_uri:
                            data = self._decrypt_segment(data, segment)
                            if data is None:
                                # 解密失败的分片不能作为成功分片落盘/记入续传
                                continue
                            with open(segment_path, 'wb') as f:
                                f.write(data)
                        self._resume_map[segment.url] = segment_path
                        return segment_path
                    continue

                data = self._download_content(segment.url, binary=True, headers=seg_headers,
                                              quiet=True, try_alt=True, attempts=2)
                if not data:
                    continue

                self._throttle(len(data))
                if not self._validate_segment(data, segment.url):
                    continue

                # 自动解密（仅解密该分片实际标记为 AES-128 的）
                if segment.key_method == 'AES-128' and segment.key_data and segment.key_uri:
                    data = self._decrypt_segment(data, segment)
                    if data is None:
                        # 解密失败：不写文件、不记续传，交给上层判定失败
                        continue

                with open(segment_path, 'wb') as f:
                    f.write(data)
                # 记录到续传表
                self._resume_map[segment.url] = segment_path
                return segment_path

            except Exception:
                if attempt == self.retry_times - 1:
                    return None
        return None

    def _validate_segment(self, data: bytes, url: str) -> bool:
        """校验分片数据（异常时只记录前 3 条，避免整片失败刷屏）"""
        if len(data) < 2:
            self._last_error = "分片数据为空"
            return False
        if data[:2] == b'BM':
            self._last_error = "服务器返回 BMP 图片（可能被拦截）"
            self._blocked_log_count += 1
            if self._blocked_log_count <= 3:
                self.log(f"服务器拦截（BMP图片）：{url}", "ERROR")
            return False
        if b'<html' in data[:100].lower() or b'<!DOCTYPE' in data[:100].upper():
            self._last_error = "服务器返回 HTML（可能被拦截）"
            self._blocked_log_count += 1
            if self._blocked_log_count <= 3:
                self.log(f"服务器拦截（HTML）：{url}", "ERROR")
            return False
        if data[:1] == b'{' or data[:1] == b'[':
            try:
                json.loads(data.decode('utf-8', errors='ignore'))
                self._last_error = "服务器返回 JSON 错误"
                self._blocked_log_count += 1
                if self._blocked_log_count <= 3:
                    self.log(f"服务器返回JSON错误：{url}", "ERROR")
                return False
            except Exception:
                pass
        return True

    def _decrypt_segment(self, data: bytes, segment: M3U8Segment) -> Optional[bytes]:
        """解密分片（AES-128-CBC，符合 RFC 8216 规范，支持非标准私有加密回退）

        返回 None 表示解密失败（密钥/密文长度异常或解密抛错），调用方不得把
        原始密文当成成功分片落盘。
        """
        try:
            if not segment.key_data:
                return data

            if AES is None:
                self.log("缺少 pycryptodome，无法解密 AES-128 分片（pip install pycryptodome）", "ERROR")
                return None

            # 校验密钥长度（AES-128 需要 16 字节密钥）
            if len(segment.key_data) != 16:
                self.log(f"分片序号 {segment.media_sequence} 密钥长度异常({len(segment.key_data)})，无法解密", "ERROR")
                return None

            # 校验数据长度（AES-CBC 要求数据为 16 的倍数）
            if len(data) == 0 or len(data) % 16 != 0:
                self.log(f"分片序号 {segment.media_sequence} 数据长度异常({len(data)})，无法解密", "ERROR")
                return None

            # 构造 IV：优先显式 IV；无 IV 时用媒体序列号作 16 字节大端整数
            has_explicit_iv = segment.iv is not None
            iv = None
            if has_explicit_iv:
                if len(segment.iv) == 16:
                    iv = segment.iv
                elif len(segment.iv) < 16:
                    iv = segment.iv.rjust(16, b'\0')
                    self.log(f"分片序号 {segment.media_sequence} IV 长度不足({len(segment.iv)})，左侧补零", "WARNING")
                else:
                    iv = segment.iv[:16]
                    self.log(f"分片序号 {segment.media_sequence} IV 长度超长({len(segment.iv)})，截取前16字节", "WARNING")
            else:
                # RFC 8216: 无显式 IV 时，使用分片的媒体序列号作为 IV
                seq = segment.media_sequence % (1 << 128)
                iv = seq.to_bytes(16, byteorder='big')

            cipher = AES.new(segment.key_data, AES.MODE_CBC, iv)
            decrypted = cipher.decrypt(data)

            # 验证解密结果是否为有效媒体数据
            if not self._is_valid_decrypted(decrypted):
                # 非标准私有加密回退：尝试替代 IV 策略（仅当无显式 IV 时）
                if not has_explicit_iv:
                    for fallback_iv, label in [
                        (b'\x00' * 16, '零IV'),
                        (segment.key_data, '密钥作IV'),
                    ]:
                        try:
                            cipher2 = AES.new(segment.key_data, AES.MODE_CBC, fallback_iv)
                            alt_decrypted = cipher2.decrypt(data)
                            if self._is_valid_decrypted(alt_decrypted):
                                self.log(f"分片序号 {segment.media_sequence} 标准 IV 解密无效，{label} 回退成功", "WARNING")
                                decrypted = alt_decrypted
                                break
                        except Exception:
                            continue
                    else:
                        self.log(f"分片序号 {segment.media_sequence} 所有 IV 策略解密结果均无效，使用标准 IV 结果", "WARNING")

            # PKCS7 去填充，完整合法性校验
            if decrypted:
                pad_length = decrypted[-1]
                if 1 <= pad_length <= 16 and len(decrypted) >= pad_length:
                    padding = decrypted[-pad_length:]
                    if all(b == pad_length for b in padding):
                        decrypted = decrypted[:-pad_length]
                    else:
                        self.log(f"分片序号 {segment.media_sequence} PKCS7 填充字节不匹配，保留原始解密数据", "WARNING")
                else:
                    self.log(f"分片序号 {segment.media_sequence} PKCS7 填充值越界({pad_length})，保留原始解密数据", "WARNING")

            return decrypted

        except ValueError as e:
            self.log(f"分片序号 {segment.media_sequence} 解密参数错误: {e}", "ERROR")
            return None
        except Exception as e:
            self.log(f"分片序号 {segment.media_sequence} 解密异常: {e}", "ERROR")
            self.log(traceback.format_exc(), "ERROR")
            return None

    @staticmethod
    def _is_valid_decrypted(data: bytes) -> bool:
        """检测解密后的数据是否为有效媒体（TS/fMP4 头部校验）"""
        if not data or len(data) < 4:
            return False
        # MPEG-TS: 同步字节 0x47，每 188 字节出现
        if data[0] == 0x47 and len(data) >= 189 and data[188] == 0x47:
            return True
        # fMP4: ftyp box
        if data[:4] == b'\x66\x74\x79\x70':
            return True
        # fMP4: moof/mdat/styp box (offset 4-8)
        if len(data) >= 8 and data[4:8] in (b'moof', b'mdat', b'styp', b'sidx'):
            return True
        # 部分分片可能以 PAT/PMT 开头而非同步字节，检查是否含多个 0x47
        ts_sync_count = sum(1 for i in range(0, min(len(data), 188 * 5), 188) if i < len(data) and data[i] == 0x47)
        return ts_sync_count >= 3

    def _select_best_stream(self, streams: List[M3U8Stream]) -> Optional[M3U8Stream]:
        if not streams:
            return None
        return max(streams, key=lambda s: s.bandwidth)

    def _get_media_duration(self, file_path: str) -> float:
        """使用 FFmpeg 获取媒体时长（秒）"""
        try:
            result = subprocess.run(
                [self.ffmpeg_path, '-i', str(file_path), '-f', 'null', '-'],
                capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=10
            )
            match = re.search(r'Duration:\s*(\d+):(\d+):(\d+\.?\d*)', result.stderr or '')
            if match:
                h, m, s = match.groups()
                return int(h) * 3600 + int(m) * 60 + float(s)
        except Exception:
            pass
        return 0.0

    def _run_ffmpeg_with_progress(self, cmd: list, total_duration: float,
                                  label: str = "合并视频") -> int:
        """运行 FFmpeg 并解析 stderr 进度，返回 returncode"""
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding='utf-8', errors='replace'
            )
            self._ffmpeg_procs.append(proc)
            try:
                # 修复：用后台线程读取 stderr，避免 readline 阻塞导致 stop_flag 无法响应
                stderr_q: queue.Queue = queue.Queue()

                def _drain_stderr():
                    try:
                        for line in proc.stderr:
                            stderr_q.put(line)
                    except Exception:
                        pass
                    finally:
                        stderr_q.put(None)

                # 同时排空 stdout，防止管道缓冲区写满导致 FFmpeg 阻塞
                def _drain_stdout():
                    try:
                        for _ in proc.stdout:
                            pass
                    except Exception:
                        pass

                t_err = threading.Thread(target=_drain_stderr, daemon=True)
                t_out = threading.Thread(target=_drain_stdout, daemon=True)
                t_err.start()
                t_out.start()

                # 保留 stderr 尾部：FFmpeg 失败时唯一能说明原因的就是这几行
                err_tail = deque(maxlen=12)

                while True:
                    if self.stop_flag:
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except Exception:
                            proc.kill()
                        return -1
                    try:
                        line = stderr_q.get(timeout=0.5)
                    except queue.Empty:
                        if proc.poll() is not None and not t_err.is_alive():
                            break
                        continue
                    if line is None:
                        break
                    text = line.strip()
                    if text:
                        err_tail.append(text)
                    # 从 FFmpeg 初始输出解析总时长（concat 合并时比单分片探测更准确）
                    if total_duration <= 0:
                        d_match = re.search(r'Duration:\s*(\d+):(\d+):(\d+\.?\d*)', line)
                        if d_match:
                            dh, dm, ds = d_match.groups()
                            total_duration = int(dh) * 3600 + int(dm) * 60 + float(ds)
                    # 解析当前处理时间，计算进度
                    match = re.search(r'time=(\d+):(\d+):(\d+\.?\d*)', line)
                    if match and total_duration > 0:
                        h, m, s = match.groups()
                        current = int(h) * 3600 + int(m) * 60 + float(s)
                        pct = min((current / total_duration) * 100, 99)
                        self.progress(pct, f"{label}... {pct:.0f}%")
                proc.wait(timeout=10)
                if proc.returncode != 0 and err_tail:
                    self.log(f"{label}：FFmpeg 返回 {proc.returncode}，错误输出：", "ERROR")
                    for text in err_tail:
                        self.log(f"  {text[:300]}", "ERROR")
                return proc.returncode
            finally:
                try:
                    self._ffmpeg_procs.remove(proc)
                except ValueError:
                    pass
        except Exception as e:
            self.log(f"FFmpeg 执行异常: {e}", "ERROR")
            return -1

    def _merge_ts_fast(self, segments: List[Path], output_path: str) -> bool:
        """TS 分片快速合并：先顺序二进制拼接，再单文件封装为 MP4/TS

        比 FFmpeg concat 逐个打开 1000+ 文件快很多，避免合并阶段长时间卡住。
        """
        merged_ts = self.temp_dir / "merged_concat.ts"
        total = len(segments)
        started = time.time()
        self.log(f"TS 分片快速拼接：{total} 个分片...", "INFO")
        try:
            with open(merged_ts, 'wb') as outfile:
                for i, seg in enumerate(segments, 1):
                    if self.stop_flag:
                        return False
                    with open(seg, 'rb') as infile:
                        shutil.copyfileobj(infile, outfile, length=4 * 1024 * 1024)
                    if i == total or i % max(1, total // 10) == 0:
                        self.progress((i / total) * 100, f"拼接分片 {i}/{total}")
        except Exception as e:
            self.log(f"TS 二进制拼接失败: {e}", "ERROR")
            return False

        out_ext = os.path.splitext(output_path)[1].lower()
        if out_ext == '.ts':
            try:
                os.replace(merged_ts, output_path)
                self.progress(100, "合并完成")
                self.log(f"TS 分片已直接合并完成（{time.time() - started:.1f} 秒）", "INFO")
                return True
            except Exception as e:
                self.log(f"保存 TS 文件失败: {e}", "ERROR")
                return False

        # 单文件 remux，避免 concat 逐个打开 1455 个文件
        self.log("TS 拼接完成，正在封装为 MP4...", "INFO")
        cmd = [
            self.ffmpeg_path, '-y',
            '-i', str(merged_ts),
            '-c', 'copy',
            '-bsf:a', 'aac_adtstoasc',
            str(Path(output_path))
        ]
        rc = self._run_ffmpeg_with_progress(cmd, 0, "封装MP4")
        try:
            merged_ts.unlink(missing_ok=True)
        except Exception:
            pass
        if rc == 0:
            self.progress(100, "合并完成")
            self.log(f"MP4 封装完成（{time.time() - started:.1f} 秒）", "INFO")
            return True
        self.log(f"MP4 封装失败（返回码={rc}），尝试常规合并", "WARNING")
        return False

    def _merge_segments(self, segments: List[Path], output_path: str) -> bool:
        """使用 FFmpeg 合并分片（使用 pathlib.Path 处理路径）"""
        try:
            merge_started = time.time()
            is_ts = self._is_ts_segment(segments[0]) if segments else True

            # 大量 TS 分片走快速路径，避免 FFmpeg concat 打开上千文件
            if is_ts and len(segments) >= 20:
                if self._merge_ts_fast(segments, output_path):
                    return True
                if self.stop_flag:
                    return False
            elif not is_ts:
                self.log("分片为 fMP4/m4s，使用 FFmpeg 合并（必要时会重编码）...", "INFO")

            # 分片路径可能含单引号（例如输出目录 "John's Videos"），ffmpeg 的
            # concat 清单会把单引号当成字符串边界导致解析失败。这里做两件事：
            # 1) 把 concat 清单放到无特殊字符的临时目录；
            # 2) 按 concat 语法转义路径中的单引号。
            concat_dir = Path(tempfile.mkdtemp(prefix="video_dl_concat_"))
            concat_file = concat_dir / "concat_list.txt"
            try:
                with open(concat_file, 'w', encoding='utf-8') as f:
                    for segment_path in segments:
                        abs_path = Path(segment_path).absolute().as_posix()
                        escaped_path = abs_path.replace("'", "'\\''")
                        f.write(f"file '{escaped_path}'\n")

                cmd = [
                    self.ffmpeg_path, '-y',
                    '-f', 'concat', '-safe', '0',
                    '-i', str(concat_file),
                    '-c', 'copy',
                ]
                if is_ts:
                    cmd.extend(['-bsf:a', 'aac_adtstoasc'])
                cmd.extend(['-fflags', '+genpts', '-avoid_negative_ts', 'make_zero',
                            str(Path(output_path))])

                total_duration = self._get_media_duration(str(segments[0])) if segments else 0
                self.log("执行 FFmpeg 合并...", "INFO")
                rc = self._run_ffmpeg_with_progress(cmd, total_duration)

                if rc != 0 and not self.stop_flag:
                    self.log("FFmpeg copy 合并失败，尝试重编码...", "INFO")
                    cmd_reencode = [
                        self.ffmpeg_path, '-y',
                        '-f', 'concat', '-safe', '0',
                        '-i', str(concat_file),
                        '-c:v', 'libx264', '-c:a', 'aac',
                        str(Path(output_path))
                    ]
                    rc = self._run_ffmpeg_with_progress(cmd_reencode, total_duration, "重编码合并")
                    if rc != 0:
                        self.log("FFmpeg 重编码也失败", "ERROR")
                        return self._fallback_merge(segments, output_path)

                if not self.stop_flag:
                    self.progress(100, "合并完成")
                    self.log(f"FFmpeg 合并完成（{time.time() - merge_started:.1f} 秒）", "INFO")
                return rc == 0
            finally:
                shutil.rmtree(concat_dir, ignore_errors=True)
        except Exception as e:
            self.log(f"FFmpeg 合并失败：{str(e)}，尝试备用方式...", "ERROR")
            return self._fallback_merge(segments, output_path)

    @staticmethod
    def _is_ts_segment(segment_path) -> bool:
        """检测分片是否为 TS 格式（通过读取头部 magic bytes）"""
        try:
            with open(segment_path, 'rb') as f:
                header = f.read(8)
                if len(header) >= 4 and header[:4] == b'\x66\x74\x79\x70':
                    return False
                if len(header) >= 8 and header[4:8] in (b'mdat', b'moof', b'styp'):
                    return False
                return True
        except Exception:
            return True

    def _fallback_merge(self, segments: List[Path], output_path: str) -> bool:
        """备用合并方式：TS 二进制拼接 或 fMP4/m4s FFmpeg concat filter"""
        try:
            is_ts = self._is_ts_segment(segments[0]) if segments else True

            if not is_ts:
                self.log("分片为 fMP4/m4s 格式，尝试 FFmpeg concat filter 合并...", "INFO")
                return self._ffmpeg_concat_fmp4(segments, output_path)

            self.log("使用二进制拼接合并(TS)...", "INFO")
            with open(output_path, 'wb') as outfile:
                for segment_path in segments:
                    with open(segment_path, 'rb') as infile:
                        outfile.write(infile.read())
            self.log("二进制合并完成", "INFO")
            return True
        except Exception as e:
            self.log(f"备用合并失败: {e}", "ERROR")
            return False

    def _ffmpeg_concat_fmp4(self, segments: List[Path], output_path: str) -> bool:
        """使用 FFmpeg concat filter 合并 fMP4/m4s 分片（重编码）"""
        try:
            n = len(segments)
            inputs = []
            for seg in segments:
                inputs.extend(['-i', str(seg)])

            filter_parts = []
            for i in range(n):
                filter_parts.append(f'[{i}:v:0][{i}:a:0]')
            filter_str = ''.join(filter_parts) + f'concat=n={n}:v=1:a=1[outv][outa]'

            cmd = [
                self.ffmpeg_path, '-y',
                *inputs,
                '-filter_complex', filter_str,
                '-map', '[outv]', '-map', '[outa]',
                '-c:v', 'libx264', '-c:a', 'aac',
                str(Path(output_path))
            ]
            rc = self._run_ffmpeg_with_progress(cmd, 0, "fMP4合并")
            if rc == 0:
                self.log("fMP4/m4s concat filter 合并完成", "INFO")
                return True

            self.log("concat filter (v+a) 失败，尝试仅视频轨道...", "INFO")
            filter_parts_v = []
            for i in range(n):
                filter_parts_v.append(f'[{i}:v:0]')
            filter_str_v = ''.join(filter_parts_v) + f'concat=n={n}:v=1:a=0[outv]'
            cmd_v = [
                self.ffmpeg_path, '-y',
                *inputs,
                '-filter_complex', filter_str_v,
                '-map', '[outv]',
                '-c:v', 'libx264',
                str(Path(output_path))
            ]
            rc_v = self._run_ffmpeg_with_progress(cmd_v, 0, "fMP4合并(仅视频)")
            if rc_v == 0:
                self.log("fMP4/m4s 合并完成（仅视频轨道）", "INFO")
                return True

            self.log("FFmpeg fMP4 合并全部失败", "ERROR")
            return False
        except Exception as e:
            self.log(f"FFmpeg fMP4 合并异常: {e}", "ERROR")
            return False

    def _cleanup_temp(self):
        """清理临时文件（增强版：多次重试 + 清理残留孤儿目录）"""
        # 1. 清理当前下载器的临时目录
        if self.temp_dir and self.temp_dir.exists():
            for attempt in range(3):
                try:
                    shutil.rmtree(self.temp_dir, ignore_errors=False)
                    break
                except PermissionError:
                    # Windows 下文件可能被占用，等待后重试
                    time.sleep(0.5)
                except Exception as e:
                    logger.warning(f"清理临时目录失败(第{attempt+1}次)：{str(e)}")
                    time.sleep(0.5)
            else:
                # 最后一次尝试：强制忽略错误
                shutil.rmtree(self.temp_dir, ignore_errors=True)

        # 2. 清理系统临时目录中可能残留的孤儿 video_dl_* 目录
        try:
            sys_temp = Path(tempfile.gettempdir())
            for item in sys_temp.glob("video_dl_*"):
                if not item.is_dir():
                    continue
                # 只清理超过 1 小时的残留目录，避免误删正在使用的目录
                try:
                    age = time.time() - item.stat().st_mtime
                    if age > 3600:
                        shutil.rmtree(item, ignore_errors=True)
                        logger.info(f"清理残留临时目录: {item}")
                except Exception:
                    pass
        except Exception:
            pass

    def log(self, message: str, level: str = "INFO"):
        if self.log_callback:
            self.log_callback(message, level)

    def progress(self, value: float, message: str = ""):
        if self.progress_callback:
            self.progress_callback(value, message)

    def stop(self):
        """停止下载（设置 flag + 终止 FFmpeg + 关闭线程池）

        注意：这里不关闭 self.session。Session 关闭后不可复用，而下载器实例
        可能还要执行后续任务；如需彻底释放，由调用方在实例销毁时处理。
        """
        self.stop_flag = True
        # 快照，避免工作线程同时增删列表导致 RuntimeError
        for proc in list(self._ffmpeg_procs):
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._ffmpeg_procs.clear()
        if self._executor:
            try:
                self._executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                self._executor.shutdown(wait=False)
            self._executor = None

# ===================== 微信视频号专用解析（腾讯元宝官方链路） =====================
# 视频号分享短链本身只开放封面，不返回视频流；腾讯元宝的“视频号解析”接口
# 会用官方登录态换到带 token/eid 的播放入口，再通过视频号官方 feed 接口
# 取得 h264VideoInfo/h265VideoInfo 直链。这里只复用官方链路，不做破解。
WECHAT_CHANNELS_AGENT_ID = "naQivTmsDa/cf4d0079-ed1b-4c55-a3f3-2ca1379727d1"
WECHAT_CHANNELS_HOME = "https://yuanbao.tencent.com/"
WECHAT_CHANNELS_FEED_API = "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info"
WECHAT_CHANNELS_FEED_PAGE = "https://channels.weixin.qq.com/finder-preview/pages/feed"
WECHAT_CHANNELS_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36")


class WeChatChannelsError(Exception):
    """视频号解析失败（可直接展示给用户）"""


class WeChatChannelsLoginRequired(WeChatChannelsError):
    """腾讯元宝未登录 / 扫码超时"""


class WeChatChannelsStopped(WeChatChannelsError):
    """用户停止解析"""


class WeChatChannelsResolver:
    """通过腾讯元宝官方登录态解析微信视频号分享链接。

    - 只处理 weixin.qq.com/sph/... 和 channels.weixin.qq.com 的视频号页面；
    - 首次使用需要在弹出的浏览器窗口中登录腾讯元宝，登录态会持久化；
    - 解析成功后返回可直接下载的 MP4 资源（H.264 优先，其次 H.265）。
    """

    LOGIN_CHECK_JS = r"""async () => {
        const w = window.$webApi;
        if (!w || !w.getYbCommonHeaders || !w.setContextualRequestHeaders) {
            return { ready: false, status: 0 };
        }
        const req = {
            url: '/api/getuserinfo',
            headers: {
                ...w.getYbCommonHeaders(),
                'Accept': 'application/json, text/plain, */*',
                'X-Requested-With': 'XMLHttpRequest'
            }
        };
        await w.setContextualRequestHeaders(req);
        const res = await fetch(req.url, {
            method: 'GET', credentials: 'include', headers: req.headers
        });
        return { ready: true, status: res.status };
    }"""

    PARSE_JS = r"""async (args) => {
        const shareUrl = args.shareUrl;
        const agentId = args.agentId;
        const w = window.$webApi;
        if (!w || !w.getYbCommonHeaders || !w.setContextualRequestHeaders) {
            return { status: 0, code: 'YUANBAO_PAGE_NOT_READY', body: '' };
        }
        const baseHeaders = {
            ...w.getYbCommonHeaders(),
            'Accept': 'application/json, text/plain, */*',
            'Content-Type': 'application/json',
            'X-Requested-With': 'XMLHttpRequest'
        };
        const userRequest = { url: '/api/getuserinfo', headers: { ...baseHeaders } };
        await w.setContextualRequestHeaders(userRequest);
        const userResponse = await fetch(userRequest.url, {
            method: 'GET', credentials: 'include', headers: userRequest.headers
        });
        const userText = await userResponse.text();
        if (!userResponse.ok) {
            return { status: userResponse.status, code: 'YUANBAO_LOGIN_REQUIRED', body: userText };
        }
        let userInfo = {};
        try { userInfo = JSON.parse(userText); } catch (_) {}
        const userData = userInfo && typeof userInfo.data === 'object' ? userInfo.data : userInfo;
        const userId = (userData && (userData.userId || userData.userid || userData.user_id || userData.id)) || '';

        const parseRequest = {
            url: '/api/weixin/get_parse_result',
            headers: { ...baseHeaders, 'X-AgentID': agentId }
        };
        await w.setContextualRequestHeaders(parseRequest);
        if (userId) {
            parseRequest.headers['T-UserID'] = String(userId);
            parseRequest.headers['X-ID'] = String(userId);
        }
        const response = await fetch(parseRequest.url, {
            method: 'POST',
            credentials: 'include',
            headers: parseRequest.headers,
            body: JSON.stringify({ type: 'video_channel_url', url: shareUrl, scene: 1 })
        });
        return {
            status: response.status,
            code: response.status === 401 ? 'YUANBAO_LOGIN_REQUIRED' : '',
            body: await response.text()
        };
    }"""

    def __init__(self, log_callback=None, stop_check=None, timeout=40, login_timeout=240):
        self.log_callback = log_callback or (lambda msg, level="INFO": None)
        self.stop_check = stop_check or (lambda: False)
        self.timeout = timeout
        self.login_timeout = login_timeout

    def _log(self, message, level="INFO"):
        try:
            self.log_callback(message, level)
        except Exception:
            pass

    def _ensure_running(self):
        if self.stop_check():
            raise WeChatChannelsStopped("已停止解析")

    @staticmethod
    def _extract_query(url):
        try:
            return dict(parse_qsl(urlparse(url).query, keep_blank_values=True))
        except Exception:
            return {}

    @classmethod
    def is_channels_url(cls, url):
        """判断是否为视频号分享链接/预览页"""
        if not url:
            return False
        try:
            parsed = urlparse(url)
            host = (parsed.netloc or '').lower().split(':')[0]
            path = parsed.path or ''
        except Exception:
            return False
        if host in ('weixin.qq.com', 'www.weixin.qq.com') and path.startswith('/sph/'):
            return True
        if host == 'channels.weixin.qq.com' and (
                path.startswith('/finder-preview/pages/sph') or
                path.startswith('/finder-preview/pages/feed')):
            return True
        return False

    @classmethod
    def _feed_params_from_url(cls, url):
        """预览页 feed 链接若自带 token/eid，可直接跳过元宝"""
        parsed = urlparse(url)
        if not parsed.path.startswith('/finder-preview/pages/feed'):
            return '', ''
        q = cls._extract_query(url)
        token = (q.get('token') or '').strip()
        eid = (q.get('eid') or '').strip()
        if token and eid:
            return token, eid
        return '', ''

    @staticmethod
    def _profile_dir():
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
        path = os.path.join(base, 'VideoDownloader', 'wechat_yuanbao_profile')
        os.makedirs(path, exist_ok=True)
        return path

    @staticmethod
    def _browser_executable():
        if hasattr(sys, '_MEIPASS'):
            ms_pw_dir = Path(sys._MEIPASS) / 'ms-playwright'
            if ms_pw_dir.exists():
                for item in ms_pw_dir.iterdir():
                    if item.is_dir() and item.name.startswith('chromium-'):
                        for sub in ('chrome-win', 'chrome-win64'):
                            exe = item / sub / 'chrome.exe'
                            if exe.exists():
                                return str(exe)
        return None

    @classmethod
    def normalize_share_url(cls, url):
        """把预览页地址还原成元宝可识别的 weixin.qq.com/sph 短链"""
        try:
            parsed = urlparse(url)
            host = (parsed.netloc or '').lower().split(':')[0]
            if host == 'channels.weixin.qq.com' and parsed.path.startswith('/finder-preview/pages/sph'):
                q = cls._extract_query(url)
                short_id = (q.get('id') or '').strip()
                if short_id and re.fullmatch(r'[A-Za-z0-9_-]+', short_id):
                    return f"https://weixin.qq.com/sph/{short_id}"
        except Exception:
            pass
        return url

    def resolve(self, url):
        """解析视频号链接，返回 VideoResource 列表"""
        self._ensure_running()
        share_url = self.normalize_share_url(url)
        token, eid = self._feed_params_from_url(url)
        if not (token and eid):
            yuanbao_data = self._resolve_via_yuanbao(share_url)
            token, eid = self._playable_params(yuanbao_data)
        else:
            self._log("链接自带播放凭据，直接向视频号官方接口取流…", "INFO")

        self._ensure_running()
        feed = self._fetch_feed(token, eid)
        resources = self._build_resources(feed, share_url)
        if not resources:
            raise WeChatChannelsError(
                "视频号接口没有返回可下载的视频地址。该内容可能仅限微信内播放、已删除或需要付费。")
        return resources

    # ---------- 元宝登录 & 解析 ----------
    def _resolve_via_yuanbao(self, share_url):
        if not PLAYWRIGHT_AVAILABLE:
            raise WeChatChannelsError("缺少 Playwright，无法登录腾讯元宝。请执行: pip install playwright && playwright install chromium")

        self._log("启动腾讯元宝解析窗口（首次使用需登录腾讯元宝）…", "INFO")
        playwright = None
        context = None
        try:
            playwright = sync_playwright().start()
            launch_kwargs = dict(
                user_data_dir=self._profile_dir(),
                headless=False,
                viewport={'width': 520, 'height': 800},
                locale='zh-CN',
                user_agent=WECHAT_CHANNELS_UA,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--no-sandbox',
                    '--disable-gpu',
                    '--no-first-run',
                    '--window-size=540,860',
                    '--window-position=80,60',
                ],
                timeout=120000,
            )
            exe = self._browser_executable()
            if exe:
                launch_kwargs['executable_path'] = exe
            try:
                context = playwright.chromium.launch_persistent_context(**launch_kwargs)
            except Exception as e:
                raise WeChatChannelsError(
                    f"浏览器启动失败：{e}。\n"
                    "如果提示浏览器未安装，请执行: playwright install chromium") from e

            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(WECHAT_CHANNELS_HOME, wait_until='domcontentloaded', timeout=60000)
            except Exception as e:
                raise WeChatChannelsError(f"打开腾讯元宝失败，请检查网络：{e}") from e

            # 等待前端 $webApi 就绪（页面未登录也能加载）
            ready = False
            deadline = time.time() + 30
            while time.time() < deadline:
                self._ensure_running()
                try:
                    ready = bool(page.evaluate(
                        "() => !!(window.$webApi && window.$webApi.getYbCommonHeaders "
                        "&& window.$webApi.setContextualRequestHeaders)"))
                except Exception:
                    ready = False
                if ready:
                    break
                time.sleep(0.5)
            if not ready:
                raise WeChatChannelsError("腾讯元宝页面加载异常（$webApi 未就绪），请稍后重试。")

            self._wait_login(page)
            self._ensure_running()

            self._log("正在调用腾讯元宝解析视频号链接…", "INFO")
            result = page.evaluate(self.PARSE_JS, {
                'shareUrl': share_url,
                'agentId': WECHAT_CHANNELS_AGENT_ID,
            }) or {}
            status = int(result.get('status') or 0)
            code = result.get('code') or ''
            body = result.get('body') or ''
            if status == 401 or code == 'YUANBAO_LOGIN_REQUIRED':
                raise WeChatChannelsLoginRequired("腾讯元宝登录状态已失效，请重新扫码。")
            if status != 200:
                raise WeChatChannelsError(f"腾讯元宝解析失败（HTTP {status or '未知'}）：{body[:200]}")

            try:
                data = json.loads(body)
            except Exception as e:
                raise WeChatChannelsError(f"腾讯元宝返回格式异常：{body[:200]}") from e
            if not isinstance(data, dict) or not isinstance(data.get('data'), dict):
                raise WeChatChannelsError(f"腾讯元宝没有返回视频号解析数据：{body[:200]}")
            if not data['data'].get('playable_url'):
                message = ''
                try:
                    message = (data.get('data', {}).get('errMsg') or {}).get('title') or ''
                except Exception:
                    pass
                raise WeChatChannelsError(message or "腾讯元宝未能解析该视频（内容可能已删除或受限）。")
            return data
        finally:
            try:
                if context is not None:
                    context.close()
            except Exception:
                pass
            try:
                if playwright is not None:
                    playwright.stop()
            except Exception:
                pass

    def _wait_login(self, page):
        state = {}
        try:
            state = page.evaluate(self.LOGIN_CHECK_JS) or {}
        except Exception:
            state = {}
        if int(state.get('status') or 0) == 200:
            self._log("腾讯元宝已登录，开始解析…", "INFO")
            return

        self._log("首次使用需要登录腾讯元宝：请在弹出的浏览器窗口中用 QQ 或手机号登录。", "WARNING")
        self._log("登录只需一次，之后会自动复用；登录完成后请不要关闭窗口。", "INFO")
        # 自动点开登录弹窗，省得用户找不到入口（微信扫码在普通网页端不可用，
        # 这里只打开弹窗，由用户自行选择 QQ 登录或手机号验证码登录）
        try:
            page.get_by_text("登录", exact=True).last.click(timeout=3000, force=True)
        except Exception:
            pass
        deadline = time.time() + max(30, int(self.login_timeout))
        last_hint = time.time()
        while time.time() < deadline:
            self._ensure_running()
            try:
                if page.is_closed():
                    raise WeChatChannelsLoginRequired("登录窗口已关闭，未能完成腾讯元宝登录。")
                state = page.evaluate(self.LOGIN_CHECK_JS) or {}
                if int(state.get('status') or 0) == 200:
                    self._log("腾讯元宝登录成功，开始解析…", "INFO")
                    return
            except WeChatChannelsLoginRequired:
                raise
            except Exception:
                pass
            if time.time() - last_hint >= 20:
                last_hint = time.time()
                self._log("仍在等待扫码登录腾讯元宝…", "INFO")
            time.sleep(1.0)
        raise WeChatChannelsLoginRequired(
            "等待腾讯元宝登录超时（4 分钟）。请重新点击「开始」，然后在弹出的窗口中登录（QQ 或手机号）。")

    # ---------- 视频号官方 feed 接口 ----------
    def _playable_params(self, data):
        playable_url = str((data.get('data') or {}).get('playable_url') or '')
        try:
            parsed = urlparse(playable_url)
        except Exception:
            parsed = None
        if (parsed is None or parsed.scheme != 'https' or
                (parsed.hostname or '').lower() != 'channels.weixin.qq.com' or
                not parsed.path.startswith('/finder-preview/pages/feed')):
            raise WeChatChannelsError("腾讯元宝返回的播放入口不可信，已中止。")
        q = self._extract_query(playable_url)
        token = (q.get('token') or '').strip()
        eid = (q.get('eid') or '').strip()
        if not token or not eid:
            raise WeChatChannelsError("腾讯元宝返回的播放入口缺少 token/eid，解析接口可能已变化。")
        return token, eid

    def _fetch_feed(self, token, eid):
        if not REQUESTS_AVAILABLE:
            raise WeChatChannelsError("缺少 requests，无法请求视频号官方接口。请执行: pip install requests")
        rid = f"{int(time.time()):x}-{os.urandom(4).hex()}"
        request_url = WECHAT_CHANNELS_FEED_API + '?' + urlencode({
            '_rid': rid, '_pageUrl': WECHAT_CHANNELS_FEED_PAGE})
        referer = WECHAT_CHANNELS_FEED_PAGE + '?' + urlencode({
            'entry_card_type': '48',
            'comment_scene': '39',
            'appid': '0',
            'token': token,
            'entry_scene': '0',
            'eid': eid,
        })
        headers = {
            'Accept': 'application/json, text/plain, */*',
            'Content-Type': 'application/json; charset=utf-8',
            'Origin': 'https://channels.weixin.qq.com',
            'Referer': referer,
            'User-Agent': WECHAT_CHANNELS_UA,
        }
        body = {'baseReq': {'generalToken': token}, 'exportId': eid}
        self._log("正在向视频号官方接口获取视频地址…", "INFO")
        try:
            resp = requests.post(request_url, headers=headers, json=body, timeout=self.timeout)
        except Exception as e:
            raise WeChatChannelsError(f"连接视频号官方接口失败：{e}") from e
        if resp.status_code >= 400:
            raise WeChatChannelsError(
                f"视频号官方接口返回 HTTP {resp.status_code}，登录凭据可能已过期，请重试。")
        try:
            data = resp.json()
        except Exception as e:
            raise WeChatChannelsError("视频号官方接口返回了无效 JSON。") from e
        if not isinstance(data, dict) or data.get('errCode') not in (0, '0'):
            err = data.get('errMsg') if isinstance(data, dict) else ''
            raise WeChatChannelsError(f"视频号官方接口解析失败：{err or '未知错误'}")
        feed = (data.get('data') or {}).get('feedInfo')
        if not isinstance(feed, dict):
            raise WeChatChannelsError("视频号官方接口没有返回 feedInfo，解析可能已变化。")
        return data

    def _probe_media_size(self, url, kind=''):
        """HEAD 探测媒体大小（失败不影响解析）"""
        if not REQUESTS_AVAILABLE:
            return 0
        try:
            resp = requests.head(
                url,
                headers={
                    'User-Agent': WECHAT_CHANNELS_UA,
                    'Referer': 'https://channels.weixin.qq.com/',
                },
                timeout=6,
                allow_redirects=True)
            if resp.status_code < 400:
                return int(resp.headers.get('content-length') or 0)
        except Exception:
            pass
        return 0

    def _build_resources(self, feed_data, share_url):
        data = feed_data.get('data') or {}
        feed = data.get('feedInfo') or {}
        author = ''
        try:
            author = ((data.get('authorInfo') or {}).get('nickname') or '').strip()
        except Exception:
            author = ''
        description = str(feed.get('description') or '').strip()
        title = description.splitlines()[0].strip() if description else '微信视频号视频'
        if len(title) > 80:
            title = title[:80] + '…'
        page_title = description or (f"视频号 - {author}" if author else '微信视频号视频')

        candidates = []
        for kind, key, label, base_score in (
                ('h264', 'h264VideoInfo', 'H.264', 97),
                ('h265', 'h265VideoInfo', 'H.265', 93),
                ('default', 'videoUrl', '视频', 90)):
            info = feed.get(key)
            if isinstance(info, str) and info.startswith('http'):
                info = {'videoUrl': info}
            if not isinstance(info, dict):
                continue
            media_url = info.get('videoUrl') or info.get('url') or ''
            if not media_url:
                continue
            width = info.get('width') or info.get('videoWidth') or 0
            height = info.get('height') or info.get('videoHeight') or 0
            size = info.get('fileSize') or info.get('videoSize') or info.get('size') or 0
            try:
                width, height, size = int(width or 0), int(height or 0), int(size or 0)
            except Exception:
                width, height, size = 0, 0, 0
            if not size:
                size = self._probe_media_size(media_url, kind)
            resolution = f"{width}x{height}" if width and height else ''
            score = base_score
            if height:
                score += min(10, max(0, height // 108))
            note = f"微信视频号 · {label}"
            if author:
                note += f" · {author}"
            if kind == 'h265':
                note += "（H.265 兼容性较差）"
            candidates.append(VideoResource(
                url=media_url,
                resource_type='mp4',
                title=f"视频号 {label}",
                resolution=resolution,
                bandwidth=0,
                size=size,
                headers={
                    'User-Agent': WECHAT_CHANNELS_UA,
                    'Referer': 'https://channels.weixin.qq.com/',
                    'Origin': 'https://channels.weixin.qq.com',
                },
                source='wechat',
                quality_score=score,
                note=note,
                page_url=share_url,
                page_title=page_title,
            ))

        # 去重（同一 URL 只保留分数最高的一条）
        unique = {}
        for res in candidates:
            old = unique.get(res.url)
            if old is None or res.quality_score > old.quality_score:
                unique[res.url] = res
        result = sorted(unique.values(), key=lambda r: -r.quality_score)
        if result:
            self._log(f"视频号解析成功：{title}", "INFO")
            if author:
                self._log(f"作者：{author}", "INFO")
            for res in result:
                res_str = f"  · {res.note}"
                if res.resolution:
                    res_str += f" {res.resolution}"
                if res.size:
                    res_str += f" {res.size/1024/1024:.1f}MB"
                self._log(res_str, "INFO")
        return result


# ===================== Playwright 浏览器监控（增强版） =====================
class BrowserMonitor:
    """增强版浏览器监控器 - 全能嗅探"""

    def __init__(self, sniffer: SmartSniffer, log_callback=None, update_callback=None):
        self.sniffer = sniffer
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.page_title = ""
        self.last_page_title = ""
        self.page_url = ""
        self.last_page_url = ""
        self.log_callback = log_callback
        self.update_callback = update_callback  # v4.1: 嗅探过程中实时刷新资源列表
        self.scan_dom = True
        self.cookies = []
        self.browser_ua = ""
        self._active = False  # 浏览器是否活跃（close 后置 False，防回调访问已销毁资源）
        self._stop_event = threading.Event()  # v4.1: 停止嗅探请求（由嗅探线程自己收尾）
        self._close_lock = threading.Lock()   # 防止嗅探线程与退出流程同时 close
        self._session_headless = None         # 当前会话的显示模式，用于判断能否复用
        # 新增：playinfo 重新读取标记
        self._playinfo_read_count = 0
        self._last_playinfo_str = ""
        # 新增：HAR 抓包数据记录
        self.captured_requests: List[dict] = []
        # 抖音详情接口会被页面反复调用，每次签名 URL 都不同（路径前缀就是签名），
        # 按 URL 去重无效，这里按「作品 + 清晰度 + 编码」记录当前在用的直链
        self._douyin_gears: Dict[tuple, str] = {}
        self._douyin_lock = threading.Lock()
        # 当前页面要下载的抖音作品 ID（用于过滤推荐列表等无关响应）
        self._douyin_target_id = ""

    def _log(self, message: str, level: str = "INFO"):
        if self.log_callback:
            self.log_callback(message, level)
        else:
            logger.info(message) if level == "INFO" else logger.error(message)

    def _detect_drm(self):
        """检测页面是否使用 DRM（EME API）"""
        try:
            info = self.page.evaluate(r"""() => {
                let enc = false;
                for (const v of document.querySelectorAll('video, audio')) {
                    if (v.encrypted || v.mediaKeys) enc = true;
                }
                return { enc: enc, eme: window.__emeCalled === true };
            }""") or {}
            # 只有视频元素真的加密才算 DRM；抖音这类站点会随手探测一次 EME 能力，
            # 按老逻辑直接报警会造成「⚠ ⚠ DRM」误报，还把整批资源标成受保护
            if isinstance(info, dict) and info.get('enc'):
                self._log("检测到 DRM 加密（EME），该页面视频可能受版权保护无法下载", "WARNING")
                with self.sniffer.lock:
                    for res in self.sniffer.captured_resources.values():
                        res.is_drm = True
            elif isinstance(info, dict) and info.get('eme'):
                self._log("页面调用了 EME 接口（多为能力探测，非加密内容）", "DEBUG")
        except Exception:
            pass

    def _detect_mpd_streams(self):
        """检测页面中的 MPD 清单并解析"""
        try:
            mpd_urls = self.page.evaluate(r"""() => {
                const urls = [];
                // 搜索 MPD 链接
                document.querySelectorAll('source[type*="dash"], [type*="mpd"]').forEach(el => {
                    if (el.src) urls.push(el.src);
                });
                // 搜索页面 HTML 中的 .mpd
                const html = document.documentElement.innerHTML;
                const re = /(https?:\/\/[^"'\s]+\.mpd[^"'\s]*)/gi;
                let m;
                while ((m = re.exec(html)) !== null) urls.push(m[1]);
                return [...new Set(urls)];
            }""")
            for mpd_url in mpd_urls:
                if not self._active:
                    break
                self._log(f"发现 MPD 清单: {mpd_url[:80]}", "INFO")
                # 下载并解析 MPD
                try:
                    resp = self.page.request.get(mpd_url, timeout=2500)
                    if resp.ok:
                        mpd_content = resp.text()
                        parser = MPDParser(mpd_url)
                        mpd_resources = parser.parse(mpd_content, mpd_url)
                        for r in mpd_resources:
                            r.headers = {
                                'Referer': self.page_url,
                                'Origin': urlparse(self.page_url).scheme + '://' + urlparse(self.page_url).netloc,
                                'User-Agent': self.browser_ua,
                            }
                            r.cookies = self.cookies
                            if self.sniffer.force_update_resource(r):
                                self._log(f"  MPD 解析: {r.resolution} {r.bandwidth//1000}kbps", "INFO")
                except Exception as e:
                    self._log(f"MPD 解析失败: {e}", "WARNING")
        except Exception:
            pass

    def _session_usable(self, headless: bool) -> bool:
        """同一线程、同一显示模式、连接未断的 Chromium 会话可以直接复用"""
        if not (self.playwright and self.browser and self._session_headless == headless):
            return False
        try:
            return bool(self.browser.is_connected())
        except Exception:
            return False

    def _ensure_browser(self, headless: bool) -> bool:
        """确保有可用的 Chromium 会话；已有则复用，没有才启动"""
        if self._session_usable(headless):
            return True
        # 会话不可用（首次 / 切换无头模式 / 已断开）：彻底清理后再启动
        self.close()
        try:
            self.playwright = sync_playwright().start()
        except Exception as e:
            self._log(f"Playwright 启动失败: {e}", "ERROR")
            self.close()
            return False

        browser_path = None
        if hasattr(sys, '_MEIPASS'):
            # 动态查找 chromium 版本目录，避免硬编码版本号
            ms_pw_dir = Path(sys._MEIPASS) / "ms-playwright"
            if ms_pw_dir.exists():
                for item in ms_pw_dir.iterdir():
                    if item.is_dir() and item.name.startswith("chromium-"):
                        for sub in ("chrome-win", "chrome-win64"):
                            exe_path = item / sub / "chrome.exe"
                            if exe_path.exists():
                                browser_path = str(exe_path)
                                break
                        if browser_path:
                            break

        self.browser_ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36")
        try:
            self.browser = self.playwright.chromium.launch(
                headless=headless,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--no-sandbox',
                    '--disable-gpu',
                    '--disable-extensions',
                    '--disable-background-networking',
                    '--disable-sync',
                    '--no-first-run',
                    '--mute-audio',
                ],
                executable_path=browser_path,
                timeout=120000)
        except Exception as e:
            err_str = str(e).lower()
            if 'executable' in err_str or 'browser' in err_str:
                self._log("Chromium 浏览器内核未安装，请执行: playwright install chromium", "ERROR")
            else:
                self._log(f"浏览器启动失败: {e}", "ERROR")
            self.close()
            return False
        self._session_headless = headless
        return True

    def _release_page(self):
        """只关掉本次嗅探的页面与上下文，保留 Chromium 会话给下次复用"""
        with self._close_lock:
            self._active = False
            try:
                if self.page:
                    self.page.close()
            except Exception:
                pass
            try:
                if self.context:
                    self.context.close()
            except Exception:
                pass
            self.page = None
            self.context = None
            self.cookies = []
            self._playinfo_read_count = 0
            self._last_playinfo_str = ""

    def start(self, url: str, headless: bool = True) -> bool:
        """启动浏览器并打开页面"""
        self.captured_requests = []  # 清空上次抓包数据
        with self._douyin_lock:
            self._douyin_gears.clear()  # 新页面重新记录抖音各档位当前直链
        self._douyin_target_id = ""
        self._stop_event.clear()
        self.last_page_title = ""
        self.last_page_url = ""
        if not PLAYWRIGHT_AVAILABLE:
            self._log("Playwright 未安装，无法使用浏览器嗅探功能。请执行: pip install playwright", "ERROR")
            return False

        # 复用同一线程里已启动的 Chromium 会话：启动约 0.3 秒、关闭要 2.3 秒，
        # 每次嗅探都重开等于白扔 2.5 秒以上，所以同模式会话直接复用。
        if not self._ensure_browser(headless):
            return False
        # 可见模式下可能故意留着上一次的页面，重新嗅探前先收掉，避免上下文越堆越多
        if self.page or self.context:
            self._release_page()

        try:

            self._active = True
            if self._stop_event.is_set():
                self.close()
                return True

            self.context = self.browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent=self.browser_ua
            )

            self.page = self.context.new_page()
            self.page.on('request', self._on_request)
            self.page.on('response', self._on_response)
            self.page.on('framenavigated', self._on_navigated)

            # v4.0: 注入 blob/MSE 检测（在页面脚本执行前）
            try:
                self.page.add_init_script("""
                    if (!window.__blobHooked) {
                        window.__blobHooked = true;
                        if (navigator.requestMediaKeySystemAccess) {
                            const _eme = navigator.requestMediaKeySystemAccess.bind(navigator);
                            navigator.requestMediaKeySystemAccess = function() {
                                window.__emeCalled = true;
                                return _eme.apply(this, arguments);
                            };
                        }
                        const _createURL = URL.createObjectURL;
                        URL.createObjectURL = function(obj) {
                            const url = _createURL.call(this, obj);
                            try {
                                if (obj instanceof MediaSource) {
                                    console.log('[VDL] MSE blob URL:', url);
                                } else if (obj instanceof Blob) {
                                    console.log('[VDL] Blob URL:', url, obj.type, obj.size);
                                }
                            } catch(e) {}
                            return url;
                        };
                        if (window.MediaSource) {
                            const _addSB = MediaSource.prototype.addSourceBuffer;
                            MediaSource.prototype.addSourceBuffer = function(mime) {
                                console.log('[VDL] MSE SourceBuffer:', mime);
                                return _addSB.apply(this, arguments);
                            };
                        }
                    }
                """)
            except Exception:
                pass

            is_douyin_url = 'douyin.com' in url or 'iesdouyin.com' in url
            try:
                if is_douyin_url:
                    # 抖音首屏 domcontentloaded 要 2~3 秒，而我们只关心网络请求：
                    # commit 之后立刻进嗅探循环，正片请求照样能被 request 事件抓到
                    self.page.goto(url, wait_until='commit', timeout=30000)
                    self._wait_video_element(1.2)
                else:
                    self.page.goto(url, wait_until='domcontentloaded', timeout=30000)
            except Exception as e:
                self._log(f"页面加载失败: {e}", "ERROR")
                self.close()
                return False

            self.page_url = url
            self.last_page_url = url
            # 记录要下载的抖音作品 ID（短链会跳转，以浏览器实际停留地址为准），
            # 供 _collect_douyin_awemes 过滤掉推荐列表里的无关作品
            try:
                final_url = self.page.url or url
            except Exception:
                final_url = url
            aweme_match = re.search(r'/(?:video|note)/(\d{6,})', final_url)
            self._douyin_target_id = (self._extract_aweme_id(final_url)
                                      or self._extract_aweme_id(url))

            # v4.2: 若用户在页面加载期间点了停止，直接收尾
            if self._stop_event.is_set():
                self.close()
                return True

            # v4.2: 不再死等 networkidle（常见站点会持续挂起请求，白等 3 秒）
            try:
                self.page.wait_for_load_state('load', timeout=800)
            except Exception:
                pass

            try:
                self.page_title = self.page.title() or ""
                self.last_page_title = self.page_title
            except Exception:
                self.page_title = ""

            # 抖音首页 DOM 有上万节点，扫描一次要 0.7 秒且基本只能拿到 blob: 占位地址，
            # 网络嗅探已经能拿到档位，所以先跳过，抓空了再补扫（见下方）
            if self.scan_dom and not self._is_douyin():
                self._scan_dom_videos()

            self._try_click_play()

            # 等待视频资源加载（智能轮询机制）
            self._wait_for_video_resources()
            if self.scan_dom and self._is_douyin() and not self.sniffer.get_sorted_resources():
                self._scan_dom_videos()
            if self._stop_event.is_set():
                self._log("嗅探已停止", "WARNING")
                return True

            # 页面标题可能由播放器/SPA 延迟写入，收尾前再取一次
            try:
                final_title = self.page.title()
                if final_title:
                    self.page_title = final_title
                    self.last_page_title = final_title
            except Exception:
                pass

            # 导出浏览器 Cookie（HLS 合并需要带 Cookie 请求清单）
            try:
                self.cookies = self.context.cookies()
            except Exception:
                pass

            # 短链跳转可能晚于初始解析（v.douyin.com/xxx 落到 /video/<id> 有好几秒），
            # 收尾时用最终地址再确认一次目标作品
            if self._is_douyin() and not self._douyin_target_id:
                try:
                    self._douyin_target_id = self._extract_aweme_id(self.page.url or '')
                except Exception:
                    pass

            # v4.3: 抖音音视频分离 → 配对后用 FFmpeg 混流，避免下载到无声视频
            self._pair_douyin_tracks()

            # 抖音：接口响应晚于嗅探窗口时主动补一次，避免偶发解析不到资源
            self._fallback_douyin_detail()

            # 抖音：已经拿到目标作品的档位，就把归属不明的网络 mp4 丢掉，防止下错片子
            if self._is_douyin():
                self._prune_douyin_strays()

            # v4.1: 合并重复 HLS 清单 / 折叠 TS 分片
            self._collapse_hls_resources()

            # 抖音的档位常常由详情接口补齐，等待窗口结束时列表还是空的；
            # 统一在这里播报一次最终结果，界面日志才不会停在半路
            final_count = len(self.sniffer.get_sorted_resources())
            if final_count and not self._stop_event.is_set():
                self._log(f"嗅探完成，捕获 {final_count} 个视频", "INFO")

            # 将 Cookie 附加到所有已捕获资源
            if self.cookies:
                with self.sniffer.lock:
                    for res in self.sniffer.captured_resources.values():
                        if not res.cookies:
                            res.cookies = self.cookies

            # 读取 window.__playinfo__（仅 B 站需要，其他站点直接跳过，避免无谓等待）
            self._extract_bilibili_playinfo()

            # v4.0: DRM 检测 + MPD 解析
            self._detect_drm()
            self._detect_mpd_streams()

            # 注意：这里不再直接提示「未捕获」。是否真的没有资源由上层统一判断，
            # 避免 yt-dlp 已经解析成功、浏览器没抓到时出现误导性 WARNING。
            return True

        except Exception as e:
            self._log(f"嗅探过程异常: {e}", "ERROR")
            self._log(traceback.format_exc(), "ERROR")
            return False
        finally:
            if headless or self._stop_event.is_set():
                # 不再固定等待 3 秒：短暂让 pending 回调落库即可
                old_count = len(self.sniffer.captured_resources)
                for _ in range(2):
                    time.sleep(0.1)
                    new_count = len(self.sniffer.captured_resources)
                    if new_count == old_count:
                        break
                    old_count = new_count
                # 只收本次的页面，Chromium 留着给下一次嗅探复用（关一次要 2.3 秒）
                self._release_page()

    def _wait_for_video_resources(self):
        """点击播放后等待视频资源加载（v4.3：先等真正的视频，不再抓到占位动画就收尾）"""
        min_wait = 1.0
        idle_timeout = 1.0  # 连续无新资源则结束
        check_interval = 0.12
        page_url = getattr(self, 'page_url', '') or ''
        # 抖音等短视频站点首帧较慢（占位动画之后才请求正片），多等一会儿。
        # 但抖音的清晰度档位是详情接口一次性返回的，拿到后不必再干等 1 秒静默。
        is_douyin_like = any(h in page_url for h in ('douyin.com', 'iesdouyin.com'))
        if is_douyin_like:
            min_wait = 0.9
            idle_timeout = 0.45
        max_wait = 12.0 if is_douyin_like else 8.0

        self._log("正在捕获视频资源...", "INFO")
        start_time = time.time()
        last_count = len(self.sniffer.captured_resources)
        last_new_time = start_time
        last_playinfo_check = start_time
        last_ui_update = start_time

        while True:
            elapsed = time.time() - start_time
            if elapsed > max_wait:
                break
            if self._stop_event.is_set():
                break

            current_count = len(self.sniffer.captured_resources)
            if current_count > last_count:
                last_count = current_count
                last_new_time = time.time()
                # 边嗅探边刷新列表，间隔 0.7 秒，尽快让用户看到结果
                if self.update_callback and time.time() - last_ui_update > 0.7:
                    last_ui_update = time.time()
                    try:
                        self.update_callback()
                    except Exception:
                        pass

            # 仅 B 站需要每 2 秒检查 playinfo 是否变化（清晰度切换）
            if self._is_bilibili() and time.time() - last_playinfo_check > 2.0:
                last_playinfo_check = time.time()
                self._check_playinfo_change()

            # 抖音精选/弹窗页（/jingxuan?modal_id=…）往往不在页面直连 mp4，
            # 4 秒还什么都没抓到就直接收尾，交给详情接口兜底，别干等满 12 秒。
            # 但短链还没跳成 /video/<id>、判断不出是哪个作品时不能提前收，否则拿不到 ID。
            if is_douyin_like and elapsed > 4.0 and not self.sniffer.get_sorted_resources():
                target_hint = self._douyin_target_id
                if not target_hint:
                    try:
                        target_hint = self._extract_aweme_id(self.page.url or '')
                    except Exception:
                        target_hint = ''
                if target_hint:
                    break

            # 智能终止：必须已经拿到真正的视频资源（占位动画已在 add_resource 过滤），
            # 且视频确实在播放，或资源已稳定一段时间，才收尾。
            if elapsed > min_wait and (time.time() - last_new_time) > idle_timeout:
                has_content = bool(self.sniffer.get_sorted_resources())
                try:
                    is_playing = self.page.evaluate(
                        "() => { const v = document.querySelector('video'); "
                        "return v ? (!v.paused && v.readyState >= 2 && "
                        "isFinite(v.duration) && v.duration > 1) : false; }"
                    )
                except Exception:
                    is_playing = False

                if has_content and (is_playing or elapsed > 1.8):
                    break

            time.sleep(check_interval)

        # 最终刷新一次，确保列表与资源数一致
        if self.update_callback:
            try:
                self.update_callback()
            except Exception:
                pass
        valid = self.sniffer.get_sorted_resources()
        if valid:
            self._log(f"嗅探完成，捕获 {len(valid)} 个视频", "DEBUG")

    def _is_bilibili(self) -> bool:
        """当前页面是否 B 站（playinfo 仅 B 站使用）"""
        host = urlparse(self.page_url or '').netloc.lower()
        return 'bilibili.com' in host or 'b23.tv' in host

    def _is_douyin(self) -> bool:
        """当前页面是否抖音（DOM 巨大、档位接口一次给全，等待策略要区别对待）"""
        host = urlparse(self.page_url or '').netloc.lower()
        return 'douyin.com' in host or 'iesdouyin.com' in host

    def _wait_video_element(self, timeout: float = 1.5):
        """等页面落到作品页并挂上 <video>，超时也继续（不阻塞嗅探）"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._stop_event.is_set():
                return
            try:
                page_url = self.page.url or ''
                # 短链 v.douyin.com/xxx 要等它跳成 /video/<id>，否则拿不到作品 ID
                if re.search(r'/(?:video|note)/(\d{6,})', page_url):
                    return
                if self.page.evaluate("() => !!document.querySelector('video')"):
                    return
            except Exception:
                return
            time.sleep(0.12)

    def _pair_douyin_tracks(self):
        """抖音 DASH 音视频分离：把音轨配对到视频资源，下载时用 FFmpeg 混流

        抖音网页的 video 标签走 MSE，音轨和视频轨是两条独立请求，且都标记为
        video/mp4。只下载视频轨会得到无声视频，因此这里按同一请求会话
        （查询参数 l）配对，并把音轨从资源列表隐藏。
        """
        try:
            with self.sniffer.lock:
                items = list(self.sniffer.captured_resources.values())
        except Exception:
            return
        audios = [r for r in items if getattr(r, 'media_role', '') == 'audio']
        videos = [r for r in items if getattr(r, 'media_role', '') == 'video']
        if not audios or not videos:
            return

        def _session_id(url: str) -> str:
            try:
                return dict(parse_qsl(urlparse(url).query)).get('l', '')
            except Exception:
                return ''

        used = set()
        for video in videos:
            if video.companion_url:
                continue
            sid = _session_id(video.url)
            match = next((a for a in audios if sid and _session_id(a.url) == sid), None)
            if match is None and len(audios) == 1 and len(videos) == 1:
                match = audios[0]
            if match is None:
                continue
            video.companion_url = match.url
            video.companion_init_url = match.init_url
            video.resource_type = 'dash'
            video.note = (video.note + ' · ' if video.note else '') + '抖音音视频已配对（下载时自动混流）'
            used.add(match.url)
        if used:
            self.sniffer.remove_urls(list(used), suppress=True)
            self._log(f"抖音音视频已配对 {len(used)} 组，下载时将自动混流", "INFO")

    # ==================== v4.5 抖音清晰度补齐 ====================
    # 抖音网页播放器走 MSE 自适应码率，只会请求当前带宽能承受的一档（通常 720P），
    # 所以纯靠嗅探拿不到最高清。作品详情接口的响应里带完整 bit_rate 列表
    # （含 1080P / 2K 等档位），解析出来即可把高清直链补进资源列表。
    DOUYIN_API_PATHS = (
        '/aweme/v1/web/aweme/detail/',
        '/aweme/v1/web/aweme/post/',
        '/aweme/v1/web/aweme/favorite/',
        '/aweme/v1/web/mix/aweme/',
        '/aweme/v1/web/search/item/',
        '/aweme/v1/web/general/search/single/',
        '/web/api/v2/aweme/iteminfo/',
    )
    # 注意：不要把 /aweme/v1/web/aweme/related/ 加进来。
    # 那是「相关推荐」列表，返回的全是别的作品，混进资源列表会让用户下错视频。
    DOUYIN_MOBILE_UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
                        'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 '
                        'Mobile/15E148 Safari/604.1')

    @classmethod
    def _is_douyin_api_url(cls, url: str) -> bool:
        """是否为抖音作品详情类接口（响应体含完整清晰度列表）"""
        try:
            parsed = urlparse(url)
        except Exception:
            return False
        if 'douyin.com' not in (parsed.netloc or '').lower():
            return False
        path = parsed.path or ''
        return any(p in path for p in cls.DOUYIN_API_PATHS)

    @staticmethod
    def _extract_aweme_id(url: str) -> str:
        """抖音作品 ID：优先路径 /video|note/<id>，其次 modal_id / aweme_id / item_id 等参数

        /jingxuan?modal_id=xxx 这种「精选流里弹窗播放」的地址，路径里没有作品 ID，
        取不到就会被当成"不过滤"，把信息流前后几个视频的档位一起塞进列表。
        """
        if not url:
            return ""
        m = re.search(r'/(?:video|note)/(\d{6,})', url)
        if m:
            return m.group(1)
        try:
            for key, value in parse_qsl(urlparse(url).query, keep_blank_values=False):
                if (key in ('modal_id', 'aweme_id', 'item_id', 'vid', 'group_id')
                        and value.isdigit() and len(value) >= 6):
                    return value
        except Exception:
            pass
        return ""

    @staticmethod
    def _url_query_value(url: str, key: str) -> str:
        """取 URL 查询参数（解码后），取不到返回空串"""
        try:
            for k, v in parse_qsl(urlparse(url).query, keep_blank_values=False):
                if k == key:
                    return v
        except Exception:
            pass
        return ''

    def _douyin_owned(self, url: str, res_type: str) -> bool:
        """抖音视频流归属校验：False 表示这条不该收（别的作品，或归属判断不了）"""
        target = self._douyin_target_id
        if not target or res_type not in ('mp4', 'webm', 'm4a', 'mp3'):
            return True
        vid = self._url_query_value(url, '__vid')
        if vid:
            return vid == target
        if res_type != 'mp4':
            return True
        # 没有 __vid 就判断不出归属：只有详情接口已经给出权威档位时才敢丢，
        # 否则某些页面的正片就是这种无标记直链，丢了会什么都下不了
        with self.sniffer.lock:
            return not any(getattr(r, 'source', '') == 'douyin_api'
                           for r in self.sniffer.captured_resources.values())

    def _prune_douyin_strays(self):
        """抖音：详情接口已给出目标作品的档位时，清掉归属不明的网络流

        页面上有些 mp4 直链不带 __vid，无法判断属于哪个作品；保留它们就等于
        把信息流里别的视频留在列表里（之前「下成电影解说」就是这么来的）。
        """
        target = self._douyin_target_id
        if not target:
            return
        with self.sniffer.lock:
            has_api = any(getattr(r, 'source', '') == 'douyin_api'
                          for r in self.sniffer.captured_resources.values())
            if not has_api:
                return
            drop = [r.url for r in self.sniffer.captured_resources.values()
                    if getattr(r, 'source', '') == 'network'
                    and r.resource_type == 'mp4'
                    and self._url_query_value(r.url, '__vid') != target]
        if drop:
            removed = self.sniffer.remove_urls(drop)
            if removed:
                self._log(f"丢弃 {removed} 条归属不明的视频流", "DEBUG")

    def _handle_douyin_api_response(self, response):
        """解析抖音接口 JSON 并补充清晰度档位（任何异常都静默跳过，不影响原嗅探流程）"""
        try:
            ctype = (response.headers.get('content-type', '') or '').lower()
            if 'json' not in ctype:
                return
            data = response.json()
        except Exception:
            return
        if data:
            self._collect_douyin_awemes(data)

    def _collect_douyin_awemes(self, data):
        """递归查找响应中的作品对象（aweme_id + video），逐个补充清晰度档位

        只保留当前页面这个作品：详情页同时会拉「相关推荐」「搜索联想」等接口，
        那些响应里混着大量别的视频，不加过滤会污染资源列表。
        """
        awemes: List[dict] = []
        self._walk_douyin_awemes(data, awemes)
        target = self._douyin_target_id
        for aweme in awemes:
            if target and str(aweme.get('aweme_id') or '') != target:
                continue
            self._add_douyin_aweme(aweme)

    @staticmethod
    def _walk_douyin_awemes(node, out: List[dict], depth: int = 0):
        """深度受限的递归遍历，兼容 detail（单作品）/ post（作品列表）等不同结构"""
        if depth > 6 or len(out) >= 60:
            return
        if isinstance(node, dict):
            if node.get('aweme_id') and isinstance(node.get('video'), dict):
                out.append(node)
            for value in node.values():
                BrowserMonitor._walk_douyin_awemes(value, out, depth + 1)
        elif isinstance(node, list):
            for value in node:
                BrowserMonitor._walk_douyin_awemes(value, out, depth + 1)

    def _add_douyin_aweme(self, aweme: dict):
        """把一个作品的各清晰度档位转成资源条目（接口返回的直链本身含音轨）"""
        video = aweme.get('video') or {}
        if not isinstance(video, dict):
            return

        desc = (aweme.get('desc') or '').strip()
        title = desc[:60] if desc else '抖音视频'

        candidates = []
        # 1) 首选 bit_rate 列表：每档都带独立直链与真实分辨率
        for item in (video.get('bit_rate') or []):
            if not isinstance(item, dict):
                continue
            play = item.get('play_addr') or {}
            if not isinstance(play, dict):
                continue
            urls = [u for u in (play.get('url_list') or []) if isinstance(u, str) and u]
            if not urls:
                continue
            candidates.append({
                'url': urls[0],
                'width': int(play.get('width') or item.get('width') or 0),
                'height': int(play.get('height') or item.get('height') or 0),
                'bandwidth': int(item.get('bit_rate') or 0),
                'size': int(play.get('data_size') or 0),
                'gear': str(item.get('gear_name') or ''),
                'codec': 'H.265' if item.get('is_h265') else 'H.264',
            })

        # 2) 兜底：bit_rate 缺失时取主播放地址 / 独立 H.264 / H.265 通道
        for key, codec in (('play_addr', 'H.264'),
                           ('play_addr_h264', 'H.264'),
                           ('play_addr_265', 'H.265')):
            play = video.get(key) or {}
            if not isinstance(play, dict):
                continue
            urls = [u for u in (play.get('url_list') or []) if isinstance(u, str) and u]
            if not urls:
                continue
            candidates.append({
                'url': urls[0],
                'width': int(play.get('width') or video.get('width') or 0),
                'height': int(play.get('height') or video.get('height') or 0),
                'bandwidth': 0,
                'size': int(play.get('data_size') or 0),
                'gear': key,
                'codec': codec,
            })

        if not candidates:
            return

        ua = getattr(self, 'browser_ua', '') or self.DOUYIN_MOBILE_UA

        # 同一次响应里常有多档同分辨率不同码率（normal_1080_0 / adapt_1080_1 等），
        # 只保留「分辨率 + 编码」相同里码率最高的一条，避免列表被塞满
        best: Dict[tuple, dict] = {}
        for cand in candidates:
            width, height = cand['width'], cand['height']
            # 抖音竖屏居多（1080×1920），清晰度按短边计，与「1080P」的通行叫法一致
            short_side = min(width, height) if width and height else (height or width)
            cand['short_side'] = short_side
            key = (short_side, cand['codec'])
            old = best.get(key)
            if old is None or cand['bandwidth'] > old['bandwidth']:
                best[key] = cand

        aweme_id = str(aweme.get('aweme_id') or '')
        added = 0
        for (short_side, codec), cand in best.items():
            width, height = cand['width'], cand['height']
            if short_side:
                resolution = f"{short_side}P"
                label = f"{width}×{height}" if width and height else resolution
            else:
                resolution = ''
                label = cand['gear'] or '原画'

            # 详情接口会被页面反复调用，每次下发的直链签名都不同（路径前缀即签名），
            # 按 URL 去重无效。这里按「作品 + 清晰度 + 编码」记住当前在用的那条，
            # 用最新签名替换旧条目：旧签名多半已过期，留着只会让用户下到 403。
            gear_key = (aweme_id, short_side, codec)
            with self._douyin_lock:
                prev_url = self._douyin_gears.get(gear_key)
                self._douyin_gears[gear_key] = cand['url']
            if prev_url == cand['url']:
                continue  # 同一条直链，无需重复登记

            res = VideoResource(
                url=cand['url'],
                resource_type='mp4',
                title=title,
                resolution=resolution,
                bandwidth=cand['bandwidth'],
                size=cand['size'],
                headers={
                    'User-Agent': ua,
                    'Referer': 'https://www.douyin.com/',
                    'Origin': 'https://www.douyin.com',
                },
                source='douyin_api',
                note=f"抖音 {label} · {codec} · 含音轨",
            )
            if not self.sniffer.force_update_resource(res):
                continue
            if prev_url:
                self.sniffer.remove_urls([prev_url])
            else:
                added += 1  # 只在首次出现该档位时计数，避免重复响应刷日志

        if added:
            self._log(f"抖音清晰度补齐：{added} 个档位", "DEBUG")

    # 在页面上下文里主动请求详情接口。该接口不校验 a-bogus，带上页面 Cookie 直接
    # fetch 就能拿到数据，用于「嗅探窗口内没等到接口响应」时的兜底。
    DOUYIN_DETAIL_JS = """
    async (id) => {
        const url = 'https://www.douyin.com/aweme/v1/web/aweme/detail/?device_platform=webapp'
            + '&aid=6383&channel=channel_pc_web&aweme_id=' + id
            + '&pc_client_type=1&version_code=170400&version_name=17.4.0'
            + '&cookie_enabled=true&platform=PC&browser_language=zh-CN'
            + '&browser_platform=Win32&browser_name=Chrome&browser_version=120.0.0.0';
        const ctrl = new AbortController();
        const timer = setTimeout(() => ctrl.abort(), 8000);
        try {
            const resp = await fetch(url, {credentials: 'include', signal: ctrl.signal});
            if (!resp.ok) return null;
            return await resp.json();
        } catch (e) {
            return null;
        } finally {
            clearTimeout(timer);
        }
    }
    """

    def _fallback_douyin_detail(self):
        """抖音兜底：嗅探期间没等到详情接口响应时，主动补一次请求

        抖音详情接口的响应常常晚于嗅探窗口（尤其是网络慢或首屏较慢时），
        只靠被动嗅探会偶发「解析不到任何资源」。这里用页面自身上下文补一次，
        代价只有一次几百毫秒的请求。
        """
        if not self._active or not self.page:
            return
        # 短链会跳转，以浏览器实际停留的地址为准
        try:
            current_url = self.page.url or self.page_url or ''
        except Exception:
            current_url = self.page_url or ''
        host = urlparse(current_url).netloc.lower()
        if 'douyin.com' not in host:
            return
        # 已经通过响应嗅探拿到档位就不用再补
        with self.sniffer.lock:
            if any(getattr(r, 'source', '') == 'douyin_api'
                   for r in self.sniffer.captured_resources.values()):
                return
        # /jingxuan?modal_id=xxx 这类地址路径里没有作品 ID，要连参数一起看
        aweme_id = self._extract_aweme_id(current_url) or (self._douyin_target_id or '')
        if not aweme_id:
            return
        try:
            data = self.page.evaluate(self.DOUYIN_DETAIL_JS, aweme_id)
        except Exception as e:
            self._log(f"抖音详情兜底请求失败：{e}", "WARNING")
            return
        if isinstance(data, dict) and data:
            self._collect_douyin_awemes(data)

    def _collapse_hls_resources(self):
        """v4.2: 合并同一视频的多份 HLS 清单，并清理 TS 分片噪声

        与 v4.1 的串行拉取不同，这里并行请求清单（最多 6 个 / 4 秒），
        只有存在多条 m3u8 时才做合并，避免单清单页面白等。
        """
        if not self._active or not self.page:
            return
        try:
            with self.sniffer.lock:
                m3u8_items = [r for r in self.sniffer.captured_resources.values()
                              if r.resource_type == 'm3u8']
            if len(m3u8_items) > 1:
                texts = self._fetch_playlist_texts(m3u8_items[:10])
                playlists = {}
                for r in m3u8_items:
                    text = texts.get(r.url, "")
                    if not text or '#EXTM3U' not in text:
                        continue
                    streams = M3U8Parser(r.url).parse(text, r.url)
                    variants = [s.url for s in streams if s.is_master and s.url]
                    best = max(streams, key=lambda s: s.bandwidth) if streams else None
                    media_urls = [s.url for s in streams if s.segments]
                    playlists[r.url] = {
                        'is_master': any(s.is_master for s in streams),
                        'variants': variants,
                        'best': best,
                        'signature': tuple(
                            SmartSniffer._normalize_url(u) for u in media_urls[:3]
                        ),
                        'subtitles': [s.subtitle_url for s in streams
                                      if s.media_type == 'SUBTITLES' and s.subtitle_url],
                    }

                # 1. 被 master 引用的子清单不再单独显示
                variant_norm = set()
                for info in playlists.values():
                    for v in info['variants']:
                        variant_norm.add(SmartSniffer._normalize_url(v))

                remove = set()
                for r in m3u8_items:
                    info = playlists.get(r.url)
                    if info and info['is_master']:
                        best = info['best']
                        if best:
                            r.bandwidth = best.bandwidth or r.bandwidth
                            r.resolution = best.resolution or r.resolution
                        if info['variants']:
                            name = unquote(urlparse(r.url).path).rsplit('/', 1)[-1] or '清单'
                            r.note = f"HLS 多码率（{len(info['variants'])} 档） · {name}"
                        if info['subtitles']:
                            r.subtitle_urls = info['subtitles']
                        r.quality_score = self.sniffer._calculate_quality_score(r)
                        continue
                    if SmartSniffer._normalize_url(r.url) in variant_norm:
                        remove.add(r.url)

                # 2. 分片序列完全相同的媒体清单视为同一视频（多次抓取/多 token）
                by_signature = {}
                for r in m3u8_items:
                    if r.url in remove:
                        continue
                    info = playlists.get(r.url)
                    if not info or info['is_master'] or not info['signature']:
                        continue
                    by_signature.setdefault(info['signature'], []).append(r)
                for group in by_signature.values():
                    if len(group) <= 1:
                        continue
                    keep = max(group, key=lambda x: (x.quality_score, x.bandwidth))
                    for r in group:
                        if r is not keep:
                            remove.add(r.url)

                if remove:
                    n = self.sniffer.remove_urls(remove, suppress=True)
                    self._log(f"已合并重复 HLS 清单 {n} 条", "INFO")

            # 3. 有 M3U8 时，同源 TS 分片由清单统一下载，从列表移除
            with self.sniffer.lock:
                hls_hosts = {urlparse(r.url).netloc for r in self.sniffer.captured_resources.values()
                             if r.resource_type == 'm3u8'}
                ts_urls = [r.url for r in self.sniffer.captured_resources.values()
                           if r.resource_type == 'ts'
                           and urlparse(r.url).netloc in hls_hosts]
            if ts_urls:
                n = self.sniffer.remove_urls(ts_urls, suppress=True)
                self._log(f"已忽略 {n} 个 TS 分片（随 M3U8 统一下载）", "INFO")
        except Exception as e:
            logger.debug(f"HLS 清单合并失败: {e}")

    def _fetch_playlist_texts(self, resources: List[VideoResource]) -> Dict[str, str]:
        """并行拉取多个 m3u8 清单（最多 6 并发，4 秒截止）"""
        if not REQUESTS_AVAILABLE or not resources:
            return {}
        cookie_str = '; '.join(
            f"{c.get('name')}={c.get('value')}" for c in (self.cookies or [])
            if c.get('name') and c.get('value')
        )

        def _fetch(r: VideoResource) -> Tuple[str, str]:
            try:
                headers = {k: v for k, v in (r.headers or {}).items() if v}
                headers.setdefault('User-Agent', self.browser_ua)
                headers.setdefault('Referer', self.page_url or r.url)
                if cookie_str:
                    headers.setdefault('Cookie', cookie_str)
                resp = requests.get(r.url, headers=headers, timeout=1.6, stream=True)
                if resp.status_code >= 400:
                    return r.url, ""
                data = resp.raw.read(2 * 1024 * 1024, decode_content=True)
                if not data:
                    return r.url, ""
                return r.url, data.decode('utf-8', errors='ignore')
            except Exception:
                return r.url, ""

        result: Dict[str, str] = {}
        executor = ThreadPoolExecutor(max_workers=min(6, len(resources)))
        try:
            futures = {executor.submit(_fetch, r): r.url for r in resources}
            for fut in as_completed(futures, timeout=2.0):
                try:
                    url, text = fut.result()
                except Exception:
                    continue
                if text:
                    result[url] = text
        except Exception:
            pass
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return result


    def _check_playinfo_change(self):
        """检查 B站 window.__playinfo__ 是否发生变化（清晰度切换）"""
        if not self._is_bilibili():
            return
        try:
            ready = self.page.evaluate(
                "() => { try { return window.__playinfo__ !== undefined && window.__playinfo__ !== null; } catch(e) { return false; } }"
            )
            if not ready:
                return
            playinfo_str = self.page.evaluate(
                "() => { try { return JSON.stringify(window.__playinfo__); } catch(e) { return null; } }"
            )
            if not playinfo_str:
                return
            if playinfo_str != self._last_playinfo_str:
                # 修复：首次获取到 playinfo 或发生变化时都应解析
                if self._last_playinfo_str:
                    self._log("检测到 playinfo 变化（可能切换了清晰度），重新解析...", "INFO")
                self._last_playinfo_str = playinfo_str
                self._extract_bilibili_playinfo()
        except Exception:
            pass

    def _on_navigated(self, frame):
        """页面导航回调 - 检测用户手动操作导致的页面跳转（可见模式防重复捕获）"""
        if not self._active:
            return
        try:
            if frame != self.page.main_frame:
                return
            current_url = frame.url
            if not current_url or current_url == 'about:blank':
                return
            old_url = getattr(self, 'page_url', '') or ''
            current_base = current_url.split('#')[0]
            old_base = old_url.split('#')[0]
            if not old_base or current_base == old_base:
                return
            try:
                old_domain = urlparse(old_url).netloc
                new_domain = urlparse(current_url).netloc
                if old_domain != new_domain:
                    self._log(f"检测到跨域导航: {old_domain} → {new_domain}", "DEBUG")
                    self._log("已自动清空旧页面资源，请重新嗅探当前页面", "DEBUG")
                    self.sniffer.clear()
                else:
                    self._log(f"检测到同域页面导航: {current_url[:80]}", "DEBUG")
                    self._log("旧资源仍保留，如需清除请重新嗅探", "DEBUG")
            except Exception:
                pass
            self.page_url = current_url
            self.last_page_url = current_url
        except Exception:
            pass

    def _on_request(self, request):
        """请求回调 - 捕获所有视频请求"""
        if not self._active:
            return
        url = request.url
        if not self._is_potential_video(url):
            return

        headers = request.headers
        # v4.1: 请求阶段就识别类型（原实现全部记为 other，导致 TS 分片绕过折叠逻辑）
        res_type = self.sniffer.detect_resource_type(url)
        if res_type == 'other':
            # 无扩展名的地址交给响应回调按 Content-Type 判断，避免把大量普通请求塞进列表
            return
        if res_type == 'ts':
            host = urlparse(url).netloc.lower()
            with self.sniffer.lock:
                if host in self.sniffer._ts_hosts:
                    return
        # 抖音信息流（/jingxuan?modal_id=…）会顺带把前后几条视频一起加载，
        # 直链里的 __vid 就是作品 ID，不是目标作品的流一律不收，避免下成别的片子
        if not self._douyin_owned(url, res_type):
            self._log("跳过非目标作品的视频流", "DEBUG")
            return
        res = VideoResource(
            url=url,
            resource_type=res_type,
            headers={
                'User-Agent': headers.get('user-agent', ''),
                'Referer': headers.get('referer', ''),
                'Origin': headers.get('origin', ''),
            },
            source='network'
        )
        self.sniffer.add_resource(res)

    def _on_response(self, response):
        """响应回调 - 确认资源类型和大小，同时记录 HAR 抓包数据"""
        if not self._active:
            return
        url = response.url
        # 记录 HAR 抓包数据（限制最大 500 条防内存膨胀）
        if len(self.captured_requests) < 500:
            try:
                req_entry = {
                    'url': url,
                    'method': response.request.method if hasattr(response, 'request') else 'GET',
                    'status': response.status,
                    'content_type': '',
                    'size': 0,
                    'timestamp': datetime.now().isoformat(),
                    'headers': {},
                    'response_headers': {},
                }
                try:
                    resp_headers = response.headers
                    req_entry['content_type'] = resp_headers.get('content-type', '')
                    req_entry['size'] = int(resp_headers.get('content-length', 0))
                    req_entry['response_headers'] = dict(resp_headers)
                    # 修复：填充请求头
                    if hasattr(response, 'request') and response.request is not None:
                        try:
                            req_entry['headers'] = dict(response.request.headers)
                        except Exception:
                            pass
                except Exception:
                    pass
                self.captured_requests.append(req_entry)
            except Exception:
                pass

        content_type = ''
        size = 0
        try:
            resp_headers = response.headers
            content_type = resp_headers.get('content-type', '')
            size = int(resp_headers.get('content-length', 0) or 0)
        except Exception:
            pass

        res_type = self.sniffer.detect_resource_type(url, content_type)

        # v4.5: 抖音详情接口（aweme/detail 等）的响应体带完整清晰度列表 bit_rate，
        # 而网页播放器只按当前带宽自适应请求其中一档（常见 720p），因此嗅探到的直链
        # 往往不是最高清。这里额外解析接口数据，把 1080P/2K 等档位补进资源列表。
        if self._is_douyin_api_url(url):
            self._handle_douyin_api_response(response)
            return

        # 防御：错误页/播放页常以 text/html 返回，不要当成视频资源
        if 'text/html' in content_type.lower() and res_type != 'm3u8':
            # 请求阶段可能已按扩展名（如 .mp4）记了伪资源，这里按响应类型剔除
            if url in self.sniffer.captured_resources:
                self.sniffer.remove_urls([url], suppress=True)
            return
        # 抖音等站点音视频分离：audio/mp4 纯音轨不要当成可下载视频，
        # 否则可能只下到一条无声/无画面的音轨。
        if content_type.lower().startswith('audio/') and res_type != 'm3u8':
            if url in self.sniffer.captured_resources:
                self.sniffer.remove_urls([url], suppress=True)
            return
        with self.sniffer.lock:
            resource = self.sniffer.captured_resources.get(url)
            if resource is not None:
                if size > 0:
                    resource.size = size
                # 已配好音轨的 DASH 资源不要被后续分片响应的 content-type 改回 mp4
                if res_type != 'other' and not resource.companion_url:
                    resource.resource_type = res_type
                resource.resolution = resource.resolution or self.sniffer.extract_resolution(url)
                resource.quality_score = self.sniffer._calculate_quality_score(resource)
                return

        # v4.1: 无扩展名地址在请求阶段被跳过，这里按响应 MIME 补录（仍受 TS 折叠与广告过滤约束）
        # DASH 的 m4s 分片同样不能单独成条，只接受 .mpd 清单本身
        if res_type == 'dash' and not re.search(r'\.mpd(?:[?#]|$)', url.lower()):
            return
        if res_type in ('m3u8', 'mp4', 'webm', 'flv', 'ts', 'dash'):
            # 抖音：响应阶段补录的流同样要过归属校验，否则别的作品的片子会从这里溜进来
            if not self._douyin_owned(url, res_type):
                return
            req_headers = {}
            try:
                req = response.request
                if req is not None:
                    req_headers = dict(req.headers)
            except Exception:
                pass
            res = VideoResource(
                url=url,
                resource_type=res_type,
                size=size,
                headers={
                    'User-Agent': req_headers.get('user-agent', ''),
                    'Referer': req_headers.get('referer', ''),
                    'Origin': req_headers.get('origin', ''),
                },
                source='network'
            )
            res.resolution = self.sniffer.extract_resolution(url)
            self.sniffer.add_resource(res)

    def _is_potential_video(self, url: str) -> bool:
        """判断URL是否可能是视频资源"""
        url_lower = url.lower()

        blacklist_domains = (
            'data.bilibili.com', 'metrics.bilibili.com',
            'trace.bilibili.com', 'tp.amemori.com',
            'google-analytics.com', 'googletagmanager.com',
            'googlesyndication.com', 'doubleclick.net',
            'scorecardresearch.com', 'hm.baidu.com',
            'hot.bilibili.com', 'cm.bilibili.com',
        )
        for domain in blacklist_domains:
            if domain in url_lower:
                return False

        parsed = urlparse(url)
        path = unquote(parsed.path)
        ext = os.path.splitext(path)[1].lower()

        # m4s 碎片不作为独立资源
        if ext == '.m4s':
            return False
        if ext in SmartSniffer.VIDEO_EXTENSIONS:
            return True

        # m4s 是 DASH 分片碎片，不能单独下载，只由 playinfo/MPD 解析器添加
        if re.search(r'\.m4s(?:\?|#|$)', url_lower):
            return False

        video_patterns = [
            r'\.(mp4|m3u8|webm|flv|mkv|mov|mpd)(\?|&|$)',
            r'(m3u8|mp4)\?',
            r'/(video|media|stream|vod|hls)(/|\?|$)',
        ]

        for pattern in video_patterns:
            if re.search(pattern, url_lower):
                return True

        return False

    def _scan_dom_videos(self):
        """扫描页面 DOM 中的视频/音频地址（v4.2: 单次 JS 批量取回，避免逐元素往返）"""
        try:
            items = self.page.evaluate("""() => {
                const out = [];
                const push = (u, kind, i) => {
                    if (!u) return;
                    try { u = new URL(u, document.baseURI).href; } catch (e) { return; }
                    if (/^https?:/i.test(u)) out.push({url: u, kind: kind, index: i});
                };
                document.querySelectorAll('video, audio').forEach((el, i) => {
                    push(el.currentSrc || el.src || el.getAttribute('data-src'), el.tagName.toLowerCase(), i);
                    el.querySelectorAll('source').forEach(s => {
                        push(s.src || s.getAttribute('src'), s.getAttribute('type') || 'source', i);
                    });
                });
                return out;
            }""") or []
            seen = set()
            for item in items:
                url = item.get('url')
                if not url or url in seen:
                    continue
                seen.add(url)
                self._add_dom_resource(url, item.get('kind') or 'dom', item.get('index') or 0)
        except Exception as e:
            self._log(f"DOM扫描出错：{e}", "WARNING")

    def _add_dom_resource(self, url: str, source_type: str, index: int):
        res_type = self.sniffer.detect_resource_type(url)
        resource = VideoResource(
            url=url,
            resource_type=res_type,
            source='dom',
            headers={
                'Referer': self.page_url,
                'Origin': urlparse(self.page_url).scheme + '://' + urlparse(self.page_url).netloc
            }
        )
        resource.resolution = self.sniffer.extract_resolution(url)
        self.sniffer.add_resource(resource)

    def _try_click_play(self):
        """尝试点击播放按钮触发视频加载（v4.2: 限制超时，避免卡在不可点击元素上）"""
        try:
            play_selectors = [
                '.vjs-big-play-button',
                'button[aria-label="Play"]',
                '.play-button',
                '.video-play-button',
                '.player-play',
                '#play-button',
                'video'
            ]
            for selector in play_selectors:
                try:
                    element = self.page.query_selector(selector)
                    if not element:
                        continue
                    element.click(timeout=1200, force=True)
                    break
                except Exception:
                    continue
        except Exception:
            pass
        # 部分站点需要直接调用 video.play()
        try:
            self.page.evaluate("""() => {
                const v = document.querySelector('video');
                if (v && v.paused) { v.muted = true; v.play().catch(() => {}); }
            }""")
        except Exception:
            pass

    def _fetch_bilibili_api(self):
        """当 __playinfo__ 不可用时通过 B站 API 获取"""
        try:
            page_url = self.page_url or ""
            bvid_match = re.search(r'(BV[0-9a-zA-Z]+)', page_url)
            if not bvid_match:
                return
            bvid = bvid_match.group(1)
            cid = self.page.evaluate("""() => {
                try {
                    const s = window.__INITIAL_STATE__;
                    if (s && s.videoData && s.videoData.cid) return s.videoData.cid;
                    if (s && s.epInfo && s.epInfo.cid) return s.epInfo.cid;
                    return null;
                } catch(e) { return null; }
            }""")
            headers = {'User-Agent': 'Mozilla/5.0', 'Referer': page_url}
            if self.cookies:
                headers['Cookie'] = '; '.join(
                    f"{ck['name']}={ck['value']}" for ck in self.cookies)
            if not cid:
                r = requests.get(
                    f'https://api.bilibili.com/x/web-interface/view?bvid={bvid}',
                    headers=headers, timeout=10)
                cid = r.json().get('data', {}).get('cid')
            if not cid:
                return
            r = requests.get(
                f'https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&fnval=4048&fourk=1',
                headers=headers, timeout=15)
            result = r.json()
            if result.get('code') != 0:
                self._log("B站需要登录或大会员才能获取高清流", "WARNING")
                return
            data = result.get('data', {})
            self._last_playinfo_str = json.dumps(data)
            self._playinfo_read_count += 1
            self._parse_playinfo_data(data)
        except Exception as e:
            logger.debug(f"B站API获取失败: {e}")

    def _extract_bilibili_playinfo(self):
        """读取 B站 window.__playinfo__，失败时回退 API（非 B 站直接跳过）"""
        if not self._is_bilibili():
            return
        try:
            ready = False
            for attempt in range(3):
                ready = self.page.evaluate(
                    "() => { try { return window.__playinfo__ !== undefined && window.__playinfo__ !== null; } catch(e) { return false; } }"
                )
                if ready:
                    break
                if attempt < 2:
                    time.sleep(0.4)

            if not ready:
                self._fetch_bilibili_api()
                return

            playinfo = self.page.evaluate(
                "() => { try { return JSON.stringify(window.__playinfo__); } catch(e) { return null; } }"
            )
            if not playinfo:
                return

            self._last_playinfo_str = playinfo
            self._playinfo_read_count += 1
            data = json.loads(playinfo)
            self._parse_playinfo_data(data)
        except Exception as e:
            logger.debug(f"playinfo 解析异常: {e}")

    def _parse_playinfo_data(self, data):
        """解析 playinfo JSON 数据并添加 DASH 资源"""
        try:
            if isinstance(data, str):
                data = json.loads(data)
            dash_data = data.get('data', {}).get('dash')
            if not dash_data:
                dash_data = data.get('dash')

            parsed = urlparse(self.page_url)
            origin = f"{parsed.scheme}://{parsed.netloc}"
            req_headers = {
                'Referer': self.page_url,
                'Origin': origin,
                'User-Agent': self.browser_ua,
            }

            if dash_data:
                video_tracks = dash_data.get('video', [])
                audio_tracks = dash_data.get('audio', [])

                best_video = None
                for v in video_tracks:
                    v_url = self._get_track_url(v)
                    if not v_url:
                        continue
                    if best_video is None or v.get('bandwidth', 0) > best_video.get('bandwidth', 0):
                        best_video = v

                best_audio = None
                for a in audio_tracks:
                    a_url = self._get_track_url(a)
                    if not a_url:
                        continue
                    if best_audio is None or a.get('bandwidth', 0) > best_audio.get('bandwidth', 0):
                        best_audio = a

                companion = self._get_track_url(best_audio) if best_audio else ""
                video_init = self._get_init_url(best_video) if best_video else ""
                audio_init = self._get_init_url(best_audio) if best_audio else ""

                if best_video:
                    v_url = self._get_track_url(best_video)
                    v_bw = best_video.get('bandwidth', 0)
                    v_codec = best_video.get('codecs', '')
                    v_w = best_video.get('width', 0)
                    v_h = best_video.get('height', 0)
                    v_res = f"{v_w}x{v_h}" if v_w and v_h else ""
                    v_title = f"DASH视频 {v_res} {v_codec}".strip()
                    resource = VideoResource(
                        url=v_url,
                        resource_type='dash',
                        title=v_title,
                        resolution=v_res,
                        bandwidth=v_bw,
                        headers=dict(req_headers),
                        source='playinfo',
                        companion_url=companion,
                        init_url=video_init,
                        companion_init_url=audio_init,
                        cookies=self.cookies,
                    )
                    self.sniffer.force_update_resource(resource)

                # 音频已作为 companion 附加到视频，无需单独生成资源行
                self._log("B站DASH: 解析成功", "INFO")
            else:
                durl = data.get('data', {}).get('durl') or data.get('durl')
                if durl:
                    for seg in durl:
                        seg_url = seg.get('url', '')
                        if not seg_url:
                            continue
                        resource = VideoResource(
                            url=seg_url,
                            resource_type='flv',
                            title=f"B站视频 {seg.get('order', '')}",
                            size=seg.get('size', 0),
                            headers=dict(req_headers),
                            source='playinfo',
                            cookies=self.cookies,
                        )
                        self.sniffer.force_update_resource(resource)
        except Exception as e:
            # playinfo 解析失败会让 B 站 DASH 资源整体缺失，用 warning 记录以免静默失效
            logger.warning(f"playinfo data 解析异常: {e}", exc_info=True)


    @staticmethod
    def _get_track_url(track: dict) -> str:
        """从轨道字典提取 URL，兼容 baseUrl/base_url/url 等字段名"""
        if not track:
            return ""
        for key in ('baseUrl', 'base_url', 'url'):
            val = track.get(key)
            if val and isinstance(val, str) and val.startswith('http'):
                return val
        for key in ('backupUrl', 'backup_url'):
            val = track.get(key)
            if val and isinstance(val, list):
                for u in val:
                    if isinstance(u, str) and u.startswith('http'):
                        return u
        return ""

    @staticmethod
    def _get_init_url(track: dict, base_url: str = "") -> str:
        """从轨道字典提取初始化分片 URL（兼容相对路径）"""
        if not track:
            return ""
        # B站 playinfo 中初始化分片在 SegmentBase.Initialization
        for key in ('SegmentBase', 'segment_base'):
            seg_base = track.get(key)
            if isinstance(seg_base, dict):
                for init_key in ('Initialization', 'initialization'):
                    init = seg_base.get(init_key)
                    if isinstance(init, str) and init.strip():
                        init = init.strip()
                        if init.startswith('http'):
                            return init
                        # 相对路径用 base_url 拼接
                        if base_url:
                            return urljoin(base_url, init)
                        return init
        # 某些平台直接在 init_url / initialization 字段
        for key in ('init_url', 'initialization', 'init'):
            val = track.get(key)
            if val and isinstance(val, str) and val.strip():
                val = val.strip()
                if val.startswith('http'):
                    return val
                if base_url:
                    return urljoin(base_url, val)
                return val
        return ""

    def request_stop(self):
        """请求停止嗅探：主线程只置标记，Playwright 收尾由嗅探线程完成（跨线程安全）"""
        self._stop_event.set()
        self._active = False

    def get_sorted_resources(self) -> List[VideoResource]:
        return self.sniffer.get_sorted_resources()

    def get_page_title(self) -> str:
        return self.page_title or self.last_page_title

    def get_page_url(self) -> str:
        return self.page_url or self.last_page_url

    def close(self):
        """关闭浏览器（加锁串行化，避免两个线程同时操作 Playwright 对象）"""
        with self._close_lock:
            self._close_unlocked()

    def _close_unlocked(self):
        """关闭浏览器，清空状态防残留（含 Chromium 进程强制清理）"""
        self._active = False  # 先置 False，阻止回调访问已销毁资源
        try:
            if self.page:
                try:
                    self.page.close()
                except Exception:
                    pass
            if self.context:
                try:
                    self.context.close()
                except Exception:
                    pass
            if self.browser:
                try:
                    self.browser.close()
                except Exception:
                    pass
            if self.playwright:
                try:
                    self.playwright.stop()
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"关闭浏览器失败: {e}")
        finally:
            self.page = None
            self.context = None
            self.browser = None
            self.playwright = None
            self._session_headless = None
            self.cookies = []
            self.page_title = ""
            self.page_url = ""
            self._playinfo_read_count = 0
            self._last_playinfo_str = ""

        # 修复：不再全局 taskkill chrome.exe（会误杀用户正在使用的 Chrome）
        # Playwright 的 browser.close() 已会清理其启动的 Chromium 子进程


# ===================== mitmproxy 代理抓包 =====================
class ProxySniffer:
    """mitmproxy 本地代理抓包：任何浏览器走代理即可捕获视频（无需 Playwright）"""

    def __init__(self, sniffer: SmartSniffer, log_callback=None, port: int = 8888):
        self.sniffer = sniffer
        self.log_callback = log_callback
        self.port = port
        self.master = None
        self.thread = None
        self._running = False

    def _log(self, msg, level="INFO"):
        if self.log_callback:
            self.log_callback(msg, level)
        else:
            logger.info(msg)

    def start(self) -> bool:
        if not MITMPROXY_AVAILABLE:
            self._log("mitmproxy 未安装，请执行: pip install mitmproxy", "ERROR")
            return False
        if self._running:
            return True
        try:
            from mitmproxy import options
            from mitmproxy.tools.dump import DumpMaster
            import asyncio

            sniffer = self.sniffer
            log_fn = self._log

            class VideoAddon:
                def request(self, flow):
                    pass

                def response(self, flow):
                    try:
                        url = flow.request.pretty_url
                        ctype = flow.response.headers.get('content-type', '')
                        # 检测视频资源
                        is_video = False
                        url_lower = url.lower()
                        for ext in ('.mp4', '.m3u8', '.webm', '.flv', '.m4s', '.mpd', '.ts'):
                            if ext in url_lower and (ext + '?' in url_lower or ext + '#' in url_lower or url_lower.endswith(ext)):
                                is_video = True
                                break
                        if not is_video:
                            for mt in ('video/', 'mpegurl', 'mp2t', 'dash'):
                                if mt in ctype.lower():
                                    is_video = True
                                    break
                        if not is_video:
                            return
                        rtype = sniffer.detect_resource_type(url, ctype)
                        res = VideoResource(
                            url=url,
                            resource_type=rtype,
                            headers={
                                'User-Agent': flow.request.headers.get('user-agent', ''),
                                'Referer': flow.request.headers.get('referer', ''),
                                'Origin': flow.request.headers.get('origin', ''),
                            },
                            size=int(flow.response.headers.get('content-length', 0) or 0),
                            source='proxy'
                        )
                        res.resolution = sniffer.extract_resolution(url)
                        if sniffer.add_resource(res):
                            log_fn(f"[代理] 捕获: {rtype} {url[:80]}", "INFO")
                    except Exception:
                        pass

            opts = options.Options(listen_host='127.0.0.1', listen_port=self.port)
            self.master = DumpMaster(opts)
            self.master.addons.add(VideoAddon())

            def _run():
                try:
                    asyncio.set_event_loop(asyncio.new_event_loop())
                    self.master.run()
                except Exception:
                    pass

            self._running = True
            self.thread = threading.Thread(target=_run, daemon=True)
            self.thread.start()
            self._log(f"代理抓包已启动: 127.0.0.1:{self.port}", "INFO")
            self._log("请将浏览器代理设置为 127.0.0.1:" + str(self.port), "INFO")
            self._log("HTTPS 抓包需安装 mitmproxy 证书: http://mitm.it", "WARNING")
            return True
        except Exception as e:
            self._log(f"代理启动失败: {e}", "ERROR")
            return False

    def stop(self):
        self._running = False
        if self.master:
            try:
                self.master.shutdown()
            except Exception:
                pass
            self.master = None
        self._log("代理抓包已停止", "INFO")


# ===================== 主 GUI 应用 =====================

# ===================== 系统托盘 + 剪贴板监听 =====================
class TrayManager:
    """系统托盘图标 + 剪贴板视频 URL 监听"""

    def __init__(self, app):
        self.app = app
        self.tray = None
        self.clipboard_thread = None
        self._clipboard_running = False
        self._last_clipboard = ""

    def _create_image(self):
        """创建托盘图标（PIL 动态绘制）"""
        img = Image.new('RGB', (64, 64), color=(30, 136, 229))
        dc = ImageDraw.Draw(img)
        dc.rectangle([16, 20, 48, 44], fill=(255, 255, 255))
        dc.polygon([(24, 24), (24, 40), (42, 32)], fill=(30, 136, 229))
        return img

    def start(self):
        if not PYSTRAY_AVAILABLE:
            return
        try:
            menu = pystray.Menu(
                pystray.MenuItem("显示主窗口", self._show_window, default=True),
                pystray.MenuItem("剪贴板监听", self._toggle_clipboard,
                                 checked=lambda item: self._clipboard_running),
                pystray.MenuItem("退出", self._quit),
            )
            self.tray = pystray.Icon("video_downloader", self._create_image(),
                                     "视频下载器", menu)
            self.clipboard_thread = threading.Thread(target=self.tray.run, daemon=True)
            self.clipboard_thread.start()
        except Exception as e:
            logger.warning(f"托盘启动失败: {e}")

    def _show_window(self, icon=None, item=None):
        self.app.after(0, self.app.bring_to_front)

    def _toggle_clipboard(self, icon=None, item=None):
        """托盘菜单开关：与设置页共用同一个监听状态"""
        def _apply():
            self.app.set_clipboard_monitor(not self.app._clipboard_enabled)
        self.app.after(0, _apply)

    def _quit(self, icon=None, item=None):
        self._clipboard_running = False
        if self.tray:
            self.tray.stop()
        self.app.after(0, self.app.on_close_app)

    def stop(self):
        self._clipboard_running = False
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass


# ===================== 主 GUI 应用 =====================
class VideoDownloaderApp(tk.Tk):
    """视频下载器主应用 v4.2 - 网页嗅探 + yt-dlp 统一下载"""

    # 深色主题配色
    DARK_COLORS = {
        'primary': '#42a5f5', 'primary_light': '#64b5f6',
        'primary_dark': '#1976d2', 'secondary': '#00bcd4',
        'accent': '#90caf9', 'bg': '#1e1e2e', 'bg_card': '#252535',
        'text': '#e0e0e0', 'text_light': '#9fa8da',
        'border': '#333344', 'sash': '#4a5068', 'success': '#66bb6a',
        'warning': '#ffa726', 'error': '#ef5350'
    }
    LIGHT_COLORS = {
        'primary': '#1e88e5', 'primary_light': '#42a5f5',
        'primary_dark': '#1565c0', 'secondary': '#00bcd4',
        'accent': '#64b5f6', 'bg': '#f5f9ff', 'bg_card': '#ffffff',
        'text': '#1a237e', 'text_light': '#5c6bc0',
        'border': '#e3f2fd', 'sash': '#b7c5d8', 'success': '#4caf50',
        'warning': '#ff9800', 'error': '#f44336'
    }
    # 这些站点优先走 yt-dlp：浏览器嗅探通常抓不到 DASH 分片，还会白等 10 秒
    YTDLP_FIRST_HOSTS = (
        'bilibili.com', 'bilibili.tv', 'b23.tv', 'youtube.com', 'youtu.be',
        'tiktok.com', 'douyin.com', 'kuaishou.com', 'ixigua.com',
        'twitter.com', 'x.com', 'vimeo.com', 'twitch.tv',
        'instagram.com', 'facebook.com', 'dailymotion.com',
        'soundcloud.com', 'weibo.com', 'xiaohongshu.com',
        'youku.com', 'iqiyi.com', 'qq.com', 'mgtv.com', 'acfun.cn',
    )
    # 这些站点用 yt-dlp 解析必须带 cookie，否则解析阶段直接失败（抖音为典型：
    # 详情接口要 s_v_web_id，缺了会报 "Fresh cookies are needed"）。
    # 未导入 cookie 文件时改走浏览器嗅探，避免「yt-dlp 失败 → 回退」的多余流程。
    YTDLP_COOKIE_REQUIRED_HOSTS = (
        'douyin.com',
    )

    def __init__(self):
        super().__init__()
        self.title(f"网页视频嗅探下载器 {APP_VERSION}")
        self.geometry("1180x960")
        self.minsize(1020, 720)
        self.resizable(True, True)
        self.center_window()
        self._apply_window_icon()

        self.is_dark_theme = False
        self.colors = dict(self.LIGHT_COLORS)

        self.settings = {
            "skip_ad_count": "0", "max_retry": "3", "timeout": "30",
            "default_threads": "16",
            "ffmpeg_path": "ffmpeg",
            "auto_filter_ad": True, "auto_dedup": True,
            "scan_dom": True, "auto_headers": True,
            # 新增设置项
            "headless_browser": True,
            "auto_open_folder": False,
            "speed_limit": "0",
            "save_history": True,
            "aria2_path": "",
            "use_aria2": False,
            "proxy": "",
            "proxy_enabled": False,
            "download_mode": "auto",
            "show_welcome": True,
            "clipboard_monitor": False,
            "close_action": "ask",
            # v4.3 批量下载页选项
            "batch_save_dir": "",
            "batch_quality": "自动最佳",
            "batch_skip_existing": True,
            # 已导入的 yt-dlp Cookie 文件路径（抖音等站点解析必须带 cookie）
            "ytdlp_cookie_path": "",
        }

        # 核心组件
        self.sniffer = SmartSniffer()
        self.monitor = BrowserMonitor(self.sniffer, log_callback=self._log_message,
                                      update_callback=self.update_resource_list)
        # v4.0: 代理抓包 + 系统托盘
        self.proxy_sniffer = ProxySniffer(self.sniffer, log_callback=self._log_message)
        self.tray_manager = TrayManager(self)
        self.download_thread = None
        self.sniff_thread = None
        self.selected_resource: Optional[VideoResource] = None
        # 运行状态标记
        self.is_sniffing = False
        self.is_downloading = False
        self._task_kind = None          # 'sniff' / 'download' / None
        self._stop_requested = False
        self._task_generation = 0
        self._download_run_id = 0
        self._ytdlp_resources: List[VideoResource] = []  # 统一列表中 yt-dlp 解析出的格式
        self._ytdlp_page_title = ""
        self._sniff_token = 0
        self._sniff_run_id = 0
        self._probe_engine = None       # 嗅探阶段并行的 yt-dlp 解析引擎
        self._active_ytdlp_engine = None  # 当前下载使用的 yt-dlp 引擎
        self._last_download_error = ""
        # v4.3: 批量下载模块状态
        self.batch_items: List[BatchItem] = []
        self.batch_parsing = False
        self.batch_downloading = False
        self.batch_stop_requested = False
        self._batch_engine = None
        self._batch_thread = None
        self._batch_gen = 0
        self._batch_current_item: Optional[BatchItem] = None
        self._batch_current_index = 0
        self._batch_total_count = 0
        self._batch_iid_by_index: Dict[int, str] = {}
        self._batch_item_by_iid: Dict[str, BatchItem] = {}
        # 剪贴板监听（在主线程轮询，避免 Tk 线程问题）
        self._clipboard_enabled = False
        self._clipboard_job = None
        self._last_clipboard = ""
        # v4.0: 从嗅探页移到设置页的选项变量（提前初始化，控件在设置页创建）
        self.headless_var = tk.BooleanVar(value=self.settings.get("headless_browser", True))
        self.download_mode_var = tk.StringVar(value=self.settings.get("download_mode", "auto"))
        self.audio_only_var = tk.BooleanVar(value=False)
        self.live_mode_var = tk.BooleanVar(value=False)
        self.subtitles_var = tk.BooleanVar(value=False)
        # 新增：下载管理锁（防止 current_downloader 并发覆盖）
        self._download_lock = threading.Lock()
        # 新增：下载历史文件锁（下载线程写入与历史页删除/清空互斥）
        self._history_lock = threading.RLock()
        # 新增：下载任务队列
        self._download_queue: queue.Queue = queue.Queue()
        # 常驻嗅探线程队列：固定线程才能复用 Playwright/Chromium 会话
        self._sniff_queue: queue.Queue = queue.Queue()
        self._sniff_worker = None
        self._sniff_job_running = False
        self._queue_running = False
        self._queue_gen = 0  # 队列世代号：停止/兜底时递增，旧 worker 不再改全局状态
        # 新增：下载历史
        self._history_file = Path(os.path.join(_app_data_dir(), "download_history.json"))
        # 新增：配置文件路径
        self._config_file = Path(os.path.join(_app_data_dir(), "downloader_config.json"))

        self._load_config()
        # 上面两个 Tk 变量在 _load_config() 之前创建，这里按已加载的配置回填，
        # 否则「无头模式 / 下载模式」会一直用默认值，并在保存设置时把配置覆盖掉
        self.headless_var.set(bool(self.settings.get("headless_browser", True)))
        if self.settings.get("download_mode") in ("auto", "ffmpeg", "local"):
            self.download_mode_var.set(self.settings["download_mode"])
        if self.is_dark_theme:
            self.colors = dict(self.DARK_COLORS)
        self._setup_styles()
        self._create_main_frame()
        self.create_notebook()
        self._apply_theme()

        # 恢复上次导入的 yt-dlp Cookie 文件：抖音等站点的详情接口必须带 cookie，
        # 不持久化的话每次启动都要重新导入，等于用不了 yt-dlp 路径
        _cookie_path = self.settings.get("ytdlp_cookie_path", "") or ""
        if _cookie_path and os.path.isfile(_cookie_path):
            self._ydlp_cookie_path = _cookie_path
            if hasattr(self, 'ydlp_cookie_lbl'):
                self.ydlp_cookie_lbl.config(text=os.path.basename(_cookie_path))

        # 窗口关闭事件
        self.protocol("WM_DELETE_WINDOW", self._on_close_request)
        # 新增：注册 atexit 清理
        atexit.register(self._cleanup_on_exit)

        # FFmpeg 探测要起一个 98MB 的子进程（约 0.4 秒），依赖汇总和欢迎弹窗也要时间，
        # 全部推到窗口首帧画完后再做，否则启动阶段主线程一直被占着，
        # Windows 会把窗口标成「无响应」。
        self.ffmpeg_available = True
        self.update_idletasks()
        self.after(20, self._finish_startup)

        # 剪贴板监听默认关闭，只有用户在设置页或托盘里开启后才轮询
        if self.settings.get("clipboard_monitor", False):
            self.after(400, lambda: self.start_clipboard_monitor()
                       if self.settings.get("clipboard_monitor", False) else None)

    def _finish_startup(self):
        """窗口显示后的启动收尾：FFmpeg 探测、依赖汇总、托盘、欢迎页"""
        try:
            self.ffmpeg_available = UniversalDownloader.check_ffmpeg(self._resolve_ffmpeg_path())
            if not self.ffmpeg_available:
                self._log_message("FFmpeg 不可用，合并功能将受限。请在设置中配置正确的 FFmpeg 路径", "WARNING")
        except Exception:
            self.ffmpeg_available = False
        try:
            for _dname, _dok, _dhint in self._dependency_status():
                if not _dok:
                    self._log_message(f"可选组件缺失: {_dname} -> {_dhint}", "WARNING")
        except Exception:
            pass
        try:
            if PYSTRAY_AVAILABLE:
                self.tray_manager.start()
        except Exception:
            pass
        try:
            if self.settings.get("show_welcome", True):
                self.show_welcome_dialog()
        except Exception:
            pass

    def center_window(self):
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1 or h <= 1:  # 窗口尚未映射时 winfo_width 返回 1
            w, h = 1180, 960
        ws, hs = self.winfo_screenwidth(), self.winfo_screenheight()
        # 小屏（1366x768 / 高 DPI 缩放）下不要让窗口高于屏幕，否则底部日志区够不到
        w = min(w, max(1020, ws - 80))
        h = min(h, max(720, hs - 120))
        x, y = int((ws - w) / 2), max(0, int((hs - h) / 2))
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _apply_window_icon(self):
        """用 app.ico 顶掉 Tk 默认羽毛图标（标题栏、任务栏、子弹窗一起生效）"""
        if sys.platform == 'win32':
            try:
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                    'VideoDownloader.Desktop.App.1')
            except Exception:
                pass
        ico = self._resource_path('app.ico')
        if not os.path.isfile(ico):
            ico = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app.ico')
        if not os.path.isfile(ico):
            return
        # 注意：iconbitmap(default=ico) 只影响此后新建的窗口，对已存在的本窗口无效，
        # 必须显式给本窗口设一次；两者都设，子弹窗才不会退回 Tk 羽毛图标。
        try:
            self.iconbitmap(ico)
        except Exception:
            pass
        try:
            self.iconbitmap(default=ico)
        except Exception:
            pass

    def _minimize_to_tray(self):
        """最小化到系统托盘（无托盘时最小化到任务栏）"""
        has_tray = bool(PYSTRAY_AVAILABLE and getattr(self, 'tray_manager', None)
                        and getattr(self.tray_manager, 'tray', None))
        try:
            if has_tray:
                self.withdraw()
                self._log_message("已最小化到系统托盘，剪贴板监听继续运行", "INFO")
            else:
                self.iconify()
        except Exception:
            pass

    def _on_close_request(self):
        """点击窗口关闭按钮：按记忆设置处理，或询问最小化/退出"""
        action = self.settings.get('close_action', 'ask')
        if action == 'minimize':
            self._minimize_to_tray()
            return
        if action == 'exit':
            self.on_close_app()
            return
        if getattr(self, '_close_prompt_open', False):
            return
        self._close_prompt_open = True

        has_tray = bool(PYSTRAY_AVAILABLE and getattr(self, 'tray_manager', None)
                        and getattr(self.tray_manager, 'tray', None))
        minimize_text = "最小化到托盘" if has_tray else "最小化到任务栏"
        p = self._dialog_palette()

        dlg = tk.Toplevel(self)
        dlg.title("退出视频下载器")
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.configure(bg=p['bg'])
        dlg.protocol("WM_DELETE_WINDOW", lambda: self._close_choice(dlg, None))
        dlg.bind('<Escape>', lambda e: self._close_choice(dlg, None))

        box = tk.Frame(dlg, bg=p['bg'])
        box.pack(fill=tk.BOTH, expand=True, padx=24, pady=20)

        tk.Label(box, text="要退出视频下载器吗？", font=("微软雅黑", 14, "bold"),
                 bg=p['bg'], fg=p['text']).pack(anchor='w')
        tk.Label(box,
                 text=("最小化后程序会继续在后台运行，可从托盘图标恢复。" if has_tray
                       else "最小化后程序会继续在后台运行，可从任务栏恢复。"),
                 font=("微软雅黑", 9), bg=p['bg'], fg=p['sub'],
                 wraplength=380, justify=tk.LEFT).pack(anchor='w', pady=(8, 0))

        remember_var = tk.BooleanVar(value=False)
        tk.Checkbutton(box, text="记住我的选择，下次不再提示",
                       variable=remember_var, font=("微软雅黑", 9),
                       bg=p['bg'], fg=p['sub'], activebackground=p['bg'],
                       activeforeground=p['text'], selectcolor=p['card'],
                       highlightthickness=0, bd=0).pack(anchor='w', pady=(12, 0))

        btn_row = tk.Frame(box, bg=p['bg'])
        btn_row.pack(fill=tk.X, pady=(16, 0))
        self._dialog_button(btn_row, "取消",
                            lambda: self._close_choice(dlg, None),
                            'ghost', width=7).pack(side=tk.RIGHT, padx=(8, 0))
        self._dialog_button(btn_row, "退出程序",
                            lambda: self._close_choice(dlg, 'exit', remember_var.get()),
                            'danger', width=7).pack(side=tk.RIGHT, padx=(8, 0))
        self._dialog_button(btn_row, minimize_text,
                            lambda: self._close_choice(dlg, 'minimize', remember_var.get()),
                            'primary', width=9).pack(side=tk.RIGHT)

        # 高度按内容自适应：固定 430x248 会在按钮下方留一大片空白
        dlg.update_idletasks()
        dlg.geometry(f"430x{dlg.winfo_reqheight()}")
        dlg.update_idletasks()
        x = self.winfo_rootx() + max(0, (self.winfo_width() - dlg.winfo_width()) // 2)
        y = self.winfo_rooty() + max(0, (self.winfo_height() - dlg.winfo_height()) // 2)
        x = max(0, min(x, self.winfo_screenwidth() - dlg.winfo_width()))
        y = max(0, min(y, self.winfo_screenheight() - dlg.winfo_height() - 40))
        dlg.geometry(f"+{x}+{y}")
        try:
            dlg.grab_set()
            dlg.focus_force()
        except Exception:
            pass

    def _close_choice(self, dialog, choice, remember=False):
        """关闭提示框的选项处理；remember=True 时记住本次选择"""
        try:
            dialog.grab_release()
        except Exception:
            pass
        try:
            dialog.destroy()
        except Exception:
            pass
        self._close_prompt_open = False
        if choice in ('minimize', 'exit'):
            if remember:
                self.settings['close_action'] = choice
                if hasattr(self, 'close_action_var'):
                    self.close_action_var.set('最小化到后台' if choice == 'minimize' else '退出程序')
                self._save_config()
                action_text = "最小化到后台" if choice == 'minimize' else "退出程序"
                self._log_message(f"已记住关闭方式：{action_text}（可在设置页修改）", "INFO")
            if choice == 'minimize':
                self._minimize_to_tray()
            else:
                self.on_close_app()

    def on_close_app(self):
        """窗口关闭：停止嗅探、下载、浏览器，防进程残留"""
        self._save_config()
        try:
            with self._download_lock:
                if hasattr(self, 'current_downloader') and self.current_downloader:
                    self.current_downloader.stop()
                if getattr(self, '_active_ytdlp_engine', None):
                    self._active_ytdlp_engine.stop()
                if getattr(self, '_batch_engine', None):
                    self.batch_stop_requested = True
                    self._batch_engine.stop()
                if getattr(self, '_probe_engine', None):
                    self._probe_engine.stop()
        except Exception:
            pass
        try:
            if hasattr(self, 'monitor') and self.monitor:
                self.monitor.request_stop()
                # 先给嗅探线程一点收尾时间（它会自行 close Playwright），
                # 避免主线程与嗅探线程同时操作同一批同步对象
                sniff = getattr(self, 'sniff_thread', None)
                if (getattr(self, '_sniff_job_running', False) and sniff is not None
                        and sniff.is_alive() and sniff is not threading.current_thread()):
                    sniff.join(timeout=2.0)
                # 通知常驻嗅探线程退出，避免残留
                if getattr(self, '_sniff_queue', None) is not None:
                    try:
                        self._sniff_queue.put(None)
                    except Exception:
                        pass
                self.monitor.close()
        except Exception:
            pass
        # v4.0: 停止代理和托盘
        try:
            self.stop_clipboard_monitor(quiet=True)
        except Exception:
            pass
        try:
            if hasattr(self, 'proxy_sniffer'):
                self.proxy_sniffer.stop()
        except Exception:
            pass
        try:
            if hasattr(self, 'tray_manager'):
                self.tray_manager.stop()
        except Exception:
            pass
        self.destroy()
        sys.exit(0)

    def _cleanup_on_exit(self):
        """atexit 清理：强制停止残留下载器和临时文件"""
        try:
            if hasattr(self, 'current_downloader') and self.current_downloader:
                self.current_downloader.stop()
                if self.current_downloader.temp_dir and self.current_downloader.temp_dir.exists():
                    shutil.rmtree(self.current_downloader.temp_dir, ignore_errors=True)
        except Exception:
            pass

    def _load_config(self):
        """从配置文件加载设置（损坏自动备份+加载默认）"""
        if not self._config_file.exists():
            return
        try:
            with open(self._config_file, 'r', encoding='utf-8') as f:
                saved = json.load(f)
            for key, val in saved.items():
                if key in self.settings:
                    self.settings[key] = val
            if 'is_dark_theme' in saved:
                self.is_dark_theme = saved['is_dark_theme']
        except Exception as e:
            # 配置文件损坏/不可读：备份旧文件后使用默认配置，避免每次启动都报错
            bak_path = self._config_file.with_suffix('.bak')
            try:
                if not bak_path.exists():
                    shutil.copy2(self._config_file, bak_path)
                logger.warning(f"配置文件读取失败，已备份到 {bak_path}，错误: {e}")
            except Exception:
                pass

    def _save_config(self):
        """保存设置到配置文件（先写临时文件再原子替换，避免中断导致配置损坏）"""
        tmp_path = self._config_file.with_suffix('.tmp')
        try:
            data = dict(self.settings)
            data['is_dark_theme'] = self.is_dark_theme
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._config_file)
        except Exception as e:
            logger.warning(f"保存配置失败: {e}")
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass

    def _load_history(self) -> List:
        """加载下载历史"""
        try:
            if self._history_file.exists():
                with open(self._history_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception:
            pass
        return []

    def _save_history(self, url: str, save_path: str, status: str = "success",
                      resource: Optional[VideoResource] = None):
        """保存或更新一条下载历史（记录文件信息、页面信息、引擎等）"""
        try:
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            abs_path = os.path.abspath(save_path) if save_path else ""
            file_name = os.path.basename(abs_path) if abs_path else ""
            try:
                file_size = os.path.getsize(abs_path) if abs_path and os.path.exists(abs_path) else 0
            except Exception:
                file_size = 0

            page_url = ""
            page_title = ""
            title = ""
            engine = "内置下载器"
            browser = "内置浏览器"
            if resource is not None:
                page_url = (getattr(resource, 'page_url', '') or '').strip()
                page_title = (getattr(resource, 'page_title', '') or '').strip()
                title = (getattr(resource, 'title', '') or '').strip()
                if getattr(resource, 'engine', '') == 'ytdlp':
                    engine = "yt-dlp"
                    browser = "yt-dlp"
                    if not page_title:
                        page_title = title
                elif getattr(resource, 'source', '') == 'direct':
                    browser = "直链下载"
                else:
                    browser = "内置浏览器（Chromium）"

            # 页面标题/页面地址兜底（直链、本地导入不要误用上一次嗅探的页面）
            allow_page_fallback = (resource is None or
                                   getattr(resource, 'source', '') in
                                   ('network', 'dom', 'playinfo', 'yt-dlp'))
            if not page_title and allow_page_fallback:
                page_title = (getattr(self.monitor, 'last_page_title', '') or
                              getattr(self.monitor, 'page_title', '') or '')
            if not page_url and allow_page_fallback:
                page_url = (getattr(self.monitor, 'page_url', '') or url or '')
            if not page_url:
                page_url = url or ''
            if not title:
                title = os.path.splitext(file_name)[0] if file_name else (page_title or url)

            record = {
                'url': url or page_url,
                'page_url': page_url,
                'page_title': page_title,
                'title': title,
                'path': abs_path,
                'file_name': file_name,
                'size': file_size,
                'time': now,
                'added_on': now,
                'last_try': now,
                'status': status,
                'engine': engine,
                'browser': browser,
                'source': getattr(resource, 'source', '') if resource else '',
            }

            with self._history_lock:
                history = self._load_history()
                norm_path = os.path.normcase(abs_path) if abs_path else ""
                matched = False
                for i, old in enumerate(history):
                    old_path = old.get('path', '') or ''
                    if norm_path and old_path and os.path.normcase(os.path.abspath(old_path)) == norm_path:
                        added_on = old.get('added_on') or old.get('time') or now
                        old.update(record)
                        old['added_on'] = added_on
                        old['last_try'] = now
                        old['time'] = now
                        history.pop(i)
                        history.insert(0, old)
                        matched = True
                        break
                if not matched:
                    history.insert(0, record)
                self._write_history(history[:100])
            # 下载线程写完后刷新历史页
            try:
                self.after(0, self._refresh_history)
            except Exception:
                pass
        except Exception:
            pass

    def _write_history(self, history):
        """原子写入历史文件（调用方需持有 self._history_lock）"""
        tmp_path = self._history_file.with_suffix('.tmp')
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self._history_file)

    def _toggle_theme(self):
        """切换深色/浅色主题"""
        self.is_dark_theme = not self.is_dark_theme
        self.colors = dict(self.DARK_COLORS if self.is_dark_theme else self.LIGHT_COLORS)
        try:
            self._apply_theme()
        except Exception as e:
            logger.warning(f"切换主题失败: {e}")
        # 更新按钮文字（不用 emoji，避免部分系统缺少字形显示成方块）
        self._sync_theme_button()

    def _sync_theme_button(self):
        """让主题按钮文字与当前主题保持一致（启动时按配置进入深色主题也要同步）"""
        if hasattr(self, 'theme_btn'):
            self.theme_btn.config(text="浅色模式" if self.is_dark_theme else "深色模式")

    # ---------------- 剪贴板监听（主线程轮询） ----------------
    def bring_to_front(self):
        """把窗口从最小化/托盘恢复并短暂置顶"""
        try:
            self.deiconify()
            try:
                if self.state() == 'iconic':
                    self.state('normal')
            except Exception:
                pass
            self.lift()
            self.attributes('-topmost', True)
            self.focus_force()

            def _reset_topmost():
                try:
                    self.attributes('-topmost', False)
                except Exception:
                    pass
            self.after(900, _reset_topmost)
        except Exception:
            pass

    def _safe_clipboard_get(self) -> str:
        try:
            text = self.clipboard_get()
            if text:
                return text if isinstance(text, str) else str(text)
        except Exception:
            pass
        # 某些剪贴板内容需要指定类型才能读取
        for clip_type in ('STRING', 'UTF8_STRING'):
            try:
                text = self.clipboard_get(type=clip_type)
                if text:
                    return text if isinstance(text, str) else str(text)
            except Exception:
                continue
        return ""

    @staticmethod
    def _extract_clipboard_url(text: str) -> str:
        """从剪贴板文本中提取可嗅探的网页/视频 URL（排除图片等静态资源）"""
        text = (text or '').strip()
        if not text:
            return ""
        match = re.search(r'https?://[^\s<>"\']+', text, re.I)
        if match:
            url = match.group(0)
        else:
            match = re.search(r'(?<![\w.])www\.[^\s<>"\']+', text, re.I)
            if not match:
                return ""
            url = 'https://' + match.group(0)
        url = url.rstrip('.,;:!?)]}>，。；！？）】》')
        try:
            parsed = urlparse(url)
        except Exception:
            return ""
        if not parsed.netloc:
            return ""
        bad_ext = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.ico',
                   '.css', '.js', '.json', '.xml', '.pdf', '.zip', '.rar',
                   '.7z', '.exe', '.msi', '.apk', '.txt', '.doc', '.docx',
                   '.xls', '.xlsx', '.ppt', '.pptx')
        if parsed.path.lower().endswith(bad_ext):
            return ""
        return url

    def _refresh_clipboard_button(self):
        """更新主界面剪贴板监听按钮文字，并同步设置页复选框状态"""
        if hasattr(self, 'clipboard_btn'):
            try:
                self.clipboard_btn.config(
                    text="剪贴板监听：开" if self._clipboard_enabled else "剪贴板监听：关")
            except Exception:
                pass
        # 复选框必须与实际监听状态一致，避免出现“勾选了但没监听/没勾选却在监听”
        if hasattr(self, 'clipboard_var'):
            try:
                if bool(self.clipboard_var.get()) != bool(self._clipboard_enabled):
                    self.clipboard_var.set(bool(self._clipboard_enabled))
            except Exception:
                pass

    def _toggle_clipboard_from_toolbar(self):
        """主界面一键开关剪贴板监听"""
        self.set_clipboard_monitor(not self._clipboard_enabled)

    def start_clipboard_monitor(self):
        """开启剪贴板监听（在主线程用 after 轮询，避免后台线程访问 Tk）"""
        if self._clipboard_enabled:
            # 已在监听：仍然同步一次复选框/按钮，避免界面状态残留
            self._refresh_clipboard_button()
            return
        self._clipboard_enabled = True
        self._last_clipboard = self._safe_clipboard_get()
        if hasattr(self, 'clipboard_var'):
            self.clipboard_var.set(True)
        if hasattr(self, 'tray_manager'):
            self.tray_manager._clipboard_running = True
        self._refresh_clipboard_button()
        self._log_message("剪贴板监听已开启：复制网页/视频链接会自动弹窗", "DEBUG")
        if self._clipboard_job:
            try:
                self.after_cancel(self._clipboard_job)
            except Exception:
                pass
        self._clipboard_job = self.after(650, self._poll_clipboard)

    def stop_clipboard_monitor(self, quiet: bool = False):
        """关闭剪贴板监听"""
        self._clipboard_enabled = False
        if self._clipboard_job:
            try:
                self.after_cancel(self._clipboard_job)
            except Exception:
                pass
            self._clipboard_job = None
        if hasattr(self, 'clipboard_var'):
            self.clipboard_var.set(False)
        if hasattr(self, 'tray_manager'):
            self.tray_manager._clipboard_running = False
        self._refresh_clipboard_button()
        if not quiet:
            self._log_message("剪贴板监听已关闭", "INFO")

    def set_clipboard_monitor(self, enabled: bool):
        """统一设置剪贴板监听状态（设置页/托盘共用）"""
        if enabled:
            self.start_clipboard_monitor()
        else:
            self.stop_clipboard_monitor()
        self.settings["clipboard_monitor"] = bool(enabled)
        self._save_config()

    def _poll_clipboard(self):
        self._clipboard_job = None
        if not self._clipboard_enabled:
            return
        try:
            text = self._safe_clipboard_get()
            if text and text != self._last_clipboard:
                self._last_clipboard = text
                url = self._extract_clipboard_url(text)
                if url:
                    self._log_message(f"剪贴板检测到链接：{url[:90]}", "DEBUG")
                    self._on_clipboard_url(url)
        except Exception:
            pass
        if self._clipboard_enabled:
            self._clipboard_job = self.after(650, self._poll_clipboard)

    def _on_clipboard_url(self, url: str):
        """剪贴板检测到链接：置顶主窗口后询问是否下载"""
        self.bring_to_front()
        try:
            confirmed = messagebox.askyesno(
                "检测到网页/视频链接",
                f"是否用视频下载器打开以下链接？\n\n{url[:120]}",
                parent=self)
        except Exception:
            confirmed = messagebox.askyesno(
                "检测到网页/视频链接",
                f"是否用视频下载器打开以下链接？\n\n{url[:120]}")
        if confirmed:
            self.url_entry.delete(0, tk.END)
            self.url_entry.insert(0, url)
            self.bring_to_front()
            self._dl_click_start()


    def _apply_theme(self):
        """应用当前主题到所有控件"""
        c = self.colors
        dark = self.is_dark_theme

        # 窗口背景
        self.configure(bg=c['bg_card'])

        # 头部框架
        self.header_frame.config(bg=c['primary'])
        for child in self.header_frame.winfo_children():
            if isinstance(child, tk.Label):
                child.config(bg=c['primary'])
        if hasattr(self, 'header_line'):
            self.header_line.config(bg=c['primary_dark'])

        # ttk 全局样式
        self.style.configure('.', background=c['bg_card'], foreground=c['text'])

        # Notebook 样式
        self.style.configure('Custom.TNotebook', background=c['bg_card'])
        tab_bg = c['bg'] if dark else '#f5f5f5'
        self.style.configure('Custom.TNotebook.Tab',
            background=tab_bg, foreground=(c['text_light'] if dark else '#666666'),
            padding=[20, 9, 20, 9], font=('微软雅黑', 10))
        self.style.map('Custom.TNotebook.Tab',
            background=[('selected', c['bg_card']), ('active', c['bg'] if dark else '#eeeeee')],
            foreground=[('selected', c['primary']), ('active', c['text'])])

        # 主按钮样式
        self.style.configure('Primary.TButton',
            background=c['primary'], foreground='white', borderwidth=0,
            padding=(20, 10), font=('微软雅黑', 10, 'bold'), relief='flat')
        self.style.map('Primary.TButton',
            background=[('pressed', c['primary_dark']), ('active', c['primary_light'])])

        # 顶部栏浅色按钮 / 工具行按钮
        self.style.configure('Header.TButton',
            background='#ffffff', foreground=c['primary_dark'], borderwidth=0,
            padding=(9, 5), font=('微软雅黑', 9), relief='flat')
        self.style.map('Header.TButton',
            background=[('active', '#e3f2fd'), ('pressed', '#bbdefb')])
        self.style.configure('Tool.TButton',
            background=c['bg_card'], foreground=c['primary'], borderwidth=1,
            bordercolor=c['border'], padding=(9, 4),
            font=('微软雅黑', 9), relief='solid')
        self.style.map('Tool.TButton',
            background=[('active', c['bg']), ('pressed', c['bg'])],
            bordercolor=[('active', c['primary_light'])])
        self.style.configure('Flat.TButton',
            background=c['bg_card'], foreground=c['primary'], borderwidth=0,
            padding=(9, 5), font=('微软雅黑', 9), relief='flat')
        self.style.map('Flat.TButton',
            background=[('active', c['bg']), ('pressed', c['bg'])],
            foreground=[('active', c['primary_dark'])])

        # 次按钮样式
        self.style.configure('Secondary.TButton',
            background=c['bg_card'], foreground=c['primary'], borderwidth=1,
            bordercolor=c['border'], padding=(18, 8),
            font=('微软雅黑', 10), relief='solid')
        self.style.map('Secondary.TButton',
            background=[('pressed', c['bg']), ('active', c['bg'])])

        # 输入框样式
        self.style.configure('Modern.TEntry',
            fieldbackground=c['bg'], foreground=c['text'], borderwidth=1,
            bordercolor=c['border'], padding=5, font=('微软雅黑', 10), relief='solid')
        self.style.map('Modern.TEntry',
            bordercolor=[('focus', c['primary']), ('!focus', c['border'])],
            fieldbackground=[('readonly', c['bg'])])
        self.style.configure('TCombobox',
            fieldbackground=c['bg'], foreground=c['text'], background=c['bg_card'],
            arrowcolor=c['primary'], bordercolor=c['border'], padding=3,
            font=('微软雅黑', 9), relief='solid')
        self.style.configure('TEntry',
            fieldbackground=c['bg'], foreground=c['text'],
            insertcolor=c['text'], bordercolor=c['border'])
        self.style.map('TCombobox',
            fieldbackground=[('readonly', c['bg']), ('focus', c['bg']),
                             ('active', c['bg']), ('disabled', c['bg_card'])],
            foreground=[('readonly', c['text']), ('focus', c['text']),
                        ('active', c['text'])],
            selectbackground=[('readonly', c['primary'])],
            selectforeground=[('readonly', 'white')],
            arrowcolor=[('active', c['primary_light'])])

        # 进度条样式
        self.style.configure('Blue.Horizontal.TProgressbar',
            troughcolor=c['bg'], background=c['primary'],
            borderwidth=0, thickness=8)

        # 表格样式
        self.style.configure('Resource.Treeview',
            background=c['bg_card'], foreground=c['text'], rowheight=26,
            fieldbackground=c['bg_card'], font=('微软雅黑', 9))
        self.style.map('Resource.Treeview',
            background=[('selected', c['primary'])],
            foreground=[('selected', 'white')])
        self.style.configure('Resource.Treeview.Heading',
            background=c['bg'], foreground=c['text'], padding=(8, 6),
            font=('微软雅黑', 9, 'bold'))
        self.style.map('Resource.Treeview.Heading',
            background=[('active', c['bg'])])

        # 复选框样式
        self.style.configure('TCheckbutton',
            background=c['bg_card'], foreground=c['text'],
            font=('微软雅黑', 10))
        self.style.map('TCheckbutton',
            background=[('active', c['bg_card'])],
            foreground=[('active', c['text'])])

        # 滚动条样式
        self.style.configure('TScrollbar',
            background=c['bg'], troughcolor=c['bg_card'],
            bordercolor=c['border'], arrowcolor=c['text'])
        self.style.map('TScrollbar',
            background=[('active', c['bg'])])

        # 4个标签页 Frame 背景及子控件递归更新
        for frame in (self.download_frame, self.batch_frame,
                      self.settings_frame, self.history_frame):
            # 下载页使用浅色工作区，内部再放白色卡片
            frame.config(bg=c['bg'] if frame is self.download_frame else c['bg_card'])
            self._update_widget_colors(frame)

        # 设置页的卡片是 Canvas 的内嵌窗口，单独再刷新一次，确保深色模式标签/输入框颜色正确
        if hasattr(self, 'settings_card'):
            self.settings_card.config(bg=c['bg_card'], highlightbackground=c['border'])
            self._update_widget_colors(self.settings_card)

        # tk.PanedWindow 的 sash 颜色（必须单独设置，它不属于 Frame/Label）
        if hasattr(self, '_body_paned'):
            self._body_paned.configure(bg=c.get('sash', '#b7c5d8'))
        if hasattr(self, 'log_frame'):
            self.log_frame.configure(bg=c['bg_card'])
        # 操作按钮颜色跟随主题
        self._refresh_action_button_colors()
        # 下载页自定义卡片/日志/表格颜色
        self._apply_download_extras()
        # 批量下载页自定义卡片/日志/表格颜色
        self._apply_batch_extras()

    def _apply_download_extras(self):
        """下载页自定义控件的主题色（卡片、状态胶囊、日志控制台、斑马纹）"""
        c = self.colors
        dark = self.is_dark_theme
        if hasattr(self, 'url_card'):
            self.url_card.config(bg=c['bg'], highlightbackground=c['border'])
        if hasattr(self, 'url_card_inner'):
            self.url_card_inner.config(bg=c['bg'])
        if hasattr(self, 'url_link_lbl'):
            self.url_link_lbl.config(bg=c['bg'], fg=c['text_light'])
        if hasattr(self, 'top_sep'):
            self.top_sep.config(bg=c['border'])
        if hasattr(self, 'status_dot'):
            self.status_dot.config(bg=c['bg_card'])
        if hasattr(self, 'resource_count_lbl'):
            self.resource_count_lbl.config(
                bg='#26364a' if dark else '#e8f1fe',
                fg=c['primary_light'] if dark else c['primary'])
        if hasattr(self, '_body_paned'):
            self._body_paned.config(bg=c.get('sash', '#b7c5d8'))
        if hasattr(self, 'log_frame'):
            self.log_frame.config(bg=c['bg_card'])
        if hasattr(self, 'log_text'):
            log_bg = '#141824' if dark else '#171b26'
            log_fg = '#d7dce5'
            self.log_text.config(bg=log_bg, fg=log_fg,
                                 insertbackground=log_fg,
                                 selectbackground=c['primary_dark'],
                                 selectforeground='white')
            self.log_text.tag_config('INFO', foreground=log_fg)
            self.log_text.tag_config('WARNING', foreground='#ffcc80')
            self.log_text.tag_config('ERROR', foreground='#ff8a80')
        if hasattr(self, 'resource_tree'):
            self.resource_tree.tag_configure(
                'row_even', background=c['bg_card'])
            self.resource_tree.tag_configure(
                'row_odd', background='#2a2a3d' if dark else '#f7faff')
        if hasattr(self, 'history_tree'):
            self.history_tree.tag_configure(
                'row_even', background=c['bg_card'])
            self.history_tree.tag_configure(
                'row_odd', background='#2a2a3d' if dark else '#f7faff')
        # 设置/历史页标题等直接挂在 frame 上的控件也不会被递归到，单独刷新
        if hasattr(self, 'settings_title_lbl'):
            self.settings_title_lbl.config(bg=c['bg_card'], fg=c['text'])
        if hasattr(self, 'history_title_lbl'):
            self.history_title_lbl.config(bg=c['bg_card'], fg=c['text'])
        if hasattr(self, 'history_count_lbl'):
            self.history_count_lbl.config(bg=c['bg_card'], fg=c['text_light'])
        self._refresh_clipboard_button()

    def _update_widget_colors(self, parent):
        """递归更新子控件颜色以匹配当前主题"""
        c = self.colors
        # 主题文字色集合，用于判断是否需要切换前景色
        theme_text_fgs = {'#1a237e', '#e0e0e0'}
        theme_light_fgs = {'#5c6bc0', '#9fa8da', '#e3f2fd'}
        for child in parent.winfo_children():
            if isinstance(child, scrolledtext.ScrolledText):
                child.config(bg=c['bg'], fg=c['text'],
                           insertbackground=c['text'],
                           selectbackground=c['primary'],
                           selectforeground='white')
                self._update_widget_colors(child)
            elif isinstance(child, tk.Frame):
                child.config(bg=c['bg_card'])
                try:
                    if float(child.cget('highlightthickness') or 0) > 0:
                        child.config(highlightbackground=c['border'])
                except (ValueError, TypeError):
                    pass
                self._update_widget_colors(child)
            elif isinstance(child, tk.Label):
                current_fg = str(child.cget('fg'))
                if current_fg in theme_text_fgs:
                    child.config(bg=c['bg_card'], fg=c['text'])
                elif current_fg in theme_light_fgs:
                    child.config(bg=c['bg_card'], fg=c['text_light'])
                else:
                    child.config(bg=c['bg_card'])
            elif isinstance(child, tk.Canvas):
                child.config(bg=c['bg_card'])
                self._update_widget_colors(child)
            else:
                self._update_widget_colors(child)

    def _dependency_status(self):
        """汇总可选依赖状态（供启动提示与欢迎弹窗展示）"""
        ffmpeg_ok = getattr(self, 'ffmpeg_available', False)
        return [
            ("yt-dlp 解析引擎", YTDLP_AVAILABLE, "pip install yt-dlp"),
            ("网络请求库 (requests)", REQUESTS_AVAILABLE, "pip install requests"),
            ("AES 解密库 (pycryptodome)", CRYPTO_AVAILABLE, "pip install pycryptodome"),
            ("浏览器嗅探 (Playwright)", PLAYWRIGHT_AVAILABLE, "pip install playwright && playwright install chromium"),
            ("代理抓包 (mitmproxy)", MITMPROXY_AVAILABLE, "pip install mitmproxy"),
            ("系统托盘 (pystray)", PYSTRAY_AVAILABLE, "pip install pystray"),
            ("FFmpeg 合并引擎", ffmpeg_ok, "请下载 ffmpeg 并在设置中配置路径"),
        ]

    @staticmethod
    def _resource_path(filename: str) -> str:
        """获取源码/打包后的资源文件路径"""
        base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(base, filename)

    def _dialog_palette(self):
        """弹窗配色：跟随当前深色/浅色主题"""
        if self.is_dark_theme:
            return {'bg': '#252535', 'card': '#2d2d40', 'text': '#e0e0e0',
                    'sub': '#9fa8da', 'hint': '#8a8aa8', 'border': '#4a4a66',
                    'primary': '#42a5f5', 'primary_dark': '#1976d2',
                    'primary_fg': '#ffffff', 'danger': '#ef5350'}
        return {'bg': '#ffffff', 'card': '#f7f9fc', 'text': '#1a237e',
                'sub': '#5c6bc0', 'hint': '#8a94a6', 'border': '#d7e2ef',
                'primary': '#1e88e5', 'primary_dark': '#1565c0',
                'primary_fg': '#ffffff', 'danger': '#d32f2f'}

    def _dialog_button(self, parent, text, command, kind='ghost', width=8):
        """统一样式的弹窗按钮：primary=实心主按钮 / danger=红色文字 / ghost=普通"""
        p = self._dialog_palette()
        if kind == 'primary':
            bg, fg, abg, afg = p['primary'], p['primary_fg'], p['primary_dark'], p['primary_fg']
            hl = 0
        elif kind == 'danger':
            bg, fg, abg, afg = p['card'], p['danger'], p['bg'], p['danger']
            hl = 1
        else:
            bg, fg, abg, afg = p['card'], p['sub'], p['bg'], p['text']
            hl = 1
        return tk.Button(parent, text=text, command=command,
                         font=("微软雅黑", 10), bg=bg, fg=fg,
                         activebackground=abg, activeforeground=afg,
                         relief=tk.FLAT, bd=0, highlightthickness=hl,
                         highlightbackground=p['border'], cursor='hand2',
                         padx=10, pady=7, width=width)

    def show_group_dialog(self):
        """加入群聊：QQ / Telegram / GitHub，二维码自适应缩放，点击可放大"""
        if getattr(self, '_group_dialog', None) is not None:
            try:
                if self._group_dialog.winfo_exists():
                    self._group_dialog.lift()
                    self._group_dialog.focus_force()
                    return
            except Exception:
                pass
        self._group_dialog = None
        entries = [dict(item) for item in GROUP_LINKS]
        p = self._dialog_palette()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = min(560, max(440, sw - 160))
        h = min(780, max(540, sh - 160))

        dlg = tk.Toplevel(self)
        dlg.title("加入群聊 - 视频下载器")
        dlg.geometry(f"{w}x{h}")
        dlg.minsize(420, 500)
        dlg.transient(self)
        dlg.configure(bg=p['bg'])
        self._group_dialog = dlg

        def on_close():
            self._group_dialog = None
            try:
                dlg.destroy()
            except Exception:
                pass

        dlg.protocol("WM_DELETE_WINDOW", on_close)

        head = tk.Frame(dlg, bg=p['bg'])
        head.pack(fill=tk.X, padx=24, pady=(20, 0))
        tk.Label(head, text="加入群聊", font=("微软雅黑", 17, "bold"),
                 bg=p['bg'], fg=p['text']).pack(anchor='w')
        tk.Label(head, text="bug 反馈、使用答疑、新版通知都在群里",
                 font=("微软雅黑", 9), bg=p['bg'],
                 fg=p['sub']).pack(anchor='w', pady=(6, 0))
        tk.Frame(dlg, bg=p['border'], height=1).pack(fill=tk.X, padx=24, pady=(12, 0))

        tab_row = tk.Frame(dlg, bg=p['bg'])
        tab_row.pack(fill=tk.X, padx=24, pady=(12, 0))

        body = tk.Frame(dlg, bg=p['card'], highlightbackground=p['border'],
                        highlightthickness=1)
        body.pack(fill=tk.BOTH, expand=True, padx=24, pady=(10, 0))
        img_holder = tk.Frame(body, bg=p['card'])
        img_holder.pack(fill=tk.BOTH, expand=True, padx=8, pady=(8, 0))

        foot = tk.Frame(body, bg=p['card'])
        foot.pack(fill=tk.X, padx=12, pady=(0, 12))
        info_var = tk.StringVar(value='')
        status_var = tk.StringVar(value='')
        tk.Label(foot, textvariable=info_var, font=("微软雅黑", 9), bg=p['card'],
                 fg=p['sub'], wraplength=w - 80,
                 justify='left').pack(anchor='w')
        btn_row = tk.Frame(foot, bg=p['card'])
        btn_row.pack(fill=tk.X, pady=(8, 0))

        state = {'idx': 0, 'photo': None, 'label': None, 'fallback': None}
        tab_btns = []

        def current():
            return entries[state['idx']]

        def copy_value(kind):
            entry = current()
            value = entry.get('number') if kind == 'number' else entry.get('url')
            if not value:
                return
            try:
                self.clipboard_clear()
                self.clipboard_append(value)
                # 记一下，避免剪贴板监听把自己刚复制的链接又弹一次窗
                self._last_clipboard = value
                status_var.set(f"已复制{'群号' if kind == 'number' else '链接'}")
            except Exception as e:
                status_var.set(f"复制失败：{e}")

        def open_url():
            url = current().get('url') or ''
            if not url:
                return
            try:
                import webbrowser
                webbrowser.open(url)
                status_var.set("已在浏览器打开")
            except Exception:
                status_var.set("打开失败，可复制链接手动打开")

        copy_num_btn = self._dialog_button(btn_row, "复制群号",
                                           lambda: copy_value('number'), 'ghost', width=8)
        copy_num_btn.pack(side=tk.LEFT, padx=(0, 8))
        copy_url_btn = self._dialog_button(btn_row, "复制链接", lambda: copy_value('url'),
                                           'ghost', width=8)
        copy_url_btn.pack(side=tk.LEFT, padx=(0, 8))
        open_btn = self._dialog_button(btn_row, "打开链接", open_url, 'primary', width=8)
        open_btn.pack(side=tk.LEFT)
        tk.Label(btn_row, textvariable=status_var, font=("微软雅黑", 9),
                 bg=p['card'], fg=p['sub']).pack(side=tk.LEFT, padx=(12, 0))

        def render():
            """按当前可用区域缩放二维码；切标签、改窗口大小都会重绘"""
            try:
                if not dlg.winfo_exists():
                    return
                iw, ih = img_holder.winfo_width(), img_holder.winfo_height()
            except Exception:
                return
            if iw < 60 or ih < 60:
                iw, ih = w - 70, h - 280
            entry = current()
            max_w, max_h = max(120, iw - 4), max(120, ih - 4)
            photo = None
            qr = entry.get('qr') or ''
            if qr:
                try:
                    from PIL import Image, ImageTk
                    path = self._resource_path(qr)
                    if os.path.exists(path):
                        img = Image.open(path).convert("RGB")
                        img.thumbnail((max_w, max_h), Image.LANCZOS)
                        photo = ImageTk.PhotoImage(img)
                except Exception:
                    photo = None
            if photo is None:
                if state['label'] is not None:
                    state['label'].pack_forget()
                if state['fallback'] is None:
                    state['fallback'] = tk.Label(
                        img_holder, bg=p['card'], fg=p['hint'],
                        font=("微软雅黑", 10), justify='center')
                if qr:
                    state['fallback'].config(
                        text=f"{entry['name']} 二维码还没配置\n把 {qr} 放到程序目录就能显示")
                else:
                    state['fallback'].config(text=f"{entry['name']} 暂无二维码")
                state['fallback'].pack(expand=True)
            else:
                if state['fallback'] is not None:
                    state['fallback'].pack_forget()
                state['photo'] = photo
                if state['label'] is None:
                    state['label'] = tk.Label(img_holder, bg=p['card'], cursor='hand2')
                    state['label'].pack(expand=True)
                    state['label'].bind('<Button-1>', lambda e: self._show_group_qr(current()))
                state['label'].config(image=photo)

            parts = []
            if entry.get('number'):
                parts.append(f"群号：{entry['number']}")
            if entry.get('url'):
                parts.append(entry['url'])
            info_var.set('\n'.join(parts) if parts else (entry.get('hint') or '联系方式待补充'))
            copy_num_btn.config(state=tk.NORMAL if entry.get('number') else tk.DISABLED)
            copy_url_btn.config(state=tk.NORMAL if entry.get('url') else tk.DISABLED)
            open_btn.config(state=tk.NORMAL if entry.get('url') else tk.DISABLED)

        def select(idx):
            state['idx'] = idx
            for i, btn in enumerate(tab_btns):
                sel = (i == idx)
                btn.config(bg=p['primary'] if sel else p['card'],
                           fg=p['primary_fg'] if sel else p['sub'],
                           activebackground=p['primary_dark'] if sel else p['border'],
                           activeforeground=p['primary_fg'] if sel else p['text'])
            status_var.set('')
            render()

        for i, entry in enumerate(entries):
            btn = tk.Button(tab_row, text=entry['name'], font=("微软雅黑", 10, "bold"),
                            relief=tk.FLAT, bd=0, cursor='hand2', padx=18, pady=7,
                            command=lambda i=i: select(i))
            btn.pack(side=tk.LEFT, padx=(0, 8))
            tab_btns.append(btn)

        rerender_job = {'id': None}

        def on_configure(event):
            if rerender_job['id']:
                try:
                    dlg.after_cancel(rerender_job['id'])
                except Exception:
                    pass
            rerender_job['id'] = dlg.after(120, render)

        dlg.bind('<Configure>', on_configure)
        dlg.after(80, lambda: select(0))

    def _show_group_qr(self, entry):
        """放大查看群二维码"""
        p = self._dialog_palette()
        qr = entry.get('qr') or ''
        if not qr:
            return
        path = self._resource_path(qr)
        try:
            from PIL import Image, ImageTk
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            img = Image.open(path).convert("RGB")
            img.thumbnail((min(720, sw - 120), min(720, sh - 160)), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
        except Exception:
            return
        if getattr(self, '_group_qr_win', None) is not None:
            try:
                if self._group_qr_win.winfo_exists():
                    self._group_qr_win.destroy()
            except Exception:
                pass
        win = tk.Toplevel(self)
        win.title(f"{entry['name']} - 群二维码")
        win.configure(bg=p['bg'])
        win.resizable(False, False)
        win._photo = photo
        self._group_qr_win = win
        tk.Label(win, text=f"{entry['name']} · 扫码进群", font=("微软雅黑", 13, "bold"),
                 bg=p['bg'], fg=p['text']).pack(pady=(14, 8))
        tk.Label(win, image=photo, bg=p['bg']).pack(padx=14)
        tk.Label(win, text="按 Esc 或点击下方按钮关闭", font=("微软雅黑", 9),
                 bg=p['bg'], fg=p['hint']).pack(pady=(8, 0))

        def close_win():
            self._group_qr_win = None
            try:
                win.destroy()
            except Exception:
                pass

        self._dialog_button(win, "关闭", close_win, 'primary', width=8).pack(pady=12)
        win.bind('<Escape>', lambda e: close_win())
        win.protocol("WM_DELETE_WINDOW", close_win)
        win.update_idletasks()
        x = (self.winfo_screenwidth() - win.winfo_width()) // 2
        y = max(0, (self.winfo_screenheight() - win.winfo_height()) // 2)
        win.geometry(f"+{x}+{y}")
        win.focus_force()

    def show_sponsor_dialog(self):
        """赞助作者：按屏幕自适应显示大图收款码（微信/支付宝切换，点击可放大）"""
        # 重复点击只把已有窗口提到前面，避免叠出一堆窗口
        if getattr(self, '_sponsor_dialog', None) is not None:
            try:
                if self._sponsor_dialog.winfo_exists():
                    self._sponsor_dialog.lift()
                    self._sponsor_dialog.focus_force()
                    return
            except Exception:
                pass
        self._sponsor_dialog = None
        p = self._dialog_palette()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = min(620, max(460, sw - 120))
        h = min(940, max(560, sh - 90))

        dlg = tk.Toplevel(self)
        dlg.title("赞助作者 - 视频下载器")
        dlg.geometry(f"{w}x{h}")
        dlg.minsize(440, 520)
        dlg.transient(self)
        dlg.configure(bg=p['bg'])
        self._sponsor_dialog = dlg

        head = tk.Frame(dlg, bg=p['bg'])
        head.pack(fill=tk.X, padx=24, pady=(20, 0))
        tk.Label(head, text="赞助作者", font=("微软雅黑", 17, "bold"),
                 bg=p['bg'], fg=p['text']).pack(anchor='w')
        tk.Label(head, text="如果这个工具帮到了你，欢迎扫码请作者喝杯咖啡",
                 font=("微软雅黑", 9), bg=p['bg'], fg=p['sub']).pack(anchor='w', pady=(6, 0))
        tk.Frame(dlg, bg=p['border'], height=1).pack(fill=tk.X, padx=24, pady=(12, 0))

        tab_row = tk.Frame(dlg, bg=p['bg'])
        tab_row.pack(fill=tk.X, padx=24, pady=(12, 0))
        body = tk.Frame(dlg, bg=p['card'], highlightbackground=p['border'],
                        highlightthickness=1)
        body.pack(fill=tk.BOTH, expand=True, padx=24, pady=(10, 0))
        img_holder = tk.Frame(body, bg=p['card'])
        img_holder.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        qr_files = (("微信支付", "wx.jpg"), ("支付宝", "zfb.jpg"))
        state = {'title': qr_files[0][0], 'file': qr_files[0][1],
                 'photo': None, 'label': None, 'fallback': None}
        tags = {}

        def render():
            """按当前可用区域重新缩放图片（响应窗口大小变化）"""
            try:
                if not dlg.winfo_exists():
                    return
                iw, ih = img_holder.winfo_width(), img_holder.winfo_height()
            except Exception:
                return
            if iw < 60 or ih < 60:
                iw, ih = w - 70, h - 300
            max_w, max_h = max(140, iw - 4), max(140, ih - 4)
            path = self._resource_path(state['file'])
            photo = None
            try:
                from PIL import Image, ImageTk
                if os.path.exists(path):
                    img = Image.open(path).convert("RGB")
                    img.thumbnail((max_w, max_h), Image.LANCZOS)
                    photo = ImageTk.PhotoImage(img)
            except Exception:
                photo = None
            if photo is None:
                if state['label'] is not None:
                    state['label'].pack_forget()
                if state['fallback'] is None:
                    state['fallback'] = tk.Label(
                        img_holder, bg=p['card'], fg=p['hint'], font=("微软雅黑", 10),
                        text=f"未能加载收款码图片\n{state['file']}")
                state['fallback'].pack(expand=True)
                return
            if state['fallback'] is not None:
                state['fallback'].pack_forget()
            state['photo'] = photo
            if state['label'] is None:
                state['label'] = tk.Label(img_holder, bg=p['card'], cursor='hand2')
                state['label'].pack(expand=True)
                state['label'].bind(
                    '<Button-1>',
                    lambda e: self._show_sponsor_qr(state['title'], state['file'], dlg))
            state['label'].config(image=photo)

        def select(title, filename):
            state['title'], state['file'] = title, filename
            for (t, _f), btn in tags.items():
                sel = (t == title)
                btn.config(bg=p['primary'] if sel else p['card'],
                           fg=p['primary_fg'] if sel else p['sub'],
                           activebackground=p['primary_dark'] if sel else p['border'],
                           activeforeground=p['primary_fg'] if sel else p['text'])
            render()

        for title, filename in qr_files:
            btn = tk.Button(tab_row, text=title, font=("微软雅黑", 10, "bold"),
                            relief=tk.FLAT, bd=0, cursor='hand2', padx=18, pady=7,
                            command=lambda t=title, f=filename: select(t, f))
            btn.pack(side=tk.LEFT, padx=(0, 8))
            tags[(title, filename)] = btn

        render_job = {'id': None}

        def on_configure(event):
            if render_job['id']:
                try:
                    dlg.after_cancel(render_job['id'])
                except Exception:
                    pass
            render_job['id'] = dlg.after(120, render)

        img_holder.bind('<Configure>', on_configure)

        footer = tk.Frame(dlg, bg=p['bg'])
        footer.pack(fill=tk.X, padx=24, pady=14)
        tk.Label(footer, text="感谢你的支持！", font=("微软雅黑", 9),
                 bg=p['bg'], fg=p['hint']).pack(side=tk.LEFT)

        def close_dlg():
            self._sponsor_dialog = None
            try:
                dlg.grab_release()
            except Exception:
                pass
            dlg.destroy()

        self._dialog_button(footer, "关闭", close_dlg, 'primary', width=8).pack(side=tk.RIGHT)
        dlg.bind('<Escape>', lambda e: close_dlg())
        dlg.protocol("WM_DELETE_WINDOW", close_dlg)

        select(*qr_files[0])
        dlg.update_idletasks()
        x = self.winfo_rootx() + max(0, (self.winfo_width() - dlg.winfo_width()) // 2)
        y = self.winfo_rooty() + max(0, (self.winfo_height() - dlg.winfo_height()) // 2)
        x = max(0, min(x, sw - dlg.winfo_width()))
        y = max(0, min(y, sh - dlg.winfo_height() - 40))
        dlg.geometry(f"+{x}+{y}")
        try:
            dlg.grab_set()
            dlg.focus_force()
        except Exception:
            pass

    def _show_sponsor_qr(self, title, filename, parent=None):
        """放大查看收款码（跟随主题，不超过屏幕，同一时间只保留一个）"""
        p = self._dialog_palette()
        old = getattr(self, '_sponsor_qr_win', None)
        if old is not None:
            try:
                if old.winfo_exists():
                    old.destroy()
            except Exception:
                pass
        self._sponsor_qr_win = None
        try:
            from PIL import Image, ImageTk
        except ImportError:
            return
        path = self._resource_path(filename)
        if not os.path.exists(path):
            return
        try:
            img = Image.open(path).convert("RGB")
        except Exception:
            return
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        img.thumbnail((int(sw * 0.86), int(sh * 0.82) - 40), Image.LANCZOS)
        photo = ImageTk.PhotoImage(img)
        win = tk.Toplevel(parent or self)
        win.title(f"{title} - 收款码")
        win.configure(bg=p['bg'])
        win.resizable(False, False)
        win.transient(parent or self)
        win._photo = photo
        self._sponsor_qr_win = win

        tk.Label(win, text=f"{title} · 扫码赞助", font=("微软雅黑", 13, "bold"),
                 bg=p['bg'], fg=p['text']).pack(pady=(14, 8))
        tk.Label(win, image=photo, bg=p['bg']).pack(padx=14)
        tk.Label(win, text="按 Esc 或点击下方按钮关闭", font=("微软雅黑", 9),
                 bg=p['bg'], fg=p['hint']).pack(pady=(8, 0))

        def close_win():
            self._sponsor_qr_win = None
            try:
                win.grab_release()
            except Exception:
                pass
            win.destroy()
            try:
                if parent is not None and parent.winfo_exists():
                    parent.grab_set()
                    parent.focus_force()
            except Exception:
                pass

        self._dialog_button(win, "关闭", close_win, 'primary', width=8).pack(pady=12)
        win.bind('<Escape>', lambda e: close_win())
        win.protocol("WM_DELETE_WINDOW", close_win)
        win.update_idletasks()
        x = (sw - win.winfo_width()) // 2
        y = max(0, (sh - win.winfo_height()) // 2)
        win.geometry(f"+{x}+{y}")
        try:
            win.grab_set()
            win.focus_force()
        except Exception:
            pass


    def show_welcome_dialog(self):
        """软件说明（v4.2：重写为简洁的使用指南）"""
        dialog = tk.Toplevel(self)
        dialog.title(f"使用说明 - 视频下载器 {APP_VERSION}")
        dialog.geometry("680x620")
        dialog.minsize(620, 540)
        dialog.transient(self)
        dialog.configure(bg='#ffffff')

        header = tk.Frame(dialog, bg='#1e88e5', height=58)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text=f"视频下载器 {APP_VERSION}",
                 font=("微软雅黑", 18, "bold"), fg='white', bg='#1e88e5').pack(side=tk.LEFT, padx=22, pady=14)
        tk.Label(header, text="网页嗅探 · yt-dlp · 直链下载",
                 font=("微软雅黑", 9), fg='#e3f2fd', bg='#1e88e5').pack(side=tk.LEFT, pady=(20, 0))

        content = tk.Frame(dialog, bg='#ffffff', padx=22, pady=12)
        content.pack(fill=tk.BOTH, expand=True)

        help_text = """一、它能做什么
• 网页视频：粘贴网页地址，自动用浏览器嗅探 + yt-dlp 解析；B 站、YouTube 等站点直接用 yt-dlp，速度更快
• 批量下载：粘贴 B 站合集 / 多P / 播放列表链接（支持一次多个），一次解析整个列表，勾选后整批下载
• 直链 / M3U8：地址以 .mp4、.m3u8、.webm 等结尾时，直接进入下载
• 微信视频号：粘贴 weixin.qq.com/sph/... 分享链接即可；首次使用需在弹出窗口登录腾讯元宝（QQ 或手机号，登录一次后自动复用）
• 支持 MP4、WebM、FLV、TS、M3U8、DASH；自动合并音视频、AES-128 解密、字幕、仅提取音频
• 需要登录的视频：用「Cookie 文件」导入浏览器导出的 cookies.txt（Netscape 格式）

二、下载步骤
1. 粘贴地址，点击「开始」（普通网页会自动嗅探，通常 3~8 秒；B 站/YouTube 通常 2~5 秒）
2. 在资源列表选一个清晰度：yt-dlp 行会直接标出 1080p、720p、仅音频等
3. 点击「下载选中视频」，选择保存位置；中途可随时点击「停止」
4. 合集/多P：切到「批量下载」页，粘贴链接（每行一个），点「解析列表」，勾选后点「开始批量下载」；带 ?p=7 的链接也会从第 1 P 展开整部合集

三、界面说明
• 资源列表：格式 / 清晰度 / 大小 / 来源 / 视频说明
• 批量下载页：勾选「选择」列可选择/取消单个视频，配合「全选 / 全不选 / 反选」；文件名自动加 001、002 序号，保持合集顺序
• 批量下载页：可设置清晰度、保存目录，并默认「跳过已存在文件」，中断后再次下载不会重复占用带宽
• 格式列显示真实容器：普通网页为 MP4/M3U8，B 站视频轨为 MP4（DASH 实际为 m4s），音频轨为 M4A
• B 站下载时会自动把 MP4 视频轨和 M4A 音频轨合并；想只保存音频请选「仅音频」行，或在批量页选择「仅音频 (MP3)」
• yt-dlp 行：来源显示 yt-dlp，适合 YouTube、Bilibili、TikTok 等 1000+ 站点
• 浏览器行：来源显示「网络」或「DOM」，适合普通网页播放器；M3U8 会自动合并分片
• 运行日志：位于列表下方；拖动中间的分隔条可调整高度，点「隐藏日志」可收起
• 保存文件名：默认使用网页标题或视频标题，可在保存对话框中修改
• 下载历史：双击一条记录可查看文件大小、保存位置、下载 URL、页面 URL、页面标题、浏览器、添加时间等信息
• 剪贴板监听：默认关闭；点击主界面工具行的「剪贴板监听：关」或设置页勾选后开启，复制网页/视频链接会置顶主窗口并询问是否下载
• 点击窗口关闭按钮会询问：最小化到托盘/任务栏，或退出程序
• 遇到问题想反馈，点击顶部「加入群聊」扫码进群（QQ / Telegram / GitHub）
• 如果工具帮到了你，可点击顶部「赞助作者」查看微信/支付宝收款码

四、常见问题
• 下载失败：先在「系统设置」确认 FFmpeg 路径正确
• 嗅探不到：关闭「无头模式」后手动播放/登录，或导入 Cookie 文件后重试
• 需要会员/登录：导入 Cookie 文件；受 DRM 保护的内容无法下载
• 批量解析到的标题为空/不完整：部分合集接口不返回分P标题，程序会自动查询 B 站分P信息；仍失败时下载完成后可在文件管理器里批量重命名
• 视频没有声音：DASH 资源会自动合并音轨，若失败请检查 FFmpeg 是否可用
• M3U8 分片报 SSL EOF/连接断开：CDN 可能限制并发或校验浏览器 TLS。程序默认 16 线程，失败后会自动降到 4 线程重试，并尝试 HTTP/免校验回退；仍失败请用 Cookie/代理，或重新嗅探获取新地址
• M3U8 默认使用本地 16 线程分片下载（比 FFmpeg 单连接快），只有本地失败才回退 FFmpeg
• 请只下载自己有权保存的内容，尊重版权

五、免责声明
本工具仅用于个人学习、备份自有版权内容。用户应自行承担使用本工具产生的法律责任；软件不提供、不存储、不分发任何受版权保护的影音资源。"""

        text_frame = tk.Frame(content, bg='#ffffff')
        text_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
        text_widget = tk.Text(text_frame, wrap=tk.WORD, bg='#fafafa', fg='#1a237e',
                              font=("微软雅黑", 10), height=16, borderwidth=1,
                              relief='solid', padx=12, pady=10, spacing1=2, spacing3=4)
        text_scroll = ttk.Scrollbar(text_frame, orient=tk.VERTICAL, command=text_widget.yview)
        text_widget.configure(yscrollcommand=text_scroll.set)
        text_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        text_widget.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        text_widget.insert(tk.END, help_text)
        text_widget.yview_moveto(0.0)
        text_widget.config(state=tk.DISABLED)

        footer = tk.Frame(content, bg='#ffffff')
        footer.pack(fill=tk.X)
        if hasattr(self, 'show_welcome_var'):
            ttk.Checkbutton(footer, text="启动时显示本说明",
                            variable=self.show_welcome_var).pack(side=tk.LEFT)

        def confirm_ok():
            if hasattr(self, 'show_welcome_var'):
                self.settings['show_welcome'] = bool(self.show_welcome_var.get())
                self._save_config()
            dialog.destroy()

        ttk.Button(footer, text="开始使用", style='Primary.TButton',
                   command=confirm_ok).pack(side=tk.RIGHT)
        dialog.protocol("WM_DELETE_WINDOW", confirm_ok)

        dialog.update_idletasks()
        x = (dialog.winfo_screenwidth() - dialog.winfo_width()) // 2
        y = max(0, (dialog.winfo_screenheight() - dialog.winfo_height()) // 2)
        dialog.geometry(f"+{x}+{y}")
        try:
            dialog.grab_set()
        except Exception:
            pass


    def _setup_styles(self):
        self.style = ttk.Style(self)
        self.style.theme_use('default')
        self.configure(bg='#ffffff')
        self.style.configure('.', font=('微软雅黑', 10))

        self.style.configure('Primary.TButton',
            background='#1e88e5', foreground='white', borderwidth=0,
            padding=(20, 10), font=('微软雅黑', 10, 'bold'), relief='flat')
        self.style.map('Primary.TButton',
            background=[('pressed', '#1565c0'), ('active', '#1976d2')])

        # 顶部蓝色栏上的浅色按钮
        self.style.configure('Header.TButton',
            background='#ffffff', foreground='#1565c0', borderwidth=0,
            padding=(9, 5), font=('微软雅黑', 9), relief='flat')
        self.style.map('Header.TButton',
            background=[('active', '#e3f2fd'), ('pressed', '#bbdefb')],
            foreground=[('disabled', '#90caf9')])

        # 工具行小按钮
        self.style.configure('Tool.TButton',
            background='#ffffff', foreground='#1e88e5', borderwidth=1,
            bordercolor='#dbe7f5', padding=(9, 4),
            font=('微软雅黑', 9), relief='solid')
        self.style.map('Tool.TButton',
            background=[('active', '#e3f2fd'), ('pressed', '#d6e9fb')],
            bordercolor=[('active', '#90caf9')])

        # 扁平文字按钮（工具行）
        self.style.configure('Flat.TButton',
            background='#ffffff', foreground='#1e88e5', borderwidth=0,
            padding=(9, 5), font=('微软雅黑', 9), relief='flat')
        self.style.map('Flat.TButton',
            background=[('active', '#e8f1fe'), ('pressed', '#d6e9fb')],
            foreground=[('active', '#1565c0')])

        self.style.configure('Secondary.TButton',
            background='white', foreground='#1e88e5', borderwidth=1,
            bordercolor='#1e88e5', padding=(18, 8),
            font=('微软雅黑', 10), relief='solid')
        self.style.map('Secondary.TButton',
            background=[('pressed', '#e3f2fd'), ('active', '#f5f9ff')])

        self.style.configure('Modern.TEntry',
            fieldbackground='white', foreground='#1a237e', borderwidth=1,
            bordercolor='#cccccc', padding=5, font=('微软雅黑', 10), relief='solid')
        self.style.map('Modern.TEntry',
            bordercolor=[('focus', '#1e88e5'), ('!focus', '#cccccc')])
        self.style.configure('TCombobox',
            fieldbackground='#f5f9ff', foreground='#1a237e', background='#ffffff',
            arrowcolor='#1e88e5', bordercolor='#cccccc', padding=3,
            font=('微软雅黑', 9), relief='solid')
        # ttk.Combobox 内部编辑框使用 TEntry 样式，单独配置避免深色下变白
        self.style.configure('TEntry',
            fieldbackground='#f5f9ff', foreground='#1a237e',
            insertcolor='#1a237e', bordercolor='#cccccc')
        self.style.map('TCombobox',
            fieldbackground=[('readonly', '#f5f9ff'), ('focus', '#f5f9ff'),
                             ('active', '#f5f9ff'), ('disabled', '#ffffff')],
            foreground=[('readonly', '#1a237e'), ('focus', '#1a237e'),
                        ('active', '#1a237e')],
            selectbackground=[('readonly', '#1e88e5')],
            selectforeground=[('readonly', 'white')])

        self.style.configure('Custom.TNotebook', background='#ffffff')
        self.style.configure('Custom.TNotebook.Tab',
            background='#f5f5f5', foreground='#666666',
            padding=[20, 9, 20, 9], font=('微软雅黑', 10))
        self.style.map('Custom.TNotebook.Tab',
            background=[('selected', '#ffffff'), ('active', '#eeeeee')],
            foreground=[('selected', '#1e88e5'), ('active', '#333333')])

        self.style.configure('Blue.Horizontal.TProgressbar',
            troughcolor='#e8eef7', background='#1e88e5',
            borderwidth=0, thickness=8)

        self.style.configure('Resource.Treeview',
            background='white', foreground='#1a237e', rowheight=26,
            fieldbackground='white', font=('微软雅黑', 9))
        self.style.configure('Resource.Treeview.Heading',
            background='#f5f9ff', foreground='#1a237e', padding=(8, 6),
            font=('微软雅黑', 9, 'bold'))

    def _create_main_frame(self):
        self.header_frame = tk.Frame(self, bg=self.colors['primary'], height=60)
        self.header_frame.pack(fill=tk.X)
        self.header_frame.pack_propagate(False)

        title_label = tk.Label(self.header_frame, text="视频下载器",
            font=("微软雅黑", 18, "bold"), fg='white', bg=self.colors['primary'])
        title_label.pack(side=tk.LEFT, padx=(24, 8), pady=13)

        sub_label = tk.Label(self.header_frame, text=f"网页嗅探 · yt-dlp · 直链下载  {APP_VERSION}",
            font=("微软雅黑", 9), fg='#e3f2fd', bg=self.colors['primary'])
        sub_label.pack(side=tk.LEFT, pady=(20, 0))

        self.theme_btn = ttk.Button(self.header_frame,
                                    text="浅色模式" if self.is_dark_theme else "深色模式",
                                    style='Header.TButton', command=self._toggle_theme, width=12)
        self.theme_btn.pack(side=tk.RIGHT, padx=(6, 18), pady=14)
        self.help_btn = ttk.Button(self.header_frame, text="使用说明",
                                   style='Header.TButton', command=self.show_welcome_dialog, width=9)
        self.help_btn.pack(side=tk.RIGHT, padx=6, pady=14)
        self.sponsor_btn = ttk.Button(self.header_frame, text="赞助作者",
                                      style='Header.TButton', command=self.show_sponsor_dialog,
                                      width=9)
        self.sponsor_btn.pack(side=tk.RIGHT, padx=6, pady=14)
        self.group_btn = ttk.Button(self.header_frame, text="加入群聊",
                                    style='Header.TButton', command=self.show_group_dialog,
                                    width=9)
        self.group_btn.pack(side=tk.RIGHT, padx=6, pady=14)

        self.header_line = tk.Frame(self, bg=self.colors['primary_dark'], height=2)
        self.header_line.pack(fill=tk.X)

    def create_notebook(self):
        self.notebook = ttk.Notebook(self, style='Custom.TNotebook')
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=15, pady=15)

        self.download_frame = tk.Frame(self.notebook, bg=self.colors['bg_card'])
        self.notebook.add(self.download_frame, text="  视频下载  ")
        self.create_download_page()

        self.batch_frame = tk.Frame(self.notebook, bg=self.colors['bg_card'])
        self.notebook.add(self.batch_frame, text="  批量下载  ")
        self.create_batch_page()

        self.settings_frame = tk.Frame(self.notebook, bg=self.colors['bg_card'])
        self.notebook.add(self.settings_frame, text="  系统设置  ")
        self.create_settings_page()

        self.history_frame = tk.Frame(self.notebook, bg=self.colors['bg_card'])
        self.notebook.add(self.history_frame, text="  下载历史  ")
        self.create_history_page()

        # 切换标签页时同步控件状态
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    def _on_tab_changed(self, event):
        """切换标签页回调（保留扩展点，不强制覆盖嗅探页用户选择）"""
        pass

    def create_download_page(self):
        """视频下载标签页（v4.2：统一资源列表 + 自动嗅探/yt-dlp + 可折叠日志）"""
        c = self.colors
        main = tk.Frame(self.download_frame, bg=c['bg_card'], padx=16, pady=14,
                        highlightbackground=c['border'], highlightthickness=1)
        main.pack(fill=tk.BOTH, expand=True, padx=14, pady=12)

        # ---------- 地址卡片：输入 + 单一开始/停止按钮 ----------
        self.url_card = tk.Frame(main, bg=c['bg'], highlightbackground=c['border'],
                                 highlightthickness=1, bd=0)
        self.url_card.pack(fill=tk.X)
        self.url_card_inner = tk.Frame(self.url_card, bg=c['bg'], padx=12, pady=10)
        self.url_card_inner.pack(fill=tk.X)

        self.url_link_lbl = tk.Label(self.url_card_inner, text="🔗  网页 / 视频地址",
                                     font=("微软雅黑", 9), bg=c['bg'], fg=c['text_light'])
        self.url_link_lbl.pack(side=tk.LEFT, padx=(0, 10))
        self.url_entry = ttk.Entry(self.url_card_inner, style='Modern.TEntry')
        self.url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))
        self.url_entry.bind('<Return>', lambda e: self._on_main_action())
        # 兼容旧引用：三种模式共用同一个输入框
        self.direct_url_entry = self.url_entry
        self.ydlp_url_entry = self.url_entry

        # 单一开始/停止按钮：空闲时显示「开始」，任务中显示「停止」
        self.main_btn = tk.Button(self.url_card_inner, text="开始",
                                  font=("微软雅黑", 10, "bold"),
                                  bg=c['primary'], fg='white',
                                  activebackground=c['primary_dark'],
                                  activeforeground='white', disabledforeground='#ffffff',
                                  relief=tk.FLAT, cursor='hand2', bd=0,
                                  width=16, pady=6, command=self._on_main_action)
        self.main_btn.pack(side=tk.LEFT)
        # 兼容旧引用
        self.sniff_toggle_btn = self.main_btn
        self.direct_download_btn = self.main_btn
        self.ydlp_parse_btn = self.main_btn
        self.direct_stop_btn = self.main_btn

        # ---------- 次级工具行（扁平文字按钮） ----------
        tool_row = tk.Frame(main, bg=c['bg_card'])
        tool_row.pack(fill=tk.X, pady=(8, 0))
        for text, cmd in [("📥 导入 M3U8", self._import_local_m3u8),
                          ("🍪 Cookie 文件", self._ydlp_browse_cookie),
                          ("🧹 清空列表", self.clear_resource_list)]:
            ttk.Button(tool_row, text=text, style='Flat.TButton',
                       command=cmd).pack(side=tk.LEFT, padx=(0, 4))
        self.ydlp_cookie_lbl = tk.Label(tool_row, text="", font=("微软雅黑", 8),
                                        fg=c['text_light'], bg=c['bg_card'])
        self.ydlp_cookie_lbl.pack(side=tk.LEFT, padx=(2, 0))
        self.clipboard_btn = ttk.Button(tool_row, text="剪贴板监听：关",
                                        style='Flat.TButton',
                                        command=self._toggle_clipboard_from_toolbar)
        self.clipboard_btn.pack(side=tk.LEFT, padx=(6, 0))
        self.log_toggle_btn = ttk.Button(tool_row, text="📜 隐藏日志", style='Flat.TButton',
                                         command=self._toggle_log_panel)
        self.log_toggle_btn.pack(side=tk.RIGHT)

        self.top_sep = tk.Frame(main, bg=c['border'], height=1)
        self.top_sep.pack(fill=tk.X, pady=(9, 0))

        # ---------- 资源操作栏 ----------
        bar = tk.Frame(main, bg=c['bg_card'])
        bar.pack(fill=tk.X, pady=(8, 5))
        self.status_dot = tk.Label(bar, text="●", font=("Segoe UI", 10),
                                   bg=c['bg_card'], fg=c['success'])
        self.status_dot.pack(side=tk.LEFT)
        tk.Label(bar, text="资源", font=("微软雅黑", 10, "bold"),
                 bg=c['bg_card'], fg=c['text']).pack(side=tk.LEFT, padx=(4, 0))
        self.resource_count_lbl = tk.Label(bar, text="共 0 个", font=("微软雅黑", 8),
                                           bg='#e8f1fe', fg=c['primary'], padx=7, pady=1)
        self.resource_count_lbl.pack(side=tk.LEFT, padx=6)
        self.status_var = tk.StringVar(value="就绪")
        tk.Label(bar, textvariable=self.status_var, font=("微软雅黑", 9),
                 bg=c['bg_card'], fg=c['text_light'], anchor=tk.W).pack(side=tk.LEFT, padx=4)
        self.clear_queue_link = tk.Label(bar, text="清空队列", font=("微软雅黑", 9),
                                         bg=c['bg_card'], fg=c['text_light'], cursor='arrow')
        self.clear_queue_link.pack(side=tk.RIGHT, padx=(8, 0))
        self.clear_queue_link.bind('<Button-1>', lambda e: self._clear_queue())
        self.download_toggle_btn = tk.Button(bar, text="↓ 下载选中视频",
                                             font=("微软雅黑", 10, "bold"),
                                             bg=c['primary'], fg='white',
                                             activebackground=c['primary_dark'],
                                             activeforeground='white',
                                             disabledforeground='#ffffff',
                                             relief=tk.FLAT, cursor='hand2', bd=0,
                                             padx=16, pady=5,
                                             command=self._toggle_download, state=tk.DISABLED)
        self.download_toggle_btn.pack(side=tk.RIGHT)

        # ---------- 主体：资源列表 + 可折叠日志 ----------
        # 使用经典 tk.PanedWindow：细窄浅色分隔条，可拖动但不抢眼
        body = tk.PanedWindow(main, orient=tk.VERTICAL, sashwidth=8,
                              sashrelief=tk.FLAT, sashpad=0, bd=0,
                              bg=c.get('sash', '#b7c5d8'), showhandle=True,
                              handlesize=8, handlepad=1)
        body.pack(fill=tk.BOTH, expand=True)
        self._body_paned = body

        table_frame = tk.Frame(body, bg=c['bg_card'],
                               highlightbackground=c['border'], highlightthickness=1)
        body.add(table_frame, stretch='always', minsize=160)
        self.view_container = table_frame

        log_frame = tk.Frame(body, bg=c['bg_card'])
        body.add(log_frame, stretch='never', minsize=80, height=120)
        self.log_frame = log_frame
        self._log_pane_visible = True

        log_head = tk.Frame(log_frame, bg=c['bg_card'])
        log_head.pack(fill=tk.X, pady=(3, 2))
        tk.Label(log_head, text="运行日志", font=("微软雅黑", 9, "bold"),
                 bg=c['bg_card'], fg=c['text']).pack(side=tk.LEFT)
        tk.Label(log_head, text="拖动上方分隔条调整高度", font=("微软雅黑", 8),
                 bg=c['bg_card'], fg=c['text_light']).pack(side=tk.LEFT, padx=8)
        ttk.Button(log_head, text="清空", style='Flat.TButton',
                   command=self._clear_log).pack(side=tk.RIGHT)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=4, font=("Consolas", 9),
                                                  bg='#171b26', fg='#d7dce5',
                                                  insertbackground='#d7dce5',
                                                  selectbackground=c['primary_dark'],
                                                  selectforeground='white',
                                                  relief=tk.FLAT, borderwidth=0)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        # 兼容旧引用：所有日志共用一个文本框
        self.direct_log = self.log_text
        self.ydlp_log = self.log_text

        # 资源列表
        columns = ("type", "resolution", "size", "source", "detail")
        self.resource_tree = ttk.Treeview(table_frame, columns=columns,
                                        style='Resource.Treeview', show='headings',
                                        selectmode='extended')
        col_cfg = [("type", "格式", 80, tk.CENTER, False),
                   ("resolution", "清晰度", 90, tk.CENTER, False),
                   ("size", "大小", 90, tk.CENTER, False),
                   ("source", "来源", 80, tk.CENTER, False),
                   ("detail", "视频 / 地址", 520, tk.W, True)]
        for cid, text, width, anchor, stretch in col_cfg:
            self.resource_tree.heading(cid, text=text)
            self.resource_tree.column(cid, width=width, anchor=anchor, stretch=stretch)
        vsb = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.resource_tree.yview)
        hsb = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL, command=self.resource_tree.xview)
        self.resource_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self.resource_tree.pack(fill=tk.BOTH, expand=True)
        self.resource_tree.bind('<<TreeviewSelect>>', self.on_resource_select)
        self.resource_tree.bind('<Button-3>', self._on_resource_right_click)
        self.resource_tree.bind('<Double-1>', self._on_resource_double_click)

        self.resource_empty = tk.Frame(table_frame, bg=c['bg_card'])
        tk.Label(self.resource_empty, text="🎬", font=("Segoe UI Emoji", 38),
                 bg=c['bg_card'], fg=c['text_light']).pack(pady=(34, 4))
        tk.Label(self.resource_empty, text="粘贴网页或视频地址，点击「开始」",
                 font=("微软雅黑", 12), bg=c['bg_card'], fg=c['text_light']).pack()
        tk.Label(self.resource_empty, text="自动嗅探网页 · yt-dlp 支持站点 · m3u8/直链直接下载",
                 font=("微软雅黑", 9), bg=c['bg_card'], fg=c['text_light']).pack(pady=4)
        self.resource_empty.place(relx=0.5, rely=0.5, anchor=tk.CENTER)

        # 进度条
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(main, variable=self.progress_var,
                                            style='Blue.Horizontal.TProgressbar', maximum=100)
        self.progress_bar.pack(fill=tk.X, pady=(5, 0))
        self.direct_progress = self.progress_bar
        self.ydlp_progress = self.progress_bar
        self._refresh_clipboard_button()

        if not YTDLP_AVAILABLE:
            self._log_message("yt-dlp 未安装，网页解析将仅依赖浏览器嗅探", "WARNING")
        else:
            self._log_message("就绪：粘贴网页或视频地址，点击「开始」", "INFO")


    def _show_dl_view(self, mode):
        """v4.2 起只有统一的资源列表，保留该接口兼容旧调用"""
        self._dl_mode = mode
        return

    def _on_main_action(self):
        """单一开始/停止按钮：空闲时开始，任务中停止"""
        if self.is_sniffing or self.is_downloading:
            if not self._stop_requested:
                self.stop_current_task()
            return
        if self.batch_parsing or self.batch_downloading:
            messagebox.showwarning("提示", "批量下载任务正在进行，请先等待完成或点击批量下载页的「停止」")
            return
        self._dl_click_start()

    def _dl_click_start(self):
        """开始按钮：直链直接下载，网页统一嗅探（浏览器 + yt-dlp 自动并行）"""
        if self.is_sniffing or self.is_downloading:
            return
        url = self.url_entry.get().strip()
        if not url:
            messagebox.showwarning("提示", "请输入网页或视频地址")
            return
        if not url.lower().startswith(('http://', 'https://')):
            messagebox.showerror("错误", "请输入有效的网页地址（以 http/https 开头）")
            return
        low = url.lower()
        video_exts = ('.mp4', '.ts', '.flv', '.webm', '.mkv', '.mov', '.avi',
                      '.m4v', '.mp3', '.m4a', '.aac', '.wav')
        if '.m3u8' in low or low.split('?')[0].endswith(video_exts):
            self.start_direct_download()
            return
        self.start_sniff()


    # ==================== 批量下载（v4.3） ====================
    def create_batch_page(self):
        """批量下载标签页：B站合集 / 多P / 播放列表一次解析、整批下载"""
        c = self.colors
        main = tk.Frame(self.batch_frame, bg=c['bg_card'], padx=16, pady=14,
                        highlightbackground=c['border'], highlightthickness=1)
        main.pack(fill=tk.BOTH, expand=True, padx=14, pady=12)

        # ---------- 标题 ----------
        head = tk.Frame(main, bg=c['bg_card'])
        head.pack(fill=tk.X)
        self.batch_title_lbl = tk.Label(head, text="批量下载", font=("微软雅黑", 16, "bold"),
                                        bg=c['bg_card'], fg=c['text'])
        self.batch_title_lbl.pack(side=tk.LEFT)
        self.batch_hint_lbl = tk.Label(
            head, text="B站合集 / 多P / 播放列表 · 一次解析，整批下载",
            font=("微软雅黑", 9), bg=c['bg_card'], fg=c['text_light'])
        self.batch_hint_lbl.pack(side=tk.LEFT, padx=(12, 0))

        # ---------- 地址输入卡片 ----------
        self.batch_input_card = tk.Frame(main, bg=c['bg'], highlightbackground=c['border'],
                                         highlightthickness=1, bd=0)
        self.batch_input_card.pack(fill=tk.X, pady=(10, 0))
        self.batch_input_inner = tk.Frame(self.batch_input_card, bg=c['bg'], padx=12, pady=10)
        self.batch_input_inner.pack(fill=tk.X)
        self.batch_url_lbl = tk.Label(
            self.batch_input_inner, text="🔗  合集 / 播放列表 / 视频地址（每行一个）",
            font=("微软雅黑", 9), bg=c['bg'], fg=c['text_light'])
        self.batch_url_lbl.pack(anchor=tk.W, pady=(0, 6))

        text_row = tk.Frame(self.batch_input_inner, bg=c['bg'])
        text_row.pack(fill=tk.X)
        self.batch_url_text = tk.Text(
            text_row, height=3, wrap=tk.WORD, font=("微软雅黑", 9),
            bg=c['bg'], fg=c['text'], insertbackground=c['text'],
            selectbackground=c['primary'], selectforeground='white',
            relief=tk.FLAT, bd=0, padx=8, pady=6)
        url_sb = ttk.Scrollbar(text_row, orient=tk.VERTICAL, command=self.batch_url_text.yview)
        self.batch_url_text.configure(yscrollcommand=url_sb.set)
        url_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.batch_url_text.pack(side=tk.LEFT, fill=tk.X, expand=True)

        action_row = tk.Frame(self.batch_input_inner, bg=c['bg'])
        action_row.pack(fill=tk.X, pady=(8, 0))
        self.batch_parse_btn = tk.Button(
            action_row, text="解析列表", font=("微软雅黑", 10, "bold"),
            bg=c['primary'], fg='white', activebackground=c['primary_dark'],
            activeforeground='white', disabledforeground='#ffffff',
            relief=tk.FLAT, cursor='hand2', bd=0, padx=18, pady=5,
            command=self.start_batch_parse)
        self.batch_parse_btn.pack(side=tk.LEFT)
        ttk.Button(action_row, text="从剪贴板粘贴", style='Flat.TButton',
                   command=self._batch_paste_clipboard).pack(side=tk.LEFT, padx=(8, 0))
        self.batch_input_tip = tk.Label(
            action_row, text="提示：B站链接带 ?p=7 也会从第 1 P 展开整部合集",
            font=("微软雅黑", 8), bg=c['bg'], fg=c['text_light'])
        self.batch_input_tip.pack(side=tk.LEFT, padx=10)

        # ---------- 下载选项 ----------
        opt = tk.Frame(main, bg=c['bg_card'])
        opt.pack(fill=tk.X, pady=(10, 6))
        tk.Label(opt, text="清晰度", font=("微软雅黑", 9),
                 bg=c['bg_card'], fg=c['text']).pack(side=tk.LEFT)
        self.batch_quality_var = tk.StringVar(
            value=self.settings.get("batch_quality") or "自动最佳")
        self.batch_quality_combo = ttk.Combobox(
            opt, textvariable=self.batch_quality_var, state='readonly', width=14,
            values=["自动最佳", "1080P 优先", "720P 优先", "480P 优先", "仅音频 (MP3)"])
        self.batch_quality_combo.pack(side=tk.LEFT, padx=(6, 16))
        self.batch_skip_var = tk.BooleanVar(
            value=bool(self.settings.get("batch_skip_existing", True)))
        self.batch_skip_chk = ttk.Checkbutton(opt, text="跳过已存在文件",
                                              variable=self.batch_skip_var)
        self.batch_skip_chk.pack(side=tk.LEFT, padx=(0, 16))
        tk.Label(opt, text="保存到", font=("微软雅黑", 9),
                 bg=c['bg_card'], fg=c['text']).pack(side=tk.LEFT)
        default_dir = (self.settings.get("batch_save_dir")
                       or getattr(self, '_last_save_dir', '')
                       or os.path.join(os.path.expanduser('~'), 'Downloads'))
        if not os.path.isdir(default_dir):
            default_dir = os.path.expanduser('~')
        self.batch_dir_var = tk.StringVar(value=default_dir)
        self.batch_dir_entry = ttk.Entry(opt, textvariable=self.batch_dir_var,
                                         style='Modern.TEntry')
        self.batch_dir_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 6))
        ttk.Button(opt, text="浏览", style='Flat.TButton',
                   command=self._batch_browse_dir).pack(side=tk.LEFT)
        ttk.Button(opt, text="打开目录", style='Flat.TButton',
                   command=self._batch_open_dir).pack(side=tk.LEFT, padx=(4, 0))

        # ---------- 列表操作栏 ----------
        bar = tk.Frame(main, bg=c['bg_card'])
        bar.pack(fill=tk.X, pady=(4, 4))
        self.batch_status_dot = tk.Label(bar, text="●", font=("Segoe UI", 10),
                                         bg=c['bg_card'], fg=c['text_light'])
        self.batch_status_dot.pack(side=tk.LEFT)
        self.batch_status_var = tk.StringVar(value="等待解析")
        self.batch_status_lbl = tk.Label(bar, textvariable=self.batch_status_var,
                                         font=("微软雅黑", 9), bg=c['bg_card'],
                                         fg=c['text_light'])
        self.batch_status_lbl.pack(side=tk.LEFT, padx=(4, 10))
        self.batch_count_lbl = tk.Label(bar, text="共 0 个 · 已选 0 个",
                                        font=("微软雅黑", 8), bg=c['bg_card'],
                                        fg=c['text_light'])
        self.batch_count_lbl.pack(side=tk.LEFT)
        ttk.Button(bar, text="反选", style='Flat.TButton',
                   command=self._batch_invert_selection).pack(side=tk.RIGHT, padx=(4, 0))
        for text, value in (("全不选", False), ("全选", True)):
            ttk.Button(bar, text=text, style='Flat.TButton',
                       command=lambda v=value: self._batch_select_all(v)
                       ).pack(side=tk.RIGHT, padx=(4, 0))

        # ---------- 条目列表 ----------
        table_frame = tk.Frame(main, bg=c['bg_card'],
                               highlightbackground=c['border'], highlightthickness=1)
        table_frame.pack(fill=tk.BOTH, expand=True)
        self.batch_tree = ttk.Treeview(
            table_frame, columns=("sel", "idx", "title", "duration", "status"),
            style='Resource.Treeview', show='headings', selectmode='none')
        col_cfg = [("sel", "选择", 52, tk.CENTER, False),
                   ("idx", "序号", 56, tk.CENTER, False),
                   ("title", "标题", 640, tk.W, True),
                   ("duration", "时长", 80, tk.CENTER, False),
                   ("status", "状态", 150, tk.CENTER, False)]
        for cid, title, width, anchor, stretch in col_cfg:
            self.batch_tree.heading(cid, text=title)
            self.batch_tree.column(cid, width=width, anchor=anchor, stretch=stretch)
        vsb = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.batch_tree.yview)
        hsb = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL, command=self.batch_tree.xview)
        self.batch_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self.batch_tree.pack(fill=tk.BOTH, expand=True)
        self.batch_tree.bind('<Button-1>', self._batch_on_tree_click)

        self.batch_empty = tk.Frame(table_frame, bg=c['bg_card'])
        tk.Label(self.batch_empty, text="📚", font=("Segoe UI Emoji", 38),
                 bg=c['bg_card'], fg=c['text_light']).pack(pady=(34, 4))
        self.batch_empty_title = tk.Label(
            self.batch_empty, text="粘贴B站合集 / 多P / 播放列表地址，点击「解析列表」",
            font=("微软雅黑", 12), bg=c['bg_card'], fg=c['text_light'])
        self.batch_empty_title.pack()
        self.batch_empty_tip = tk.Label(
            self.batch_empty,
            text="解析后勾选需要的视频，一键加入批量下载；支持一次粘贴多个链接",
            font=("微软雅黑", 9), bg=c['bg_card'], fg=c['text_light'])
        self.batch_empty_tip.pack(pady=4)
        self.batch_empty.place(relx=0.5, rely=0.5, anchor=tk.CENTER)

        # ---------- 进度 + 开始/停止 ----------
        prog_row = tk.Frame(main, bg=c['bg_card'])
        prog_row.pack(fill=tk.X, pady=(8, 0))
        self.batch_progress_var = tk.DoubleVar(value=0)
        self.batch_progress_bar = ttk.Progressbar(
            prog_row, variable=self.batch_progress_var, maximum=100,
            style='Blue.Horizontal.TProgressbar')
        self.batch_progress_bar.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.batch_progress_lbl = tk.Label(prog_row, text="0 / 0", width=12,
                                           font=("微软雅黑", 9), bg=c['bg_card'],
                                           fg=c['text_light'])
        self.batch_progress_lbl.pack(side=tk.LEFT, padx=(8, 10))
        self.batch_stop_btn = tk.Button(
            prog_row, text="停止", font=("微软雅黑", 10, "bold"),
            bg=c['error'], fg='white', activebackground='#c0392b',
            activeforeground='white', disabledforeground='#ffffff',
            relief=tk.FLAT, cursor='hand2', bd=0, padx=16, pady=5,
            state=tk.DISABLED, command=self.stop_batch)
        self.batch_stop_btn.pack(side=tk.RIGHT)
        self.batch_start_btn = tk.Button(
            prog_row, text="↓ 开始批量下载", font=("微软雅黑", 10, "bold"),
            bg=c['primary'], fg='white', activebackground=c['primary_dark'],
            activeforeground='white', disabledforeground='#ffffff',
            relief=tk.FLAT, cursor='hand2', bd=0, padx=16, pady=5,
            state=tk.DISABLED, command=self.start_batch_download)
        self.batch_start_btn.pack(side=tk.RIGHT, padx=(0, 8))

        # ---------- 日志 ----------
        log_head = tk.Frame(main, bg=c['bg_card'])
        log_head.pack(fill=tk.X, pady=(8, 2))
        self.batch_log_lbl = tk.Label(log_head, text="批量日志", font=("微软雅黑", 9, "bold"),
                                      bg=c['bg_card'], fg=c['text'])
        self.batch_log_lbl.pack(side=tk.LEFT)
        ttk.Button(log_head, text="清空", style='Flat.TButton',
                   command=self._batch_clear_log).pack(side=tk.RIGHT)
        self.batch_log_text = scrolledtext.ScrolledText(
            main, height=5, font=("Consolas", 9), bg='#171b26', fg='#d7dce5',
            insertbackground='#d7dce5', selectbackground=c['primary_dark'],
            selectforeground='white', relief=tk.FLAT, borderwidth=0)
        self.batch_log_text.pack(fill=tk.X)
        self.batch_log_text.tag_config('INFO', foreground='#d7dce5')
        self.batch_log_text.tag_config('WARNING', foreground='#ffcc80')
        self.batch_log_text.tag_config('ERROR', foreground='#ff8a80')

        if not YTDLP_AVAILABLE:
            self._batch_log("yt-dlp 未安装，批量下载不可用。请执行: pip install yt-dlp", "ERROR")

    # ---------- 批量下载：输入与选择 ----------
    def _batch_paste_clipboard(self):
        """从剪贴板提取链接并填入输入框（兼容B站分享文案）"""
        try:
            text = self.clipboard_get()
        except Exception:
            return
        urls = re.findall(r'https?://[^\s，。；、）)\]]+', text or '')
        if not urls:
            self._batch_log("剪贴板中没有找到链接", "WARNING")
            return
        current = self.batch_url_text.get('1.0', tk.END).strip()
        merged = "\n".join(urls)
        if current:
            merged = current + "\n" + merged
        self.batch_url_text.delete('1.0', tk.END)
        self.batch_url_text.insert('1.0', merged)
        self._batch_log(f"已从剪贴板粘贴 {len(urls)} 个链接", "INFO")

    def _batch_browse_dir(self):
        init_dir = self.batch_dir_var.get().strip() or os.path.expanduser('~')
        folder = filedialog.askdirectory(initialdir=init_dir, title="选择批量下载保存目录")
        if folder:
            self.batch_dir_var.set(folder)
            self._last_save_dir = folder
            self.settings['batch_save_dir'] = folder
            self._save_config()

    def _batch_open_dir(self):
        folder = self.batch_dir_var.get().strip()
        if folder and os.path.isdir(folder):
            try:
                os.startfile(folder)
            except Exception as e:
                self._batch_log(f"打开目录失败：{e}", "WARNING")
        else:
            messagebox.showwarning("提示", "保存目录不存在，请先选择目录")

    def _batch_select_all(self, checked: bool):
        for item in self.batch_items:
            item.checked = checked
        self._batch_refresh_rows()
        self._batch_refresh_buttons()

    def _batch_invert_selection(self):
        for item in self.batch_items:
            item.checked = not item.checked
        self._batch_refresh_rows()
        self._batch_refresh_buttons()

    def _batch_on_tree_click(self, event):
        """点击「选择」列切换勾选，其余位置不改变勾选"""
        if self.batch_parsing or self.batch_downloading:
            return "break"
        iid = self.batch_tree.identify_row(event.y)
        column = self.batch_tree.identify_column(event.x)
        if not iid or column != '#1':
            return
        item = self._batch_item_by_iid.get(iid)
        if not item:
            return
        item.checked = not item.checked
        self._batch_update_row(item)
        self._batch_update_selection_count()
        self._batch_refresh_buttons()
        return "break"

    def _batch_update_selection_count(self):
        total = len(self.batch_items)
        checked = sum(1 for it in self.batch_items if it.checked)
        self.batch_count_lbl.config(text=f"共 {total} 个 · 已选 {checked} 个")

    def _batch_update_row(self, item: BatchItem):
        iid = self._batch_iid_by_index.get(item.index)
        if not iid:
            return
        progress = ""
        if item.status == '下载中':
            progress = f" {item.progress:.0f}%"
        values = ('☑' if item.checked else '☐', str(item.index), item.title,
                  self._batch_format_duration(item.duration), item.status + progress)
        try:
            self.batch_tree.item(iid, values=values, tags=(self._batch_row_tag(item),))
        except Exception:
            pass

    def _batch_update_row_async(self, item: BatchItem):
        try:
            self.after(0, lambda it=item: self._batch_update_row(it))
        except Exception:
            pass

    def _batch_refresh_rows(self):
        for item in self.batch_items:
            self._batch_update_row(item)
        self._batch_update_selection_count()

    @staticmethod
    def _batch_format_duration(seconds) -> str:
        try:
            seconds = int(float(seconds or 0))
        except (TypeError, ValueError):
            seconds = 0
        if seconds <= 0:
            return "-"
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    @staticmethod
    def _batch_row_tag(item: BatchItem) -> str:
        if item.status == '已完成':
            return 'batch_done'
        if item.status == '失败':
            return 'batch_failed'
        if item.status == '已存在':
            return 'batch_skip'
        if item.status == '下载中':
            return 'batch_active'
        if item.status == '已停止':
            return 'batch_stopped'
        return 'row_even' if item.index % 2 == 0 else 'row_odd'

    # ---------- 批量下载：解析 ----------
    @staticmethod
    def _batch_expand_url(url: str) -> str:
        """B站视频链接去掉 ?p=N，确保返回整部合集/多P列表"""
        try:
            parsed = urlparse(url)
        except Exception:
            return url
        host = (parsed.netloc or '').lower()
        if 'bilibili.com' in host and '/video/' in (parsed.path or ''):
            query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                     if k.lower() not in ('p', 'page')]
            return urlunparse((parsed.scheme, parsed.netloc, parsed.path, '',
                               urlencode(query), ''))
        return url

    @staticmethod
    def _batch_extract_bvid(url: str) -> str:
        m = re.search(r'(BV[0-9A-Za-z]{10})', url or '')
        return m.group(1) if m else ''

    @staticmethod
    def _batch_entry_url(entry: dict, fallback: str) -> str:
        url = (entry.get('webpage_url') or entry.get('original_url') or
               entry.get('url') or '')
        if not url:
            eid = str(entry.get('id') or '')
            if eid.startswith('BV'):
                return f"https://www.bilibili.com/video/{eid}"
            return fallback
        if not url.startswith(('http://', 'https://')):
            if url.startswith('BV'):
                url = f"https://www.bilibili.com/video/{url}"
            else:
                url = "https://www.bilibili.com/video/" + url
        return url.split('#')[0]

    def _batch_fetch_bilibili_parts(self, bvid: str) -> Dict[int, Tuple[str, float]]:
        """B站多P分集标题/时长：一次 API 请求补齐 flat 解析缺失的标题"""
        if not bvid or not REQUESTS_AVAILABLE:
            return {}
        try:
            resp = requests.get(
                'https://api.bilibili.com/x/player/pagelist',
                params={'bvid': bvid, 'jsonp': 'jsonp'},
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                                  'AppleWebKit/537.36 (KHTML, like Gecko) '
                                  'Chrome/120.0.0.0 Safari/537.36',
                    'Referer': 'https://www.bilibili.com/',
                }, timeout=15)
            data = resp.json()
            if data.get('code') != 0:
                return {}
            parts = {}
            for item in (data.get('data') or []):
                page = int(item.get('page') or 0)
                if page > 0:
                    parts[page] = (str(item.get('part') or '').strip(),
                                   float(item.get('duration') or 0))
            return parts
        except Exception as e:
            logger.debug(f"B站分P信息获取失败: {e}")
            return {}

    def start_batch_parse(self):
        """解析批量下载列表（后台线程，不阻塞界面）"""
        if self.batch_parsing or self.batch_downloading:
            return
        if self.is_sniffing or self.is_downloading:
            messagebox.showwarning("提示", "请先等待当前视频下载/嗅探任务结束")
            return
        if not YTDLP_AVAILABLE:
            messagebox.showerror("yt-dlp 不可用", "请先安装 yt-dlp：pip install yt-dlp")
            return

        raw = self.batch_url_text.get('1.0', tk.END)
        urls = []
        for line in raw.splitlines():
            for token in line.split():
                if token.startswith(('http://', 'https://')) and token not in urls:
                    urls.append(token.rstrip('，。；、'))
        if not urls:
            messagebox.showwarning("提示", "请粘贴至少一个合集或视频地址")
            return

        self.batch_parsing = True
        self.batch_stop_requested = False
        self._batch_gen += 1
        gen = self._batch_gen
        self.batch_items = []
        self._batch_clear_tree()
        self._batch_clear_log()
        self.batch_progress_var.set(0)
        self.batch_progress_lbl.config(text="0 / 0")
        self._batch_set_status("正在解析列表…", 'warning')
        self._batch_log(f"开始解析 {len(urls)} 个地址", "INFO")
        self._batch_refresh_buttons()

        engine = self._create_ydlp_engine(probe=True)
        engine.log_callback = self._batch_log
        engine.progress_callback = lambda v, m="": None
        self._batch_engine = engine
        self._batch_thread = threading.Thread(
            target=self._run_batch_parse, args=(urls, gen, engine), daemon=True)
        self._batch_thread.start()

    def _run_batch_parse(self, urls, gen, engine):
        items: List[BatchItem] = []
        seen = set()
        try:
            for n, url in enumerate(urls, 1):
                if self.batch_stop_requested or gen != self._batch_gen:
                    break
                self._batch_log(f"[{n}/{len(urls)}] 解析：{url}", "INFO")
                target = self._batch_expand_url(url)
                try:
                    info = engine.extract_playlist(target)
                except Exception as e:
                    self._batch_log(f"解析失败：{e}", "ERROR")
                    continue
                entries = YTDLPEngine.playlist_entries(info)
                if not entries:
                    self._batch_log(f"未找到可下载的视频条目：{url}", "WARNING")
                    continue
                list_title = str((info or {}).get('title') or '').strip()
                bvid = (self._batch_extract_bvid(target)
                        or self._batch_extract_bvid(
                            str((info or {}).get('webpage_url') or
                                (info or {}).get('id') or '')))
                if not bvid and entries:
                    bvid = self._batch_extract_bvid(
                        self._batch_entry_url(entries[0], target))
                parts = self._batch_fetch_bilibili_parts(bvid) if bvid else {}
                added = 0
                for entry in entries:
                    if self.batch_stop_requested or gen != self._batch_gen:
                        break
                    if not isinstance(entry, dict):
                        continue
                    entry_url = self._batch_entry_url(entry, target)
                    if not entry_url or entry_url in seen:
                        continue
                    seen.add(entry_url)
                    title = str(entry.get('title') or '').strip()
                    try:
                        duration = float(entry.get('duration') or 0)
                    except (TypeError, ValueError):
                        duration = 0.0
                    page = 0
                    try:
                        page = int(dict(parse_qsl(urlparse(entry_url).query)).get('p') or 0)
                    except (TypeError, ValueError):
                        page = 0
                    if not title and page and page in parts:
                        title, part_duration = parts[page]
                        duration = duration or part_duration
                    if not title and len(parts) == 1:
                        title, part_duration = next(iter(parts.values()))
                        duration = duration or part_duration
                    if not title:
                        title = f"{list_title} 第{len(items) + 1}个" if list_title \
                            else f"第 {len(items) + 1} 个视频"
                    items.append(BatchItem(index=len(items) + 1, url=entry_url,
                                           title=title, duration=duration))
                    added += 1
                self._batch_log(
                    f"已解析 {added} 个视频" + (f"（共 {len(entries)} 个条目）"
                                              if added != len(entries) else ""), "INFO")
        except Exception as e:
            self._batch_log(f"解析异常：{e}", "ERROR")
        finally:
            self._batch_finalize_parse(items, gen)

    def _batch_finalize_parse(self, items, gen):
        def _apply():
            if gen != self._batch_gen:
                return
            self.batch_parsing = False
            self._batch_engine = None
            self._batch_render_items(items)
            stopped = self.batch_stop_requested
            self.batch_stop_requested = False
            if stopped:
                self._batch_set_status("已停止解析", 'warning')
                self._batch_log("解析已停止", "WARNING")
            elif not items:
                self._batch_set_status("未解析到视频", 'error')
                self._batch_log("没有解析到可下载的视频，请检查链接或登录状态", "ERROR")
            else:
                self._batch_set_status(f"解析完成，共 {len(items)} 个视频", 'success')
                self._batch_log(f"解析完成，共 {len(items)} 个视频", "INFO")
            self._batch_refresh_buttons()
        try:
            self.after(0, _apply)
        except Exception:
            pass

    # ---------- 批量下载：执行 ----------
    def _batch_quality_spec(self):
        """返回 (format_spec, audio_only)"""
        label = self.batch_quality_var.get()
        mapping = {
            "自动最佳": ('bv*+ba/b', False),
            "1080P 优先": ('bv*[height<=1080]+ba/b[height<=1080]/b', False),
            "720P 优先": ('bv*[height<=720]+ba/b[height<=720]/b', False),
            "480P 优先": ('bv*[height<=480]+ba/b[height<=480]/b', False),
            "仅音频 (MP3)": ('bestaudio/best', True),
        }
        return mapping.get(label, ('bv*+ba/b', False))

    def start_batch_download(self):
        """开始批量下载已勾选的视频"""
        if self.batch_parsing or self.batch_downloading:
            return
        if self.is_sniffing or self.is_downloading:
            messagebox.showwarning("提示", "请先等待当前视频下载/嗅探任务结束")
            return
        if not YTDLP_AVAILABLE:
            messagebox.showerror("yt-dlp 不可用", "请先安装 yt-dlp：pip install yt-dlp")
            return
        items = [it for it in self.batch_items if it.checked]
        if not items:
            messagebox.showwarning("提示", "请至少勾选一个视频")
            return
        save_dir = self.batch_dir_var.get().strip()
        if not save_dir:
            messagebox.showwarning("提示", "请选择保存目录")
            return
        try:
            os.makedirs(save_dir, exist_ok=True)
        except Exception as e:
            messagebox.showerror("保存目录无效", f"无法创建保存目录：\n{e}")
            return
        self._last_save_dir = save_dir

        if not UniversalDownloader.check_ffmpeg(self._resolve_ffmpeg_path()):
            messagebox.showerror("FFmpeg 不可用",
                                 "FFmpeg 未找到或不可用，请先在设置中配置正确的 FFmpeg 路径")
            return

        format_spec, audio_only = self._batch_quality_spec()
        subtitles = bool(self.subtitles_var.get())
        skip_existing = bool(self.batch_skip_var.get())
        # 记住批量页选项，下次打开直接沿用
        self.settings['batch_save_dir'] = save_dir
        self.settings['batch_quality'] = self.batch_quality_var.get()
        self.settings['batch_skip_existing'] = skip_existing
        try:
            self._save_config()
        except Exception:
            pass
        for it in items:
            it.status = "待下载"
            it.progress = 0.0
            it.error = ""
            it.file_path = ""
        self._batch_refresh_rows()

        self.batch_downloading = True
        self.batch_stop_requested = False
        self._batch_gen += 1
        gen = self._batch_gen
        self._batch_total_count = len(items)
        self.batch_progress_var.set(0)
        self.batch_progress_lbl.config(text=f"0 / {len(items)}")
        self._batch_set_status(f"正在批量下载 0/{len(items)}…", 'warning')
        self._batch_log(
            f"开始批量下载 {len(items)} 个视频 · 清晰度：{self.batch_quality_var.get()}", "INFO")
        self._batch_refresh_buttons()

        engine = self._create_ydlp_engine()
        engine.log_callback = self._batch_log
        engine.progress_callback = self._batch_on_engine_progress
        self._batch_engine = engine
        self._batch_thread = threading.Thread(
            target=self._run_batch_download,
            args=(items, save_dir, format_spec, audio_only, subtitles,
                  skip_existing, gen, engine), daemon=True)
        self._batch_thread.start()

    def stop_batch(self):
        """停止批量解析或批量下载"""
        if not (self.batch_parsing or self.batch_downloading):
            return
        self.batch_stop_requested = True
        self._batch_log("已请求停止，等待当前任务收尾…", "WARNING")
        self._batch_set_status("正在停止…", 'warning')
        engine = self._batch_engine
        if engine is not None:
            try:
                engine.stop()
            except Exception:
                pass
        self._batch_refresh_buttons()

    def _run_batch_download(self, items, save_dir, format_spec, audio_only,
                            subtitles, skip_existing, gen, engine):
        done = failed = skipped = 0
        completed = 0
        total = len(items)
        try:
            for index, item in enumerate(items, 1):
                if self.batch_stop_requested or gen != self._batch_gen:
                    break
                completed = index
                self._batch_current_item = item
                self._batch_current_index = index
                self._batch_total_count = total
                item.status = "下载中"
                item.progress = 0.0
                self._batch_update_row_async(item)
                self._batch_set_status(f"正在批量下载 {index}/{total}：{item.title}", 'warning')
                self._batch_log(f"[{index}/{total}] 开始下载：{item.title}", "INFO")
                base = self._batch_output_base(save_dir, item, total)

                if skip_existing:
                    existing = self._batch_find_existing(base)
                    if existing:
                        item.status = "已存在"
                        item.progress = 100.0
                        item.file_path = existing
                        skipped += 1
                        self._batch_log(f"跳过（文件已存在）：{os.path.basename(existing)}",
                                        "WARNING")
                        self._batch_update_row_async(item)
                        self._batch_progress_overall(index, total, 100)
                        continue

                resource = VideoResource(
                    url=item.url, resource_type='ytdlp', title=item.title,
                    page_title=item.title, page_url=item.url, source='yt-dlp',
                    engine='ytdlp', format_spec=format_spec)
                try:
                    ok = engine.download(item.url, base, format_spec,
                                         audio_only=audio_only, subtitles=subtitles)
                except Exception as e:
                    ok = False
                    self._batch_log(f"下载异常：{e}", "ERROR")
                if ok:
                    actual = engine._last_file or (base + ('.mp3' if audio_only else '.mp4'))
                    item.status = "已完成"
                    item.progress = 100.0
                    item.file_path = actual
                    done += 1
                    self._batch_log(f"完成：{os.path.basename(actual)}", "INFO")
                    self._save_history(item.url, actual, "success", resource=resource)
                else:
                    stopped = self.batch_stop_requested
                    item.status = "已停止" if stopped else "失败"
                    item.error = "" if stopped else "下载失败（链接失效/网络错误/需要登录）"
                    if not stopped:
                        failed += 1
                    self._save_history(item.url, base, "failed", resource=resource)
                    self._batch_log(
                        f"{'已停止' if stopped else '失败'}：{item.title}", "WARNING")
                self._batch_update_row_async(item)
                self._batch_progress_overall(index, total, 100)
        except Exception as e:
            self._batch_log(f"批量下载异常：{e}", "ERROR")
        finally:
            self._batch_current_item = None
            self._batch_finish_download(done, failed, skipped, completed, total, gen)

    def _batch_on_engine_progress(self, value, message=""):
        item = self._batch_current_item
        if item is None:
            return
        item.progress = max(0.0, min(100.0, float(value or 0)))
        now = time.time()
        if item.progress < 100 and now - getattr(self, '_batch_last_progress_ts', 0) < 0.25:
            return
        self._batch_last_progress_ts = now
        index = self._batch_current_index
        total = self._batch_total_count
        try:
            self.after(0, lambda it=item: self._batch_update_row(it))
            self.after(0, lambda i=index, t=total, v=item.progress:
                       self._batch_progress_overall(i, t, v))
        except Exception:
            pass

    def _batch_progress_overall(self, index, total, item_progress):
        if not total:
            return
        overall = ((max(0, index - 1) + max(0.0, min(100.0, item_progress)) / 100.0)
                   / total * 100.0)

        def _apply():
            try:
                self.batch_progress_var.set(overall)
                self.batch_progress_lbl.config(text=f"{min(index, total)} / {total}")
            except Exception:
                pass
        try:
            self.after(0, _apply)
        except Exception:
            pass

    def _batch_finish_download(self, done, failed, skipped, completed, total, gen):
        def _apply():
            if gen != self._batch_gen:
                return
            self.batch_downloading = False
            self._batch_engine = None
            stopped = self.batch_stop_requested
            self.batch_stop_requested = False
            self._batch_refresh_buttons()
            if stopped:
                self.batch_progress_lbl.config(text=f"{completed} / {total}")
                self._batch_set_status("已停止批量下载", 'warning')
                self._batch_log(
                    f"已停止：完成 {done} 个，失败 {failed} 个，跳过 {skipped} 个", "WARNING")
            else:
                self.batch_progress_var.set(100)
                self.batch_progress_lbl.config(text=f"{total} / {total}")
                summary = f"完成 {done} 个"
                if failed:
                    summary += f"，失败 {failed} 个"
                if skipped:
                    summary += f"，跳过 {skipped} 个"
                self._batch_set_status(f"批量下载结束：{summary}", 'success' if not failed else 'warning')
                self._batch_log(f"批量下载结束：{summary}", "INFO")
                try:
                    self._refresh_history()
                except Exception:
                    pass
                if failed:
                    messagebox.showwarning(
                        "批量下载结束",
                        f"{summary}\n\n失败的任务可在列表查看状态，重新勾选后可再次尝试。")
                else:
                    messagebox.showinfo("批量下载完成", summary)
        try:
            self.after(0, _apply)
        except Exception:
            pass

    def _batch_output_base(self, save_dir, item: BatchItem, total: int) -> str:
        """生成批量下载的目标路径（不含扩展名）"""
        prefix = f"{item.index:03d} - " if total > 1 else ""
        title = (item.title or f"video_{item.index}").replace('%', '％')
        name = self._sanitize_filename(prefix + title)[:110]
        base = os.path.join(save_dir, name)
        return self._truncate_win_full_path(base)

    @staticmethod
    def _batch_find_existing(base: str) -> str:
        """检查目标文件是否已存在（yt-dlp 可能输出 mp4/mkv/mp3 等）"""
        for ext in ('.mp4', '.mkv', '.webm', '.flv', '.mov', '.ts',
                    '.m4a', '.mp3', '.aac', '.opus', '.ogg'):
            path = base + ext
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                return path
        return ""

    # ---------- 批量下载：界面状态 ----------
    def _batch_clear_tree(self):
        if hasattr(self, 'batch_tree'):
            for row in self.batch_tree.get_children():
                self.batch_tree.delete(row)
        self._batch_iid_by_index = {}
        self._batch_item_by_iid = {}
        if hasattr(self, 'batch_empty'):
            self.batch_empty.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
        if hasattr(self, 'batch_count_lbl'):
            self.batch_count_lbl.config(text="共 0 个 · 已选 0 个")

    def _batch_render_items(self, items: List[BatchItem]):
        self.batch_items = items
        self._batch_clear_tree()
        for item in items:
            iid = self.batch_tree.insert(
                '', tk.END,
                values=('☑' if item.checked else '☐', str(item.index), item.title,
                        self._batch_format_duration(item.duration), item.status),
                tags=(self._batch_row_tag(item),))
            self._batch_iid_by_index[item.index] = iid
            self._batch_item_by_iid[iid] = item
        if items:
            self.batch_empty.place_forget()
        self._batch_update_selection_count()
        self._batch_refresh_buttons()

    def _batch_refresh_buttons(self):
        if not hasattr(self, 'batch_start_btn'):
            return
        c = self.colors
        busy = self.batch_parsing or self.batch_downloading
        disabled_bg = '#3f4a5a' if self.is_dark_theme else '#b0bec5'
        checked = sum(1 for it in self.batch_items if it.checked)
        can_start = bool(self.batch_items) and checked > 0 and not busy
        try:
            self.batch_parse_btn.config(
                state=tk.DISABLED if busy else tk.NORMAL,
                bg=disabled_bg if busy else c['primary'],
                cursor='arrow' if busy else 'hand2')
            self.batch_start_btn.config(
                text=f"↓ 开始批量下载({checked})" if self.batch_items else "↓ 开始批量下载",
                state=tk.NORMAL if can_start else tk.DISABLED,
                bg=c['primary'] if can_start else disabled_bg,
                activebackground=c['primary_dark'],
                cursor='hand2' if can_start else 'arrow')
            self.batch_stop_btn.config(
                state=tk.NORMAL if busy else tk.DISABLED,
                bg=c['error'] if busy else disabled_bg,
                activebackground='#c0392b',
                cursor='hand2' if busy else 'arrow')
        except Exception:
            pass

    def _batch_set_status(self, text, level='info'):
        if not hasattr(self, 'batch_status_var'):
            return
        color_map = {'info': self.colors['text_light'],
                     'success': self.colors['success'],
                     'warning': self.colors['warning'],
                     'error': self.colors['error']}

        def _apply():
            try:
                self.batch_status_var.set(text)
                color = color_map.get(level, self.colors['text_light'])
                self.batch_status_lbl.config(fg=color)
                self.batch_status_dot.config(fg=color)
            except Exception:
                pass
        try:
            self.after(0, _apply)
        except Exception:
            pass

    def _batch_log(self, message, level="INFO"):
        text = str(message or '').strip()
        if not text or not hasattr(self, 'batch_log_text'):
            return
        def _append():
            try:
                ts = datetime.now().strftime('%H:%M:%S')
                self.batch_log_text.insert(tk.END, f"[{ts}] {text}\n", level)
                self.batch_log_text.see(tk.END)
                # 限制日志长度，避免批量任务长时间运行占用内存
                if int(self.batch_log_text.index('end-1c').split('.')[0]) > 800:
                    self.batch_log_text.delete('1.0', '200.0')
            except Exception:
                pass
        try:
            self.after(0, _append)
        except Exception:
            pass

    def _batch_clear_log(self):
        if hasattr(self, 'batch_log_text'):
            try:
                self.batch_log_text.delete('1.0', tk.END)
            except Exception:
                pass

    def _apply_batch_extras(self):
        """批量下载页自定义控件配色（深色模式/主题切换）"""
        c = self.colors
        dark = self.is_dark_theme
        for attr, bg in (('batch_input_card', c['bg']),
                         ('batch_input_inner', c['bg'])):
            if hasattr(self, attr):
                getattr(self, attr).config(bg=bg)
        for attr in ('batch_url_lbl', 'batch_input_tip'):
            if hasattr(self, attr):
                getattr(self, attr).config(bg=c['bg'], fg=c['text_light'])
        if hasattr(self, 'batch_url_text'):
            self.batch_url_text.config(
                bg=c['bg'], fg=c['text'], insertbackground=c['text'],
                selectbackground=c['primary'], selectforeground='white')
        if hasattr(self, 'batch_log_text'):
            log_bg = '#141824' if dark else '#171b26'
            self.batch_log_text.config(
                bg=log_bg, fg='#d7dce5', insertbackground='#d7dce5',
                selectbackground=c['primary_dark'], selectforeground='white')
            self.batch_log_text.tag_config('INFO', foreground='#d7dce5')
            self.batch_log_text.tag_config('WARNING', foreground='#ffcc80')
            self.batch_log_text.tag_config('ERROR', foreground='#ff8a80')
        if hasattr(self, 'batch_tree'):
            self.batch_tree.tag_configure('row_even', background=c['bg_card'])
            self.batch_tree.tag_configure(
                'row_odd', background='#2a2a3d' if dark else '#f7faff')
            self.batch_tree.tag_configure(
                'batch_done', background='#1f3b2a' if dark else '#e8f5e9',
                foreground='#a5d6a7' if dark else '#2e7d32')
            self.batch_tree.tag_configure(
                'batch_failed', background='#4a2323' if dark else '#ffebee',
                foreground='#ef9a9a' if dark else '#c62828')
            self.batch_tree.tag_configure(
                'batch_skip', background='#3c3a24' if dark else '#fff8e1',
                foreground='#ffe082' if dark else '#f57f17')
            self.batch_tree.tag_configure(
                'batch_active', background='#1d3346' if dark else '#e3f2fd',
                foreground='#90caf9' if dark else '#1565c0')
            self.batch_tree.tag_configure(
                'batch_stopped', background='#333344' if dark else '#eeeeee',
                foreground=c['text_light'])
        self._batch_refresh_buttons()

    def create_settings_page(self):
        """系统设置（单列布局）"""
        c = self.colors
        main = tk.Frame(self.settings_frame, bg=c['bg_card'])
        main.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

        title_frame = tk.Frame(main, bg=c['bg_card'])
        title_frame.pack(fill=tk.X, pady=(0, 15))
        self.settings_title_lbl = tk.Label(title_frame, text="系统设置", font=("微软雅黑", 16, "bold"),
                fg=c['text'], bg=c['bg_card'])
        self.settings_title_lbl.pack(side=tk.LEFT)

        # 使用 Canvas 支持滚动
        canvas_frame = tk.Frame(main, bg=c['bg_card'])
        canvas_frame.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(canvas_frame, bg=c['bg_card'], highlightthickness=0)
        self.settings_canvas = canvas
        scrollbar = ttk.Scrollbar(canvas_frame, orient=tk.VERTICAL, command=canvas.yview)
        settings_card = tk.Frame(canvas, bg=c['bg_card'], relief='solid', borderwidth=1,
                                 highlightbackground=c['border'])
        self.settings_card = settings_card
        settings_card.bind('<Configure>',
            lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        _card_id = canvas.create_window((0, 0), window=settings_card, anchor='nw')
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.bind('<Configure>', lambda e: canvas.itemconfig(_card_id, width=e.width))
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # 绑定鼠标滚轮滚动（递归绑定所有子控件）
        def _on_mousewheel(event):
            delta = getattr(event, 'delta', 0)
            if not delta:
                return "break"
            canvas.yview_scroll(-1 if delta > 0 else 1, "units")
            return "break"

        def _bind_mousewheel(widget):
            widget.bind('<MouseWheel>', _on_mousewheel)
            for child in widget.winfo_children():
                _bind_mousewheel(child)

        _bind_mousewheel(canvas)
        _bind_mousewheel(settings_card)

        card_inner = tk.Frame(settings_card, bg=c['bg_card'], padx=20, pady=15)
        card_inner.pack(fill=tk.BOTH, expand=True)

        def _row(text, widget_factory, tip=None):
            """一行设置项：标签 + 控件（控件创建在行 frame 内，保证对齐）"""
            r = tk.Frame(card_inner, bg=c['bg_card'])
            r.pack(fill=tk.X, pady=6)
            tk.Label(r, text=text, width=18, anchor=tk.W, bg=c['bg_card'],
                     fg=c['text']).pack(side=tk.LEFT)
            w = widget_factory(r)
            w.pack(side=tk.LEFT, padx=(0, 8))
            if tip:
                tk.Label(r, text=tip, font=("微软雅黑", 8), fg='#999',
                         bg=c['bg_card']).pack(side=tk.LEFT)
            return w

        self.skip_ad_entry = _row("跳过片头广告分片数",
                                  lambda p: ttk.Entry(p, style='Modern.TEntry', width=10),
                                  "⚠可能误删正片")
        self.skip_ad_entry.insert(0, self.settings["skip_ad_count"])

        self.threads_entry = _row("下载线程数",
                                  lambda p: ttk.Entry(p, style='Modern.TEntry', width=10),
                                  "HLS 默认 16 线程，失败自动降到 4")
        self.threads_entry.insert(0, self.settings["default_threads"])

        self.retry_entry = _row("下载重试次数",
                                lambda p: ttk.Entry(p, style='Modern.TEntry', width=10))
        self.retry_entry.insert(0, self.settings["max_retry"])

        ffm_row = tk.Frame(card_inner, bg=c['bg_card'])
        ffm_row.pack(fill=tk.X, pady=6)
        tk.Label(ffm_row, text="FFmpeg路径", width=18, anchor=tk.W,
                 bg=c['bg_card'], fg=c['text']).pack(side=tk.LEFT)
        self.ffmpeg_entry = ttk.Entry(ffm_row, style='Modern.TEntry')
        self.ffmpeg_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        self.ffmpeg_entry.insert(0, self.settings["ffmpeg_path"])
        ttk.Button(ffm_row, text="浏览", style='Secondary.TButton',
                   command=self.browse_ffmpeg).pack(side=tk.LEFT)

        self.speed_limit_entry = _row("下载限速(KB/s)",
                                      lambda p: ttk.Entry(p, style='Modern.TEntry', width=10),
                                      "0=不限")
        self.speed_limit_entry.insert(0, str(self.settings.get("speed_limit", "0")))

        aria_row = tk.Frame(card_inner, bg=c['bg_card'])
        aria_row.pack(fill=tk.X, pady=6)
        tk.Label(aria_row, text="aria2路径", width=18, anchor=tk.W,
                 bg=c['bg_card'], fg=c['text']).pack(side=tk.LEFT)
        self.aria2_entry = ttk.Entry(aria_row, style='Modern.TEntry')
        self.aria2_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        self.aria2_entry.insert(0, str(self.settings.get("aria2_path", "")))
        self.use_aria2_var = tk.BooleanVar(value=self.settings.get("use_aria2", False))
        ttk.Checkbutton(aria_row, text="加速", variable=self.use_aria2_var).pack(side=tk.LEFT)

        proxy_row = tk.Frame(card_inner, bg=c['bg_card'])
        proxy_row.pack(fill=tk.X, pady=6)
        tk.Label(proxy_row, text="网络代理", width=18, anchor=tk.W,
                 bg=c['bg_card'], fg=c['text']).pack(side=tk.LEFT)
        self.proxy_entry = ttk.Entry(proxy_row, style='Modern.TEntry')
        self.proxy_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        self.proxy_entry.insert(0, str(self.settings.get("proxy", "")))
        self.proxy_enabled_var = tk.BooleanVar(value=self.settings.get("proxy_enabled", False))
        ttk.Checkbutton(proxy_row, text="启用", variable=self.proxy_enabled_var).pack(side=tk.LEFT, padx=(0, 6))
        tk.Label(proxy_row, text="http://127.0.0.1:7890", font=("微软雅黑", 8),
                 fg='#999', bg=c['bg_card']).pack(side=tk.LEFT)

        self.proxy_sniff_var = tk.BooleanVar(value=False)
        _row("代理抓包模式",
             lambda p: ttk.Checkbutton(p, text="mitmproxy(端口8888)",
                                       variable=self.proxy_sniff_var,
                                       command=self._toggle_proxy_sniff),
             "HTTPS需装证书")

        _row("M3U8下载模式",
             lambda p: ttk.Combobox(p, textvariable=self.download_mode_var,
                                    values=["auto", "ffmpeg", "local"],
                                    state="readonly", width=10),
             "auto=本地16线程优先，失败回退FFmpeg")

        close_labels = ["每次询问", "最小化到后台", "退出程序"]
        close_map = {'每次询问': 'ask', '最小化到后台': 'minimize', '退出程序': 'exit'}
        close_rev = {v: k for k, v in close_map.items()}
        self.close_action_var = tk.StringVar(
            value=close_rev.get(self.settings.get('close_action', 'ask'), '每次询问'))
        _row("关闭窗口时",
             lambda p: ttk.Combobox(p, textvariable=self.close_action_var,
                                    values=close_labels, state="readonly", width=18),
             "选择后不再弹提示")

        dl_opts = tk.Frame(card_inner, bg=c['bg_card'])
        dl_opts.pack(fill=tk.X, pady=6)
        tk.Label(dl_opts, text="下载选项", width=18, anchor=tk.W,
                 bg=c['bg_card'], fg=c['text']).pack(side=tk.LEFT)
        ttk.Checkbutton(dl_opts, text="无头模式", variable=self.headless_var).pack(side=tk.LEFT, padx=(0, 15))
        ttk.Checkbutton(dl_opts, text="仅音频", variable=self.audio_only_var).pack(side=tk.LEFT, padx=(0, 15))
        ttk.Checkbutton(dl_opts, text="下载字幕", variable=self.subtitles_var).pack(side=tk.LEFT, padx=(0, 15))
        ttk.Checkbutton(dl_opts, text="直播", variable=self.live_mode_var).pack(side=tk.LEFT, padx=(0, 15))
        self.clipboard_var = tk.BooleanVar(value=self.settings.get("clipboard_monitor", False))
        ttk.Checkbutton(dl_opts, text="剪贴板监听", variable=self.clipboard_var,
                        command=lambda: self.set_clipboard_monitor(
                            not self._clipboard_enabled)
                        ).pack(side=tk.LEFT)
        tk.Label(dl_opts, text="（勾选=开启监听）", font=("微软雅黑", 8),
                 bg=c['bg_card'], fg=c['text_light']).pack(side=tk.LEFT, padx=(2, 0))

        chk_row = tk.Frame(card_inner, bg=c['bg_card'])
        chk_row.pack(fill=tk.X, pady=6)
        self.ad_filter_var = tk.BooleanVar(value=self.settings["auto_filter_ad"])
        self.dedup_var = tk.BooleanVar(value=self.settings["auto_dedup"])
        self.scan_dom_var = tk.BooleanVar(value=self.settings["scan_dom"])
        self.auto_open_var = tk.BooleanVar(value=self.settings.get("auto_open_folder", False))
        self.save_history_var = tk.BooleanVar(value=self.settings.get("save_history", True))
        self.show_welcome_var = tk.BooleanVar(value=self.settings.get("show_welcome", True))
        tk.Label(chk_row, text="高级选项", width=18, anchor=tk.W,
                 bg=c['bg_card'], fg=c['text']).pack(side=tk.LEFT)
        ttk.Checkbutton(chk_row, text="自动过滤广告", variable=self.ad_filter_var).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Checkbutton(chk_row, text="自动去重", variable=self.dedup_var).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Checkbutton(chk_row, text="扫描DOM视频", variable=self.scan_dom_var).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Checkbutton(chk_row, text="自动打开文件夹", variable=self.auto_open_var).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Checkbutton(chk_row, text="保存历史", variable=self.save_history_var).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Checkbutton(chk_row, text="启动显示说明", variable=self.show_welcome_var).pack(side=tk.LEFT)

        about_row = tk.Frame(card_inner, bg=c['bg_card'])
        about_row.pack(fill=tk.X, pady=6)
        tk.Label(about_row, text="当前版本", width=18, anchor=tk.W,
                 bg=c['bg_card'], fg=c['text']).pack(side=tk.LEFT)
        tk.Label(about_row, text=f"{APP_VERSION}", bg=c['bg_card'], fg=c['text']).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Button(about_row, text="检查更新", style='Secondary.TButton',
                   command=self._check_update).pack(side=tk.LEFT, padx=3)
        ttk.Button(about_row, text="导出日志", style='Secondary.TButton',
                   command=self._export_log).pack(side=tk.LEFT, padx=3)

        # 保存按钮
        btn_bar = tk.Frame(main, bg=c['bg_card'])
        btn_bar.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(btn_bar, text="保存设置", style='Primary.TButton',
                   command=self.save_settings).pack(side=tk.RIGHT)

        # 所有设置项创建完成后再次递归绑定，确保新增的行也能用滚轮
        _bind_mousewheel(settings_card)


    # ==================== 核心功能函数 ====================
    @staticmethod
    def _sanitize_filename(title: str) -> str:
        """清洗文件名非法字符，处理超长路径和 Windows 保留名"""
        if not title:
            return "output"
        sanitized = re.sub(r'[\\/:*?"<>|]', '', title).strip()
        # Windows 不允许文件名以点或空格结尾
        sanitized = sanitized.rstrip('. ')
        if not sanitized:
            return "output"
        # Windows 保留设备名
        reserved = {'CON', 'PRN', 'AUX', 'NUL',
                    *(f'COM{i}' for i in range(1, 10)),
                    *(f'LPT{i}' for i in range(1, 10))}
        if sanitized.split('.')[0].upper() in reserved:
            sanitized = '_' + sanitized
        # Windows 路径上限 260 字符，留足目录+扩展名空间
        return sanitized[:120]

    @staticmethod
    def _truncate_win_full_path(file_path: str, max_total: int = 259) -> str:
        """截断超长完整路径，确保 Windows 260 字符上限不触发 IO 异常"""
        if len(file_path) <= max_total:
            return file_path
        # Windows: 优先使用 \\?\ 长路径前缀绕过 260 字符限制
        if sys.platform == "win32":
            abs_path = os.path.abspath(file_path)
            if not abs_path.startswith('\\\\?\\') and len(abs_path) > max_total:
                return '\\\\?\\' + abs_path
        # 回退：截断文件名
        directory = os.path.dirname(file_path)
        basename = os.path.basename(file_path)
        name, ext = os.path.splitext(basename)
        available = max_total - len(directory) - 1 - len(ext)
        if available < 10:
            name = name[:10]
        else:
            name = name[:available]
        result = os.path.join(directory, name + ext)
        return result

    @staticmethod
    def _auto_append_ext(file_path: str, ext: str = ".mp4") -> str:
        """路径没有后缀时自动补全扩展名"""
        if not os.path.splitext(file_path)[1]:
            return file_path + ext
        return file_path

    def _default_save_name(self, resource: Optional[VideoResource] = None,
                           url: str = "") -> str:
        """另存为默认文件名：优先网页标题，其次视频标题，最后 URL 文件名"""
        page_title = ""
        if resource is not None:
            page_title = (resource.page_title or "").strip()
        # 直链/本地导入没有页面标题时不要继承上一次嗅探的网页标题，否则默认文件名会串
        allow_page_fallback = (resource is None or
                               getattr(resource, 'source', '') in
                               ('network', 'dom', 'playinfo', 'proxy', 'yt-dlp'))
        if not page_title and allow_page_fallback:
            page_title = (getattr(self.monitor, 'page_title', '') or '').strip()
            if not page_title:
                page_title = (getattr(self, '_ytdlp_page_title', '') or '').strip()

        title = ""
        if resource is not None:
            title = (resource.title or "").strip()
        # 浏览器资源通常 title 是空的或等于网页标题；yt-dlp 资源用视频标题
        name = page_title or title
        if not name:
            raw_url = (resource.url if resource is not None else url) or url
            try:
                parsed = urlparse(raw_url)
                name = unquote(parsed.path).rstrip('/').rsplit('/', 1)[-1] or parsed.netloc or "video"
                name = os.path.splitext(name)[0]
            except Exception:
                name = "video"
        name = re.sub(r'[\r\n\t]+', ' ', name)
        return self._sanitize_filename(name)

    def _suggest_ext(self, resource: Optional[VideoResource] = None) -> str:
        """根据资源类型/音频模式给出建议扩展名"""
        if hasattr(self, 'audio_only_var') and self.audio_only_var.get():
            return ".mp3"
        if resource is not None:
            ext = (resource.ext or '').lower().lstrip('.')
            if ext in ('mp4', 'webm', 'mkv', 'flv', 'm4a', 'mp3', 'aac', 'ogg'):
                return '.' + ext
            if resource.resource_type == 'webm':
                return ".webm"
            if resource.resource_type == 'flv':
                return ".flv"
        return ".mp4"

    def _toggle_download(self):
        """下载按钮：多选时批量下载，单选时下载当前资源"""
        if self.is_downloading or self.is_sniffing:
            return
        if self.batch_parsing or self.batch_downloading:
            messagebox.showwarning("提示", "批量下载任务正在进行，请先等待完成或停止")
            return
        selected = self.resource_tree.selection()
        if len(selected) >= 2:
            self._batch_download()
        else:
            self.start_download()

    def _set_status(self, text: str):
        """更新资源栏状态文字和状态圆点（线程安全）"""
        def _apply():
            try:
                self.status_var.set(text)
                if hasattr(self, 'status_dot'):
                    if any(k in text for k in ('失败', '未找到', '未解析')):
                        color = self.colors['error']
                    elif any(k in text for k in ('正在', '停止中', '解析中')):
                        color = self.colors['warning']
                    else:
                        color = self.colors['success']
                    self.status_dot.config(fg=color)
            except Exception:
                pass
        try:
            self.after(0, _apply)
        except Exception:
            pass

    def _refresh_action_button_colors(self):
        """统一刷新单一开始/停止按钮、下载按钮的状态与配色"""
        if not hasattr(self, 'main_btn'):
            return
        c = self.colors
        busy = self.is_sniffing or self.is_downloading
        stopping = self._stop_requested
        muted_bg = '#546e7a' if self.is_dark_theme else '#90a4ae'
        disabled_bg = '#3f4a5a' if self.is_dark_theme else '#b0bec5'
        if busy:
            self.main_btn.config(
                text="停止中…" if stopping else "停止",
                bg=muted_bg if stopping else c['error'],
                fg='white',
                activebackground=muted_bg if stopping else '#c0392b',
                state=tk.DISABLED if stopping else tk.NORMAL,
                cursor='arrow' if stopping else 'hand2')
        else:
            self.main_btn.config(
                text="开始",
                bg=c['primary'],
                fg='white',
                activebackground=c['primary_dark'],
                state=tk.NORMAL,
                cursor='hand2')
        # 下载按钮
        if hasattr(self, 'download_toggle_btn'):
            n_selected = len(self.resource_tree.selection())
            has_sel = bool(self.selected_resource) or n_selected > 0
            enabled = has_sel and not busy
            btn_text = f"↓ 下载全部({n_selected})" if n_selected >= 2 else "↓ 下载选中视频"
            self.download_toggle_btn.config(
                text=btn_text,
                bg=c['primary'] if enabled else disabled_bg,
                activebackground=c['primary_dark'],
                disabledforeground='#ffffff',
                state=tk.NORMAL if enabled else tk.DISABLED,
                cursor='hand2' if enabled else 'arrow')
        # 日志开关文字
        if hasattr(self, 'log_toggle_btn'):
            self.log_toggle_btn.config(
                text="📜 隐藏日志" if getattr(self, '_log_pane_visible', True) else "📜 显示日志")

    def start_sniff(self):
        """开始嗅探：浏览器与 yt-dlp 同时解析，谁先有结果谁先显示"""
        if self.is_sniffing or self.is_downloading:
            return
        url = self.url_entry.get().strip()
        if not url or not url.lower().startswith(('http://', 'https://')):
            messagebox.showerror("错误", "请输入有效的网页地址")
            return

        # 微信视频号分享链接：预览页只给封面，走腾讯元宝官方登录态换 token/eid，
        # 再请求视频号官方 feed 接口拿可下载的 MP4 直链。
        if WeChatChannelsResolver.is_channels_url(url):
            self._start_wechat_channels(url)
            return

        # B 站/YouTube 等站点优先只用 yt-dlp：浏览器嗅探通常抓不到 DASH 分片，
        # 还会让用户白等 10 秒，并产生「未捕获到视频资源」的误导提示。
        if YTDLP_AVAILABLE and self._is_ytdlp_first_url(url):
            self._start_ytdlp_only()
            return

        self.sniffer.clear()
        self._ytdlp_resources = []
        self._ytdlp_page_title = ""
        self.clear_resource_list()
        self.is_sniffing = True
        self._task_kind = 'sniff'
        self._stop_requested = False
        self._sniff_token += 1
        token = self._sniff_token
        self._sniff_run_id += 1
        run_id = self._sniff_run_id
        self._set_status("正在嗅探…")
        self._refresh_action_button_colors()
        self._log_message(f"开始解析：{url}", "INFO")

        self.sniffer.ad_filter_enabled = self.ad_filter_var.get()
        self.sniffer.dedup_enabled = self.dedup_var.get()
        headless = self.headless_var.get()
        # v4.2: 在主线程一次性读取 Tk 变量，后台线程不再访问 tkinter.Variable
        scan_dom = self.scan_dom_var.get()
        ad_filter = self.ad_filter_var.get()
        dedup = self.dedup_var.get()
        # 抖音这类必须带 cookie 才能用 yt-dlp 的站点，没导入 cookie 时不要并行探测，
        # 否则每次都会先报一次「yt-dlp 解析失败」再回退，日志很误导
        probe_engine = (self._create_ydlp_engine(probe=True)
                        if YTDLP_AVAILABLE and not self._needs_ytdlp_cookie(url) else None)
        self._probe_engine = probe_engine
        if not headless:
            self._log_message("可见浏览器模式：可手动播放/登录", "INFO")

        # 嗅探固定跑在同一个常驻后台线程里：Playwright 的同步 API 绑在创建它的线程，
        # 每次换线程等于丢弃已启动的 Chromium 会话，白多花 2.5 秒以上。
        self._ensure_sniff_worker()
        self._sniff_queue.put((url, headless, token, run_id, probe_engine,
                               scan_dom, ad_filter, dedup))

    def _ensure_sniff_worker(self):
        """启动常驻嗅探线程（供 Playwright 会话复用）"""
        if getattr(self, '_sniff_worker', None) is not None and self._sniff_worker.is_alive():
            return
        self._sniff_worker = threading.Thread(target=self._sniff_worker_loop, daemon=True)
        self.sniff_thread = self._sniff_worker
        self._sniff_worker.start()

    def _sniff_worker_loop(self):
        while True:
            try:
                job = self._sniff_queue.get(timeout=600)
            except queue.Empty:
                # 空闲 10 分钟就关掉 Chromium，避免长期占内存
                try:
                    self.monitor.close()
                except Exception:
                    pass
                continue
            if job is None:
                break
            self._sniff_job_running = True
            try:
                self._run_sniff(*job)
            except Exception as e:
                logger.error(f"嗅探线程异常: {e}")
            finally:
                self._sniff_job_running = False
                self._sniff_queue.task_done()

    def _run_sniff(self, url, headless=True, token=0, run_id=0,
                   probe_engine=None, scan_dom=True, ad_filter=True, dedup=True):
        """后台嗅探：并行启动 yt-dlp 探测，再执行浏览器嗅探"""
        probe = None
        try:
            self.monitor.scan_dom = scan_dom
            self.sniffer.ad_filter_enabled = ad_filter
            self.sniffer.dedup_enabled = dedup

            # yt-dlp 与浏览器并行，互不阻塞（engine 已在主线程创建）
            if probe_engine is not None:
                probe = threading.Thread(target=self._probe_ytdlp,
                                         args=(url, token, probe_engine), daemon=True)
                probe.start()

            self.monitor.start(url, headless=headless)
            page_title = self.monitor.get_page_title() or self._ytdlp_page_title
            page_url = self.monitor.get_page_url() or url
            with self.sniffer.lock:
                for res in self.sniffer.captured_resources.values():
                    res.page_url = res.page_url or page_url
                    if page_title:
                        res.page_title = res.page_title or page_title
                        if not res.title:
                            res.title = page_title
            # 浏览器结束后给 yt-dlp 一点收尾时间；已有资源时不阻塞界面
            if probe is not None and probe.is_alive():
                if self.sniffer.captured_resources or self._ytdlp_resources:
                    probe.join(timeout=0.3)
                else:
                    probe.join(timeout=2.0)

            if token == self._sniff_token:
                self.update_resource_list()
                total = len(self.sniffer.get_sorted_resources()) + len(self._ytdlp_resources)
                if total:
                    self._set_status("解析完成")
                else:
                    self._set_status("未找到可下载资源")
        except Exception as e:
            self._log_message(f"嗅探异常: {e}", "ERROR")
            self._set_status("嗅探失败")
        finally:
            # 只有当前这次嗅探才能复位 UI（避免旧的收尾线程影响新任务）
            if run_id == self._sniff_run_id:
                was_stopped = self._stop_requested
                self.is_sniffing = False
                self._task_kind = None
                self._stop_requested = False
                if was_stopped:
                    self._set_status("已停止嗅探")
                self.update_resource_list()
                self.after(0, self._refresh_action_button_colors)

    def stop_sniff(self):
        """停止嗅探：置停止标记，由嗅探线程自己关闭浏览器"""
        if not self.is_sniffing:
            return
        self._stop_requested = True
        self._sniff_token += 1  # 忽略仍在途的 yt-dlp 结果
        self.monitor.request_stop()
        engine = getattr(self, '_probe_engine', None)
        if engine:
            try:
                engine.stop()
            except Exception:
                pass
        self._set_status("正在停止嗅探…")
        self._log_message("已请求停止嗅探", "WARNING")
        self._refresh_action_button_colors()
        # 兜底：Playwright / yt-dlp 的同步调用可能长时间不返回，10 秒后强制复位 UI，
        # 避免「开始/停止」按钮永久卡在「停止中…」（run_id 防止误伤之后的新任务）
        self.after(10000, self._force_finish_sniff_if_stuck, self._sniff_run_id)

    def _force_finish_sniff_if_stuck(self, run_id=None):
        """停止嗅探超时兜底：复位 UI（嗅探线程为 daemon，旧线程的收尾会被世代号忽略）"""
        if run_id is not None and run_id != self._sniff_run_id:
            return
        if not self.is_sniffing or not self._stop_requested:
            return
        self._sniff_run_id += 1
        self._sniff_token += 1
        self.is_sniffing = False
        self._task_kind = None
        self._stop_requested = False
        self._set_status("已停止嗅探（等待浏览器退出超时）")
        self._refresh_action_button_colors()
        self._log_message("停止嗅探超时，已强制复位界面；后台若仍占用浏览器，请稍后再开始新任务", "WARNING")

    def stop_current_task(self):
        """停止按钮统一入口：按当前任务类型停止嗅探或下载"""
        if self.is_downloading:
            self.stop_download()
        elif self.is_sniffing:
            self.stop_sniff()


    def update_resource_list(self):
        """更新资源列表（线程安全，转发到主线程执行）"""
        try:
            self.after(0, self._do_update_resource_list)
        except Exception:
            pass

    def _do_update_resource_list(self):
        """重建统一资源列表（浏览器嗅探 + yt-dlp 格式），保留已选条目"""
        selected_keys = set()
        for item_id in self.resource_tree.selection():
            tags = self.resource_tree.item(item_id, 'tags')
            if tags:
                selected_keys.add(tags[0])

        for item in self.resource_tree.get_children():
            self.resource_tree.delete(item)

        browser_resources = self.sniffer.get_sorted_resources()
        ytdlp_resources = list(getattr(self, '_ytdlp_resources', []) or [])
        for res in browser_resources:
            if not res.key:
                res.key = res.url
        for res in ytdlp_resources:
            if not res.key:
                res.key = f"{res.page_url or res.url}#{res.format_spec}"
            if not res.quality_score:
                res.quality_score = self.sniffer._calculate_quality_score(res)

        # 「自动最佳」置顶，其余按质量分排序
        for res in ytdlp_resources:
            if res.format_spec == 'bv*+ba/b':
                res.quality_score = max(res.quality_score, 99)
        resources = browser_resources + ytdlp_resources
        resources.sort(key=lambda r: -r.quality_score)

        self._resource_by_key = {}
        reselect = []
        for idx, res in enumerate(resources):
            key = res.key or res.url
            self._resource_by_key[key] = res
            if res.resource_type == 'ytdlp':
                type_display = (res.display_format or res.ext.upper() or "YT-DLP").upper()
                if type_display == '自动':
                    type_display = "自动"
            elif res.resource_type == 'dash':
                type_display = "DASH视频" if res.companion_url else "DASH"
            else:
                type_display = res.resource_type.upper()
                if res.is_drm:
                    type_display = "🔒" + type_display

            if res.size > 0:
                size_mb = res.size / (1024 * 1024)
                size_str = f"{size_mb:.1f}MB" if size_mb >= 1 else f"{res.size/1024:.0f}KB"
            else:
                size_str = "-"
            source_map = {'network': '网络', 'dom': 'DOM', 'playinfo': 'B站',
                          'manual': '手动', 'direct': '直链', 'yt-dlp': 'yt-dlp',
                          'wechat': '视频号', 'douyin_api': '抖音'}
            source_display = source_map.get(res.source, res.source or '网络')
            if res.engine == 'ytdlp':
                source_display = 'yt-dlp'

            item_id = self.resource_tree.insert('', tk.END, values=(
                type_display,
                res.resolution or "自动",
                size_str,
                source_display,
                self._resource_detail(res)
            ), tags=(key, 'row_even' if idx % 2 == 0 else 'row_odd'))
            if key in selected_keys:
                reselect.append(item_id)

        if reselect:
            self.resource_tree.selection_set(reselect)
            self.selected_resource = self._resource_by_key.get(
                self.resource_tree.item(reselect[0], 'tags')[0])
        elif selected_keys:
            self.selected_resource = None

        if hasattr(self, 'resource_count_lbl'):
            self.resource_count_lbl.config(text=f"共 {len(resources)} 个")

        if hasattr(self, 'resource_empty'):
            if resources:
                self.resource_empty.place_forget()
            else:
                self.resource_empty.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
        self._refresh_action_button_colors()

    def _resource_detail(self, res) -> str:
        """资源列表最后一列：展示说明、标题或地址，便于区分同一网页的多个资源"""
        note = (getattr(res, 'note', '') or '').strip()
        title = (res.title or '').strip()
        page_title = (res.page_title or getattr(self.monitor, 'page_title', '') or '').strip()
        if res.engine == 'ytdlp':
            if note:
                return note
            return title or res.url
        if note:
            return note
        if title and title != page_title:
            return title
        try:
            parsed = urlparse(res.url)
            name = unquote(parsed.path).rstrip('/').rsplit('/', 1)[-1] or parsed.netloc
            detail = f"{name}  ·  {parsed.netloc}"
        except Exception:
            detail = res.url[:110]
        if res.resource_type == 'ts':
            detail = "HLS 分片（随 M3U8 统一下载）  ·  " + detail
        return detail

    def _selected_resources(self) -> List[VideoResource]:
        """返回当前选中的资源列表（按表格顺序）"""
        result = []
        mapping = getattr(self, '_resource_by_key', {})
        for item_id in self.resource_tree.selection():
            tags = self.resource_tree.item(item_id, 'tags')
            if tags and tags[0] in mapping:
                result.append(mapping[tags[0]])
        return result


    def _on_resource_double_click(self, event):
        """双击资源：有选中项时直接进入下载流程"""
        item = self.resource_tree.identify_row(event.y)
        if not item:
            return
        self.resource_tree.selection_set(item)
        self.on_resource_select(None)
        self._toggle_download()

    def _on_resource_right_click(self, event):
        """右键菜单：复制URL、导出资源列表"""
        item = self.resource_tree.identify_row(event.y)
        if not item:
            return
        # 右键已在多选项内时保留多选，避免把批量选择清成单选
        if item not in self.resource_tree.selection():
            self.resource_tree.selection_set(item)
        self.on_resource_select(None)

        res = self._selected_resources()
        can_subtitle = bool(res) and res[0].resource_type == 'm3u8'
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="复制资源URL", command=self._copy_resource_url)
        menu.add_command(label="下载字幕", command=self._download_subtitle,
                         state=tk.NORMAL if can_subtitle else tk.DISABLED)
        menu.add_separator()
        menu.add_command(label="导出全部资源(JSON)", command=lambda: self._export_resources('json'))
        menu.add_command(label="导出全部资源(TXT)", command=lambda: self._export_resources('txt'))
        menu.add_separator()
        menu.add_command(label="仅导出M3U8清单", command=self._export_m3u8_manifest)
        menu.add_command(label="导出HAR抓包数据", command=self._export_har)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            # 释放 grab 并销毁菜单，避免反复右键累积子控件/抢占鼠标
            try:
                menu.grab_release()
            except Exception:
                pass
            menu.destroy()

    def _copy_resource_url(self):
        """复制选中资源的完整URL到剪贴板（yt-dlp 行复制原网页地址）"""
        selected = self._selected_resources()
        if not selected:
            return
        res = selected[0]
        url = res.page_url if res.engine == 'ytdlp' else res.url
        if url:
            self.clipboard_clear()
            self.clipboard_append(url)
            self._last_clipboard = url  # 忽略本程序自己复制的链接，避免再次弹窗
            self._log_message("已复制资源链接", "INFO")

    def _export_resources(self, fmt: str = 'json'):
        """导出全部资源列表（浏览器嗅探 + yt-dlp）为 JSON 或 TXT"""
        resources = self.sniffer.get_sorted_resources() + list(getattr(self, '_ytdlp_resources', []) or [])
        if not resources:
            messagebox.showinfo("提示", "没有可导出的资源")
            return

        ext = '.json' if fmt == 'json' else '.txt'
        path = filedialog.asksaveasfilename(
            defaultextension=ext,
            filetypes=[(f"{fmt.upper()} 文件", f"*{ext}"), ("所有文件", "*.*")],
            title="导出资源列表"
        )
        if not path:
            return

        try:
            with open(path, 'w', encoding='utf-8') as f:
                if fmt == 'json':
                    data = [{
                        'url': r.page_url if r.engine == 'ytdlp' else r.url,
                        'engine': r.engine,
                        'format': r.format_spec,
                        'type': r.resource_type,
                        'resolution': r.resolution, 'bandwidth': r.bandwidth,
                        'size': r.size, 'source': r.source, 'quality_score': r.quality_score
                    } for r in resources]
                    json.dump(data, f, ensure_ascii=False, indent=2)
                else:
                    for r in resources:
                        f.write(f"[{r.resource_type.upper()}] {r.resolution or '?'} | "
                                f"{r.page_url if r.engine == 'ytdlp' else r.url}\n")
            self._log_message(f"已导出 {len(resources)} 条资源", "INFO")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))

    def _export_m3u8_manifest(self):
        """仅导出 M3U8 清单文件，不下载分片数据"""
        selected = self._selected_resources()
        if not selected:
            messagebox.showwarning("提示", "请先选择一个 M3U8 资源")
            return
        res = selected[0]
        url = res.url
        if res.engine == 'ytdlp' or not url or 'm3u8' not in url.lower():
            messagebox.showwarning("提示", "请选择浏览器嗅探到的 M3U8 资源")
            return

        path = filedialog.asksaveasfilename(
            defaultextension=".m3u8",
            filetypes=[("M3U8 文件", "*.m3u8"), ("所有文件", "*.*")],
            title="导出 M3U8 清单"
        )
        if not path:
            return

        try:
            downloader = self._create_downloader()
            hdrs = dict(res.headers) if res.headers else None
            content = downloader._download_content(url, headers=hdrs)
            if content:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(content)
                self._log_message(f"M3U8 清单已导出: {path}", "INFO")
                messagebox.showinfo("成功", f"M3U8 清单已导出到:\n{path}")
            else:
                messagebox.showerror("失败", "M3U8 内容下载失败")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))

    def _export_har(self):
        """导出 Playwright 嗅探抓包数据为 HAR 格式"""
        if not hasattr(self.monitor, 'captured_requests') or not self.monitor.captured_requests:
            messagebox.showwarning("提示", "没有可导出的抓包数据，请先执行浏览器嗅探")
            return

        path = filedialog.asksaveasfilename(
            defaultextension=".har",
            filetypes=[("HAR 文件", "*.har"), ("JSON 文件", "*.json"), ("所有文件", "*.*")],
            title="导出 HAR 抓包数据"
        )
        if not path:
            return

        try:
            har_data = {
                "log": {
                    "version": "1.2",
                    "creator": {"name": "VideoDownloader", "version": APP_VERSION.lstrip('v')},
                    "entries": []
                }
            }
            for req in self.monitor.captured_requests:
                entry = {
                    "request": {
                        "method": req.get('method', 'GET'),
                        "url": req.get('url', ''),
                        "headers": [{"name": k, "value": v} for k, v in req.get('headers', {}).items()],
                    },
                    "response": {
                        "status": req.get('status', 200),
                        "headers": [{"name": k, "value": v} for k, v in req.get('response_headers', {}).items()],
                        "content": {
                            "mimeType": req.get('content_type', ''),
                            "size": req.get('size', 0),
                        }
                    },
                    "startedDateTime": req.get('timestamp', ''),
                    "time": req.get('duration', 0),
                }
                har_data["log"]["entries"].append(entry)

            with open(path, 'w', encoding='utf-8') as f:
                json.dump(har_data, f, ensure_ascii=False, indent=2)
            self._log_message(f"HAR 抓包数据已导出: {path}（{len(har_data['log']['entries'])} 条请求）", "INFO")
            messagebox.showinfo("成功", f"HAR 数据已导出到:\n{path}")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))

    def clear_resource_list(self):
        """清空资源列表（同时重置数量统计与空状态）"""
        for item in self.resource_tree.get_children():
            self.resource_tree.delete(item)
        self.selected_resource = None
        self._ytdlp_resources = []
        self._resource_by_key = {}
        self._refresh_action_button_colors()
        if hasattr(self, 'resource_count_lbl'):
            self.resource_count_lbl.config(text="共 0 个")
        if hasattr(self, 'resource_empty'):
            self.resource_empty.place(relx=0.5, rely=0.5, anchor=tk.CENTER)

    def on_resource_select(self, event):
        """选择资源（支持多选）"""
        selected = self.resource_tree.selection()
        if not selected:
            self.selected_resource = None
            self._refresh_action_button_colors()
            return

        resources = self._selected_resources()
        self.selected_resource = resources[0] if resources else None
        self._refresh_action_button_colors()

    def browse_ffmpeg(self):
        path = filedialog.askopenfilename(filetypes=[("可执行文件", "*.exe"), ("所有文件", "*.*")])
        if path:
            self.ffmpeg_entry.delete(0, tk.END)
            self.ffmpeg_entry.insert(0, path)

    def _resolve_ffmpeg_path(self) -> str:
        """把 'ffmpeg' 解析为实际可执行文件路径，优先使用程序目录下的 ffmpeg.exe"""
        raw = ""
        if hasattr(self, 'ffmpeg_entry'):
            raw = self.ffmpeg_entry.get().strip()
        if not raw:
            raw = str(self.settings.get('ffmpeg_path', 'ffmpeg') or 'ffmpeg')
        if os.path.isfile(raw):
            return os.path.abspath(raw)
        found = shutil.which(raw)
        if found:
            return found
        local = Path(os.path.dirname(os.path.abspath(__file__))) / "ffmpeg.exe"
        if local.exists():
            return str(local)
        return raw

    def _create_downloader(self) -> UniversalDownloader:
        """每次下载创建独立下载器实例（含数值校验、限速）"""
        ffmpeg = self._resolve_ffmpeg_path()
        downloader = UniversalDownloader(ffmpeg_path=ffmpeg)
        try:
            downloader.retry_times = max(1, min(10, int(self.retry_entry.get())))
        except (ValueError, TypeError):
            downloader.retry_times = 3
        try:
            downloader.max_workers = max(1, min(32, int(self.threads_entry.get())))
        except (ValueError, TypeError):
            downloader.max_workers = 8
        try:
            downloader.skip_ad_segments = max(0, int(self.skip_ad_entry.get()))
        except (ValueError, TypeError):
            downloader.skip_ad_segments = 0
        # 修复：限速从输入框直接读取（与 retry/threads 一致），无需先保存设置
        try:
            speed_limit = int(self.speed_limit_entry.get())
            if speed_limit > 0:
                downloader.speed_limit = speed_limit * 1024  # KB/s -> B/s
        except (ValueError, TypeError):
            pass
        # 修复：aria2 从输入框直接读取
        aria2_path = self.aria2_entry.get().strip() if hasattr(self, 'aria2_entry') else ""
        use_aria2 = self.use_aria2_var.get() if hasattr(self, 'use_aria2_var') else False
        if aria2_path and use_aria2:
            if UniversalDownloader.check_aria2(aria2_path):
                downloader.aria2_path = aria2_path
                downloader.use_aria2 = True
            else:
                self._log_message(f"aria2 不可用({aria2_path})，使用内置下载", "WARNING")
        # 代理设置（修复：从输入框直接读取）
        proxy_enabled = self.proxy_enabled_var.get() if hasattr(self, 'proxy_enabled_var') else False
        if proxy_enabled:
            proxy_url = self.proxy_entry.get().strip() if hasattr(self, 'proxy_entry') else ""
            if proxy_url:
                downloader.session.proxies = {
                    "http": proxy_url,
                    "https": proxy_url,
                }
                self._log_message(f"已启用网络代理: {proxy_url}", "INFO")
        # v4.0: 下载模式 / 音频提取 / 直播录制
        if hasattr(self, 'download_mode_var'):
            downloader.download_mode = self.download_mode_var.get()
        if hasattr(self, 'audio_only_var'):
            downloader.audio_only = self.audio_only_var.get()
        if hasattr(self, 'live_mode_var'):
            downloader.live_mode = self.live_mode_var.get()
        return downloader

    def start_download(self):
        """下载选中的视频：默认文件名优先使用网页标题"""
        if self.is_downloading or self.is_sniffing:
            return
        selected = self._selected_resources()
        if not selected:
            messagebox.showwarning("提示", "请先选择视频资源")
            return
        resource = selected[0]

        default_name = self._default_save_name(resource)
        default_ext = self._suggest_ext(resource)
        init_dir = getattr(self, '_last_save_dir', '') or os.path.expanduser('~')
        save_path = filedialog.asksaveasfilename(
            defaultextension=default_ext,
            initialfile=default_name + default_ext,
            initialdir=init_dir,
            filetypes=[
                ("视频文件", "*.mp4;*.mkv;*.webm;*.flv;*.ts"),
                ("音频文件", "*.mp3;*.m4a;*.aac;*.ogg"),
                ("所有文件", "*.*")
            ],
            title="保存视频"
        )
        if not save_path:
            return
        self._last_save_dir = os.path.dirname(save_path)
        save_path = self._auto_append_ext(save_path, default_ext)
        save_path = self._truncate_win_full_path(save_path)

        if not UniversalDownloader.check_ffmpeg(self._resolve_ffmpeg_path()):
            messagebox.showerror("FFmpeg 不可用", "FFmpeg 未找到或不可用，请先在设置中配置正确的 FFmpeg 路径")
            return

        self._start_download_task(resource, save_path)

    def _start_download_task(self, resource: VideoResource, save_path: str):
        """统一启动下载任务（在主线程准备好下载器/引擎，避免后台线程访问 Tk 变量）"""
        if self.is_sniffing or self.is_downloading:
            return
        audio_only = bool(self.audio_only_var.get())
        subtitles = bool(self.subtitles_var.get())
        downloader = None
        engine = None
        if resource.engine == 'ytdlp':
            engine = self._create_ydlp_engine()
        else:
            downloader = self._create_downloader()
            downloader.log_callback = self._log_message
            downloader.progress_callback = self._update_progress
        with self._download_lock:
            self.is_downloading = True
            self._task_kind = 'download'
            self._stop_requested = False
            self._download_run_id += 1
            run_id = self._download_run_id
            self.current_downloader = downloader
            self._active_ytdlp_engine = engine
            self.progress_var.set(0)
            self._set_status(f"正在下载：{os.path.basename(save_path)}")
            self._refresh_action_button_colors()
            self._log_message(f"开始下载：{os.path.basename(save_path)[:60]}", "INFO")

            self.download_thread = threading.Thread(
                target=self._run_download,
                args=(resource, save_path, run_id, downloader, engine, audio_only, subtitles),
                daemon=True
            )
            self.download_thread.start()

    def _run_download(self, resource: VideoResource, save_path: str, run_id: int = 0,
                      downloader=None, engine=None, audio_only=False, subtitles=False):
        """后台下载：统一处理内置下载器与 yt-dlp 引擎"""
        success = False
        actual_path = save_path
        error_text = ""
        try:
            if engine is not None:
                base, _ = os.path.splitext(save_path)
                ok = engine.download(resource.page_url or resource.url, base,
                                     resource.format_spec or 'bv*+ba/b',
                                     audio_only=audio_only,
                                     subtitles=subtitles)
                if engine._last_file and os.path.exists(engine._last_file):
                    actual_path = engine._last_file
                success = ok
            elif downloader is not None:
                if audio_only:
                    success = downloader.extract_audio(resource, save_path)
                else:
                    success = downloader.download(resource, save_path)
            else:
                error_text = "下载器初始化失败"

            if success:
                self._save_history(resource.url, actual_path, "success", resource=resource)
                if run_id == self._download_run_id:
                    self._set_status("下载完成")
                if run_id == self._download_run_id and not self._stop_requested:
                    self._open_output_folder(actual_path)
                    self.after(0, lambda p=actual_path: messagebox.showinfo(
                        "下载完成", f"视频已保存：\n{p}"))
            else:
                cancelled = self._stop_requested or run_id != self._download_run_id
                if cancelled:
                    if run_id == self._download_run_id:
                        self._set_status("已停止下载")
                    self._log_message("下载已停止", "WARNING")
                else:
                    if resource.resource_type == 'm3u8':
                        error_text = ("视频下载失败。M3U8 链接可能已过期，或 CDN 拒绝了当前网络；"
                                      "请重新嗅探后立即下载，或尝试 Cookie/代理。")
                    else:
                        error_text = (f"视频下载失败：{self._last_download_error or '网络或服务器拒绝下载'}。\n"
                                      "可尝试重新解析、导入 Cookie 文件或开启代理后重试。")
                    self._save_history(resource.url, save_path, "failed", resource=resource)
                    self._set_status("下载失败")
                    self.after(0, lambda msg=error_text: messagebox.showerror("下载失败", msg))
        except Exception as e:
            error_text = str(e)
            self._log_message(f"下载异常: {e}", "ERROR")
            if not self._stop_requested and run_id == self._download_run_id:
                self._set_status("下载失败")
                self._save_history(resource.url, save_path, "failed", resource=resource)
                self.after(0, lambda msg=error_text: messagebox.showerror("下载失败", msg))
        finally:
            if run_id == self._download_run_id:
                stopped = self._stop_requested
                with self._download_lock:
                    self.is_downloading = False
                    self._task_kind = None
                    self._stop_requested = False
                    self.current_downloader = None
                    self._active_ytdlp_engine = None
                if stopped:
                    self._set_status("已停止下载")
                self.after(0, self._finish_download_ui)

    def _finish_download_ui(self):
        """下载结束后复位按钮与进度"""
        self.progress_var.set(0)
        self._refresh_action_button_colors()
        self._update_queue_count()

    def _open_output_folder(self, save_path: str):
        """下载完成后按设置打开输出目录"""
        if not self.settings.get("auto_open_folder", False):
            return
        try:
            folder = os.path.dirname(os.path.abspath(save_path))
            if sys.platform == "win32":
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.run(['open', folder])
            else:
                subprocess.run(['xdg-open', folder])
        except Exception:
            pass

    def start_direct_download(self):
        """直接下载直链 / M3U8（不经过网页嗅探）"""
        if self.is_downloading or self.is_sniffing:
            return
        url = self.url_entry.get().strip()
        if not url or not url.lower().startswith(('http://', 'https://')):
            messagebox.showerror("错误", "请输入有效的视频URL")
            return

        res_type = self.sniffer.detect_resource_type(url)
        resource = VideoResource(url=url, resource_type=res_type, page_url=url,
                                 source='direct')
        default_name = self._default_save_name(resource, url)
        default_ext = self._suggest_ext(resource)
        init_dir = getattr(self, '_last_save_dir', '') or os.path.expanduser('~')
        save_path = filedialog.asksaveasfilename(
            defaultextension=default_ext,
            initialfile=default_name + default_ext,
            initialdir=init_dir,
            filetypes=[
                ("视频文件", "*.mp4;*.mkv;*.webm;*.flv;*.ts"),
                ("所有文件", "*.*")
            ],
            title="保存视频"
        )
        if not save_path:
            return
        self._last_save_dir = os.path.dirname(save_path)
        save_path = self._auto_append_ext(save_path, default_ext)
        save_path = self._truncate_win_full_path(save_path)

        if not UniversalDownloader.check_ffmpeg(self._resolve_ffmpeg_path()):
            messagebox.showerror("FFmpeg 不可用", "FFmpeg 未找到或不可用，请先在设置中配置正确的 FFmpeg 路径")
            return

        self._start_download_task(resource, save_path)

    def stop_download(self):
        """停止当前下载：终止 FFmpeg/yt-dlp、清空队列，等待工作线程收尾"""
        if not self.is_downloading:
            return
        self._stop_requested = True
        self._set_status("正在停止下载…")
        self._refresh_action_button_colors()
        self._log_message("正在停止下载…", "WARNING")

        with self._download_lock:
            downloader = getattr(self, 'current_downloader', None)
            engine = getattr(self, '_active_ytdlp_engine', None)
        if downloader:
            try:
                downloader.stop()
            except Exception:
                pass
        if engine:
            try:
                engine.stop()
            except Exception:
                pass

        # 清空排队任务并递增世代号，防止旧 worker 停止后又消费新队列
        self._queue_gen += 1
        while not self._download_queue.empty():
            try:
                self._download_queue.get_nowait()
            except queue.Empty:
                break
        self._queue_running = False
        self._update_queue_count()

        # 兜底：如果底层网络请求长时间不返回，5 秒后强制复位 UI
        self.after(5000, self._force_finish_download_if_stuck)

    def _force_finish_download_if_stuck(self):
        """停止超时兜底：复位 UI（工作线程为 daemon，不会阻塞退出）"""
        if not self.is_downloading or not self._stop_requested:
            return
        self._download_run_id += 1
        self._queue_gen += 1  # 让仍存活的旧队列 worker 不再覆盖新任务状态
        with self._download_lock:
            self.is_downloading = False
            self._task_kind = None
            self._stop_requested = False
            self.current_downloader = None
            self._active_ytdlp_engine = None
        self._set_status("已停止下载")
        self._finish_download_ui()


    # ---------------- yt-dlp 解析（统一资源列表） ----------------
    def _ydlp_log(self, message, level="INFO"):
        """yt-dlp 下载日志（过滤无意义的 generic/unsupported 噪声）"""
        if level == "DEBUG":
            return
        text = str(message or '')
        if 'Falling back on generic information extractor' in text:
            return
        if 'Unsupported URL' in text:
            return
        self._log_message(text, level)

    def _ydlp_probe_log(self, message, level="INFO"):
        """yt-dlp「解析探测」阶段的日志：成功/失败由 _probe_ytdlp 统一汇报，
        这里不转发 yt-dlp 的解析错误，避免浏览器已经抓到资源后仍刷 ERROR。"""
        return

    def _ydlp_progress(self, value, msg=""):
        # 与 _update_progress 等价，保留给旧调用；新代码统一用 _update_progress
        self._update_progress(value)

    def _create_ydlp_engine(self, probe: bool = False) -> YTDLPEngine:
        ffmpeg = self._resolve_ffmpeg_path() if hasattr(self, 'ffmpeg_entry') else "ffmpeg"
        proxy = ""
        if getattr(self, 'proxy_enabled_var', None) and self.proxy_enabled_var.get():
            proxy = self.proxy_entry.get().strip() if hasattr(self, 'proxy_entry') else ""
        cookie = getattr(self, '_ydlp_cookie_path', "") or ""
        log_cb = self._ydlp_probe_log if probe else self._ydlp_log
        return YTDLPEngine(ffmpeg_location=ffmpeg, proxy=proxy, cookiefile=cookie,
                           log_callback=log_cb, progress_callback=self._ydlp_progress)

    def _probe_ytdlp(self, url: str, token: int, engine=None):
        """后台用 yt-dlp 解析网页，把格式加入统一资源列表（engine 由主线程创建）"""
        if not YTDLP_AVAILABLE:
            return
        if engine is None:
            return
        self._probe_engine = engine
        try:
            info = engine.extract_info(url)
            title, resources = YTDLPEngine.build_video_resources(info, url)
            if token != self._sniff_token or not resources:
                return
            for res in resources:
                res.quality_score = self.sniffer._calculate_quality_score(res)
            self._ytdlp_page_title = title
            self._ytdlp_resources = resources
            self.update_resource_list()
            short_title = title[:48] + ('…' if len(title) > 48 else '')
            self._log_message(f"yt-dlp 解析：{short_title}（{len(resources)} 个格式）", "INFO")
            self._set_status(f"yt-dlp 已解析 {len(resources)} 个格式")
        except Exception as e:
            logger.debug(f"yt-dlp 解析失败: {e}")
            if 'cookie' in str(e).lower():
                self._log_message(
                    "该站点用 yt-dlp 解析需要 Cookie（抖音/小红书常见）："
                    "请在设置里导入浏览器导出的 cookies.txt；未导入时改用浏览器嗅探",
                    "WARNING")
            if token == self._sniff_token and not self.sniffer.captured_resources:
                self._set_status("yt-dlp 未识别此页面")
        finally:
            if self._probe_engine is engine:
                self._probe_engine = None

    def _has_ytdlp_cookie(self) -> bool:
        """是否已导入可用的 yt-dlp Cookie 文件"""
        path = getattr(self, '_ydlp_cookie_path', '') or ''
        return bool(path and os.path.isfile(path))

    @staticmethod
    def _host_in(url: str, domains) -> bool:
        """URL 的主机是否命中给定域名（含子域）"""
        try:
            host = urlparse(url).netloc.lower().split(':')[0]
        except Exception:
            return False
        if not host:
            return False
        return any(host == d or host.endswith('.' + d) for d in domains)

    def _needs_ytdlp_cookie(self, url: str) -> bool:
        """该站点用 yt-dlp 是否必须先有 cookie 文件"""
        return (self._host_in(url, self.YTDLP_COOKIE_REQUIRED_HOSTS)
                and not self._has_ytdlp_cookie())

    def _is_ytdlp_first_url(self, url: str) -> bool:
        """判断站点是否应优先使用 yt-dlp（跳过浏览器嗅探）"""
        if not self._host_in(url, self.YTDLP_FIRST_HOSTS):
            return False
        # 抖音详情接口要求「新鲜 cookie」（s_v_web_id）。没导入 cookie 文件时
        # yt-dlp 必定报 "Fresh cookies are needed"，解析阶段就失败，只能回退
        # 浏览器嗅探；这里直接走嗅探（已支持 1080P），省掉一次无谓的失败。
        return not self._needs_ytdlp_cookie(url)

    def _start_ytdlp_only(self):
        """优先/仅使用 yt-dlp 解析（B 站、YouTube 等站点跳过浏览器）"""
        if self.is_sniffing or self.is_downloading:
            return
        if not YTDLP_AVAILABLE:
            messagebox.showerror("yt-dlp 不可用", "请先安装 yt-dlp：pip install yt-dlp")
            return
        url = self.url_entry.get().strip()
        if not url or not url.lower().startswith(('http://', 'https://')):
            messagebox.showerror("错误", "请输入有效的视频URL")
            return
        self.sniffer.clear()
        self._ytdlp_resources = []
        self._ytdlp_page_title = ""
        self.clear_resource_list()
        self.is_sniffing = True
        self._task_kind = 'ytdlp'
        self._stop_requested = False
        self._sniff_token += 1
        token = self._sniff_token
        self._sniff_run_id += 1
        run_id = self._sniff_run_id
        self._set_status("正在用 yt-dlp 解析…")
        self._refresh_action_button_colors()
        self._log_message(f"yt-dlp 解析：{url}", "INFO")
        engine = self._create_ydlp_engine(probe=True)
        self._probe_engine = engine
        # 预先读取 Tk 变量，供 yt-dlp 失败时回退浏览器使用
        headless = self.headless_var.get()
        scan_dom = self.scan_dom_var.get()
        ad_filter = self.ad_filter_var.get()
        dedup = self.dedup_var.get()
        threading.Thread(target=self._run_ytdlp_only,
                         args=(url, token, run_id, engine, headless, scan_dom, ad_filter, dedup),
                         daemon=True).start()

    def _run_ytdlp_only(self, url: str, token: int, run_id: int, engine=None,
                        headless=True, scan_dom=True, ad_filter=True, dedup=True):
        try:
            self._probe_ytdlp(url, token, engine)
            if token != self._sniff_token or self._stop_requested:
                return
            if self._ytdlp_resources:
                self._set_status("解析完成")
                return
            # yt-dlp 解析失败：自动回退到浏览器嗅探
            self._log_message("yt-dlp 未解析到格式，改用浏览器嗅探", "WARNING")
            self._run_sniff(url, headless, token, run_id,
                            None, scan_dom, ad_filter, dedup)
        finally:
            if run_id == self._sniff_run_id:
                was_stopped = self._stop_requested
                self.is_sniffing = False
                self._task_kind = None
                self._stop_requested = False
                if was_stopped:
                    self._set_status("已停止嗅探")
                self.update_resource_list()
                self.after(0, self._refresh_action_button_colors)

    # ---------- 微信视频号 ----------
    def _start_wechat_channels(self, url=None):
        """启动视频号专用解析（腾讯元宝官方登录态 → 视频号官方接口）"""
        if self.is_sniffing or self.is_downloading:
            return
        url = (url or self.url_entry.get().strip())
        if not url:
            return
        self.sniffer.clear()
        self._ytdlp_resources = []
        self._ytdlp_page_title = ""
        self.clear_resource_list()
        self.is_sniffing = True
        self._task_kind = 'wechat'
        self._stop_requested = False
        self._sniff_token += 1
        token = self._sniff_token
        self._sniff_run_id += 1
        run_id = self._sniff_run_id
        self._set_status("正在解析微信视频号…")
        self._refresh_action_button_colors()
        self._log_message(f"视频号解析：{url}", "INFO")
        threading.Thread(
            target=self._run_wechat_channels,
            args=(url, token, run_id),
            daemon=True).start()

    def _run_wechat_channels(self, url, token, run_id):
        try:
            resolver = WeChatChannelsResolver(
                log_callback=self._log_message,
                stop_check=lambda: self._stop_requested or token != self._sniff_token,
            )
            resources = resolver.resolve(url)
            if token != self._sniff_token or self._stop_requested:
                return
            for res in resources:
                score = res.quality_score
                self.sniffer.force_update_resource(res)
                # force_update_resource 会按 URL/尺寸重算分数，这里恢复视频号的
                # 编码优先级：H.264 兼容性最好，排在 H.265 前面。
                res.quality_score = score
            self.update_resource_list()
            self._set_status(f"视频号解析完成（{len(resources)} 个清晰度）")
        except WeChatChannelsStopped:
            if token == self._sniff_token:
                self._set_status("已停止解析")
        except WeChatChannelsLoginRequired as e:
            self._log_message(f"视频号解析需要登录腾讯元宝：{e}", "WARNING")
            if token == self._sniff_token:
                self._set_status("请扫码登录腾讯元宝后重试")
        except WeChatChannelsError as e:
            self._log_message(f"视频号解析失败：{e}", "ERROR")
            if token == self._sniff_token:
                self._set_status("视频号解析失败")
        except Exception as e:
            self._log_message(f"视频号解析异常：{e}", "ERROR")
            logger.warning("视频号解析异常", exc_info=True)
            if token == self._sniff_token:
                self._set_status("视频号解析失败")
        finally:
            if run_id == self._sniff_run_id:
                was_stopped = self._stop_requested
                self.is_sniffing = False
                self._task_kind = None
                self._stop_requested = False
                if was_stopped:
                    self._set_status("已停止解析")
                self.update_resource_list()
                self.after(0, self._refresh_action_button_colors)

    def _ydlp_browse_cookie(self):
        path = filedialog.askopenfilename(filetypes=[("Cookie 文件", "*.txt"), ("所有文件", "*.*")])
        if path:
            self._ydlp_cookie_path = path
            if hasattr(self, 'ydlp_cookie_lbl'):
                self.ydlp_cookie_lbl.config(text=os.path.basename(path))
            # 记住路径，下次启动自动恢复（抖音等站点没有 cookie 就用不了 yt-dlp）
            self.settings['ytdlp_cookie_path'] = path
            self._save_config()
            self._log_message(f"已加载 Cookie：{os.path.basename(path)}", "INFO")


    def _clear_queue(self):
        """清空下载队列（不停止当前正在执行的任务）"""
        count = 0
        while not self._download_queue.empty():
            try:
                self._download_queue.get_nowait()
                count += 1
            except queue.Empty:
                break
        if count > 0:
            self._log_message(f"已清空 {count} 个队列任务", "INFO")
        self._update_queue_count()

    def _update_queue_count(self):
        """更新清空队列链接状态和文字"""
        if not hasattr(self, 'clear_queue_link'):
            return
        count = self._download_queue.qsize()
        text = f"清空队列({count})" if count > 0 else "清空队列"
        self.clear_queue_link.config(text=text)
        if count > 0:
            self.clear_queue_link.config(fg=self.colors['error'], cursor='hand2')
        else:
            self.clear_queue_link.config(fg=self.colors['text_light'], cursor='arrow')

    def _toggle_log_panel(self):
        """显示/隐藏底部日志面板（隐藏后列表获得全部高度）"""
        if not hasattr(self, '_body_paned') or not hasattr(self, 'log_frame'):
            return
        if self._log_pane_visible:
            self._body_paned.forget(self.log_frame)
            self._log_pane_visible = False
        else:
            self._body_paned.add(self.log_frame, stretch='never', minsize=80, height=120)
            self._log_pane_visible = True
        self._refresh_action_button_colors()

    def _clear_log(self):
        """清空运行日志"""
        try:
            self.log_text.delete('1.0', tk.END)
        except Exception:
            pass

    def _validate_int(self, entry_widget, name, min_val, max_val):
        """校验整数输入框，返回 (ok, value)"""
        raw = entry_widget.get().strip()
        try:
            val = int(raw)
        except ValueError:
            messagebox.showerror("输入错误", f"{name} 必须是整数，当前输入: \"{raw}\"")
            return False, None
        if val < min_val or val > max_val:
            messagebox.showerror("输入错误", f"{name} 范围: {min_val}~{max_val}，当前: {val}")
            return False, None
        return True, val

    def _toggle_proxy_sniff(self):
        """启动/停止 mitmproxy 代理抓包"""
        if self.proxy_sniff_var.get():
            ok = self.proxy_sniffer.start()
            if not ok:
                self.proxy_sniff_var.set(False)
                messagebox.showerror("错误",
                    "代理启动失败。请确保已安装 mitmproxy：\npip install mitmproxy")
        else:
            self.proxy_sniffer.stop()

    def save_settings(self):
        """保存设置（含数字校验、aria2校验）"""
        # 3. 数字输入校验
        ok, skip_ad = self._validate_int(self.skip_ad_entry, "跳过广告分片数", 0, 200)
        if not ok: return
        ok, threads = self._validate_int(self.threads_entry, "下载线程数", 1, 32)
        if not ok: return
        ok, retry = self._validate_int(self.retry_entry, "下载重试次数", 0, 10)
        if not ok: return
        ok, speed = self._validate_int(self.speed_limit_entry, "下载限速", 0, 1048576)
        if not ok: return

        # 4. Aria2 校验
        aria2_path = self.aria2_entry.get().strip() if hasattr(self, 'aria2_entry') else ""
        use_aria2 = self.use_aria2_var.get() if hasattr(self, 'use_aria2_var') else False
        if use_aria2:
            if not aria2_path:
                messagebox.showerror("Aria2 错误", "已勾选启用 aria2 加速，但路径为空，请填写 aria2c 路径或取消勾选")
                return
            if not Path(aria2_path).exists() and not shutil.which(aria2_path):
                messagebox.showerror("Aria2 错误", f"aria2 路径不存在: {aria2_path}")
                return

        self.settings.update({
            "skip_ad_count": str(skip_ad),
            "default_threads": str(threads),
            "max_retry": str(retry),
            "ffmpeg_path": self.ffmpeg_entry.get(),
            "auto_filter_ad": self.ad_filter_var.get(),
            "auto_dedup": self.dedup_var.get(),
            "scan_dom": self.scan_dom_var.get(),
            "headless_browser": self.headless_var.get(),
            "auto_open_folder": self.auto_open_var.get(),
            "speed_limit": str(speed),
            "save_history": self.save_history_var.get(),
            "aria2_path": aria2_path,
            "use_aria2": use_aria2,
            "proxy": self.proxy_entry.get().strip(),
            "proxy_enabled": self.proxy_enabled_var.get(),
            "download_mode": self.download_mode_var.get(),
            "show_welcome": self.show_welcome_var.get(),
            "clipboard_monitor": self.clipboard_var.get(),
            "close_action": {'每次询问': 'ask', '最小化到后台': 'minimize',
                             '退出程序': 'exit'}.get(self.close_action_var.get(), 'ask'),
        })
        self._save_config()

        # 保存设置后重新检测 FFmpeg
        ffmpeg_path = self._resolve_ffmpeg_path()
        self.ffmpeg_available = UniversalDownloader.check_ffmpeg(ffmpeg_path)

        # 7. 配置生效提示
        ff_msg = ""
        if self.ffmpeg_available:
            ver = UniversalDownloader.get_ffmpeg_version(ffmpeg_path)
            ff_msg = "FFmpeg 检测通过" + (f"(版本: {ver})" if ver else "")
        else:
            ff_msg = "FFmpeg 不可用，合并功能将受限"
        messagebox.showinfo("设置已保存",
                            f"设置已保存！{ff_msg}\n\n"
                            f"⚠ 线程数、aria2、嗅探参数仅对新任务生效，\n"
                            f"正在运行的任务不受影响。")

    def _check_update(self):
        """检查程序版本更新"""
        if not UPDATE_CHECK_URL:
            messagebox.showinfo("检查更新",
                                f"当前版本 {APP_VERSION}\n\n"
                                "本版本未开通在线更新，请通过原有安装包渠道获取新版本。")
            return
        if not REQUESTS_AVAILABLE:
            messagebox.showerror("错误", "requests 库不可用，无法检查更新")
            return
        try:
            resp = requests.get(UPDATE_CHECK_URL, timeout=8,
                              headers={
                                  "Accept": "application/vnd.github.v3+json",
                                  "User-Agent": f"VideoDownloader/{APP_VERSION}"
                              })
            if resp.status_code == 200:
                data = resp.json()
                latest = data.get('tag_name', '').lstrip('v')
                release_url = data.get('html_url', '')
                release_notes = data.get('body', '暂无说明')
                # 修复：统一去 v 后按数值元组比较，避免字符串比较和 v 前缀不一致
                def _ver_t(v):
                    return tuple(int(x) for x in re.findall(r'\d+', v))
                current_ver = APP_VERSION.lstrip('v')
                if latest and _ver_t(latest) > _ver_t(current_ver):
                    msg = (f"发现新版本！\n\n"
                           f"当前版本: {APP_VERSION}\n"
                           f"最新版本: v{latest}\n\n"
                           f"更新内容:\n{release_notes[:500]}\n\n"
                           f"是否前往下载页面？")
                    if messagebox.askyesno("发现新版本", msg):
                        import webbrowser
                        webbrowser.open(release_url)
                else:
                    messagebox.showinfo("已是最新版本", f"当前版本 {APP_VERSION} 已是最新版本")
            else:
                messagebox.showwarning("检查失败", f"服务器返回状态码: {resp.status_code}")
        except requests.exceptions.Timeout:
            messagebox.showerror("检查失败", "连接更新服务器超时，请检查网络连接后重试")
        except Exception as e:
            messagebox.showerror("检查失败", f"无法连接更新服务器: {e}")

    def _batch_download(self):
        """批量下载选中的多个资源（自动用网页标题 + 清晰度命名）"""
        selected = self._selected_resources()
        if len(selected) < 2:
            messagebox.showwarning("提示", "请选择至少 2 个资源进行批量下载")
            return
        if self.is_downloading or self.is_sniffing:
            return

        init_dir = getattr(self, '_last_save_dir', '') or os.path.expanduser('~')
        save_dir = filedialog.askdirectory(initialdir=init_dir, title="选择保存目录")
        if not save_dir:
            return
        self._last_save_dir = save_dir

        if not UniversalDownloader.check_ffmpeg(self._resolve_ffmpeg_path()):
            messagebox.showerror("FFmpeg 不可用", "FFmpeg 未找到或不可用，请先在设置中配置正确的 FFmpeg 路径")
            return

        resources_to_download = []
        used_names = set()
        for idx, res in enumerate(selected, 1):
            base_name = self._default_save_name(res)
            suffix = (res.resolution or '').strip()
            if suffix and suffix not in ('自动', '仅音频'):
                candidate = f"{base_name} - {suffix}"
            else:
                candidate = base_name
            if candidate in used_names:
                candidate = f"{candidate} ({idx})"
            used_names.add(candidate)
            ext = self._suggest_ext(res)
            save_path = os.path.join(save_dir, candidate + ext)
            save_path = self._truncate_win_full_path(save_path)
            # 在主线程准备好下载器/引擎，后台只负责执行
            if res.engine == 'ytdlp':
                job = self._create_ydlp_engine()
            else:
                job = self._create_downloader()
                job.log_callback = self._log_message
                job.progress_callback = self._update_progress
            resources_to_download.append((res, save_path, job))

        for res, path, job in resources_to_download:
            self._download_queue.put((res, path, job))

        self._log_message(f"已加入 {len(resources_to_download)} 个下载任务", "INFO")
        self._update_queue_count()
        self._process_download_queue()

    def _process_download_queue(self):
        """串行处理下载队列（支持内置下载器 + yt-dlp）"""
        if self._queue_running or self._download_queue.empty():
            return

        self._queue_running = True
        self._queue_gen += 1
        gen = self._queue_gen
        self.is_downloading = True
        self._task_kind = 'download'
        self._stop_requested = False
        self._refresh_action_button_colors()
        self._set_status("正在批量下载…")
        audio_only = bool(self.audio_only_var.get())
        subtitles = bool(self.subtitles_var.get())

        def _queue_worker():
            results = []  # [(name, success)]
            while gen == self._queue_gen and not self._stop_requested and not self._download_queue.empty():
                try:
                    res, save_path, job = self._download_queue.get_nowait()
                except queue.Empty:
                    break
                task_name = os.path.basename(save_path)
                self._set_status(f"正在下载：{task_name}")
                self._log_message(f"队列下载：{task_name}", "INFO")
                success = False
                actual_path = save_path
                try:
                    if res.engine == 'ytdlp':
                        engine = job
                        with self._download_lock:
                            self._active_ytdlp_engine = engine
                        base, _ = os.path.splitext(save_path)
                        success = engine.download(
                            res.page_url or res.url, base,
                            res.format_spec or 'bv*+ba/b',
                            audio_only=audio_only, subtitles=subtitles)
                        if engine._last_file:
                            actual_path = engine._last_file
                        with self._download_lock:
                            self._active_ytdlp_engine = None
                    else:
                        downloader = job
                        with self._download_lock:
                            self.current_downloader = downloader
                        if audio_only:
                            success = downloader.extract_audio(res, save_path)
                        else:
                            success = downloader.download(res, save_path)
                        with self._download_lock:
                            self.current_downloader = None
                    self._save_history(res.url, actual_path,
                                       "success" if success else "failed", resource=res)
                    results.append((task_name, success))
                except Exception as e:
                    self._log_message(f"队列任务失败：{e}", "ERROR")
                    results.append((task_name, False))
                self.after(0, self._update_queue_count)

            stopped = self._stop_requested
            if gen != self._queue_gen:
                # 已被 stop/兜底换代：不要覆盖新任务的全局状态与收尾弹窗
                return
            with self._download_lock:
                self._queue_running = False
                self.is_downloading = False
                self._task_kind = None
                self._stop_requested = False
                self.current_downloader = None
                self._active_ytdlp_engine = None

            def _reset():
                self.progress_var.set(0)
                self._refresh_action_button_colors()
                self._update_queue_count()
                if stopped:
                    self._set_status("已停止下载")
                    return
                if not results:
                    self._set_status("就绪")
                    return
                success_count = sum(1 for _, ok in results if ok)
                fail_count = len(results) - success_count
                self._set_status("批量下载完成" if fail_count == 0 else "批量下载结束")
                if fail_count == 0:
                    messagebox.showinfo("批量下载完成", f"全部 {success_count} 个任务下载成功")
                else:
                    failed = "\n".join(f"  ✖ {name}" for name, ok in results if not ok)
                    messagebox.showwarning(
                        "批量下载结束",
                        f"成功 {success_count} 个，失败 {fail_count} 个：\n{failed}")
            self.after(0, _reset)

        threading.Thread(target=_queue_worker, daemon=True).start()


    def _import_local_m3u8(self):
        """导入本地 m3u8 文件进行解析下载"""
        path = filedialog.askopenfilename(
            filetypes=[("M3U8 文件", "*.m3u8"), ("所有文件", "*.*")],
            title="选择本地 M3U8 文件"
        )
        if not path:
            return

        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()

            # 使用 M3U8Parser 解析
            # 修复：用 file:// URI 作为 base_url，使 urljoin 能正确拼接同目录相对路径
            base_url = Path(path).resolve().as_uri()
            parser = M3U8Parser(base_url)
            streams = parser.parse(content, base_url)

            if not streams:
                messagebox.showwarning("提示", "未在 m3u8 文件中找到有效内容")
                return

            # 检查是否是 master playlist
            if streams[0].is_master:
                self._log_message(f"检测到主 M3U8，共 {len(streams)} 个流，已全部导入", "INFO")
                for stream in streams:
                    res = VideoResource(
                        url=stream.url,
                        resource_type='m3u8',
                        title=f"本地M3U8 ({os.path.basename(path)}) {stream.resolution}",
                        resolution=stream.resolution,
                        bandwidth=stream.bandwidth,
                        source='manual'
                    )
                    self.sniffer.add_resource(res)
            elif streams[0].segments:
                # 普通 m3u8 分片列表
                res = VideoResource(
                    url=path,
                    resource_type='m3u8',
                    title=f"本地M3U8 ({os.path.basename(path)})",
                    source='manual'
                )
                if self.sniffer.add_resource(res):
                    self._log_message(f"已导入本地 M3U8: {os.path.basename(path)}", "INFO")
                else:
                    self._log_message("导入 M3U8 失败（可能重复）", "WARNING")
            else:
                messagebox.showwarning("提示", "未在 m3u8 文件中找到分片信息")
                return

            self.update_resource_list()
        except Exception as e:
            messagebox.showerror("导入失败", str(e))

    def _download_subtitle(self):
        """下载选中资源的字幕"""
        if not self.selected_resource:
            return
        res = self.selected_resource
        if res.resource_type != 'm3u8':
            messagebox.showinfo("提示", "字幕下载仅支持 M3U8 资源")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".srt",
            filetypes=[("SRT 字幕", "*.srt"), ("WebVTT", "*.vtt"), ("所有文件", "*.*")],
            title="保存字幕"
        )
        if not path:
            return

        downloader = self._create_downloader()
        downloader.log_callback = self._log_message
        downloader.progress_callback = self._update_progress

        def _worker():
            try:
                # 先解析 m3u8 找字幕轨道
                content = downloader._download_content(res.url, headers=res.headers)
                if not content:
                    self.after(0, lambda: messagebox.showerror("失败", "M3U8 清单下载失败"))
                    return
                parser = M3U8Parser(res.url)
                streams = parser.parse(content, res.url)
                sub_streams = [s for s in streams if s.media_type == 'SUBTITLES' and s.subtitle_url]
                if not sub_streams:
                    # 检查 EXT-X-MEDIA URI
                    sub_streams = [s for s in streams if s.media_type == 'SUBTITLES' and s.url]
                if not sub_streams:
                    self.after(0, lambda: messagebox.showinfo("提示", "该 M3U8 未包含字幕轨道"))
                    return
                sub_url = sub_streams[0].subtitle_url or sub_streams[0].url
                ok = downloader.download_subtitle(sub_url, path, res.headers)
                if ok:
                    self.after(0, lambda: messagebox.showinfo("成功", f"字幕已保存：\n{path}"))
                else:
                    self.after(0, lambda: messagebox.showerror("失败", "字幕下载失败"))
            except Exception as e:
                self._log_message(f"字幕下载异常: {e}", "ERROR")

        threading.Thread(target=_worker, daemon=True).start()

    def _export_log(self):
        """导出运行日志到文件"""
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
            title="导出日志"
        )
        if not path:
            return
        try:
            content = self.log_text.get(1.0, tk.END)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(content)
            self._log_message(f"日志已导出到: {path}", "INFO")
            messagebox.showinfo("成功", f"日志已导出到:\n{path}")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))

    def create_history_page(self):
        """创建下载历史页面（双击查看下载属性）"""
        c = self.colors
        main = tk.Frame(self.history_frame, bg=c['bg_card'])
        main.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

        title_frame = tk.Frame(main, bg=c['bg_card'])
        title_frame.pack(fill=tk.X, pady=(0, 12))
        self.history_title_lbl = tk.Label(title_frame, text="下载历史", font=("微软雅黑", 16, "bold"),
                 fg=c['text'], bg=c['bg_card'])
        self.history_title_lbl.pack(side=tk.LEFT)
        self.history_count_lbl = tk.Label(title_frame, text="共 0 条", font=("微软雅黑", 9),
                                          fg=c['text_light'], bg=c['bg_card'])
        self.history_count_lbl.pack(side=tk.LEFT, padx=10)
        ttk.Button(title_frame, text="刷新", style='Secondary.TButton',
                   command=self._refresh_history).pack(side=tk.RIGHT, padx=5)
        ttk.Button(title_frame, text="清空历史", style='Secondary.TButton',
                   command=self._clear_history).pack(side=tk.RIGHT, padx=5)

        # 列表容器（用于叠加空状态）
        list_container = tk.Frame(main, bg=c['bg_card'],
                                  highlightbackground=c['border'], highlightthickness=1)
        list_container.pack(fill=tk.BOTH, expand=True)

        self.history_tree = ttk.Treeview(
            list_container,
            columns=("file", "size", "status", "time", "path"),
            style='Resource.Treeview', show='headings', selectmode='browse')
        col_cfg = [("file", "文件名", 260, tk.W, True),
                   ("size", "大小", 100, tk.CENTER, False),
                   ("status", "状态", 80, tk.CENTER, False),
                   ("time", "下载时间", 155, tk.CENTER, False),
                   ("path", "保存位置", 330, tk.W, True)]
        for cid, title, width, anchor, stretch in col_cfg:
            self.history_tree.heading(cid, text=title)
            self.history_tree.column(cid, width=width, anchor=anchor, stretch=stretch)

        hsb = ttk.Scrollbar(list_container, orient=tk.HORIZONTAL, command=self.history_tree.xview)
        vsb = ttk.Scrollbar(list_container, orient=tk.VERTICAL, command=self.history_tree.yview)
        self.history_tree.configure(xscrollcommand=hsb.set, yscrollcommand=vsb.set)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.history_tree.pack(fill=tk.BOTH, expand=True)
        self.history_tree.bind('<Double-1>', self._on_history_double_click)
        self.history_tree.bind('<Return>', self._on_history_double_click)
        self.history_tree.bind('<Button-3>', self._on_history_right_click)

        # 空状态占位
        self.history_empty = tk.Frame(list_container, bg=c['bg_card'])
        tk.Label(self.history_empty, text="📭", font=("Segoe UI Emoji", 48),
                 bg=c['bg_card'], fg=c['text_light']).pack(pady=(80, 10))
        tk.Label(self.history_empty, text="暂无下载记录", font=("微软雅黑", 14),
                 bg=c['bg_card'], fg=c['text_light']).pack()
        tk.Label(self.history_empty, text="下载完成的视频会自动记录在这里，双击可查看属性",
                 font=("微软雅黑", 9), bg=c['bg_card'], fg=c['text_light']).pack(pady=5)

        self._history_by_iid = {}
        self._refresh_history()

    @staticmethod
    def _format_history_size(size) -> str:
        try:
            size = int(size or 0)
        except (TypeError, ValueError):
            size = 0
        if size <= 0:
            return "-"
        human = _human_bytes(size)
        return f"{size:,} Bytes（{human}）" if human else f"{size:,} Bytes"

    @staticmethod
    def _format_history_size_short(size) -> str:
        try:
            size = int(size or 0)
        except (TypeError, ValueError):
            size = 0
        if size <= 0:
            return "-"
        return _human_bytes(size) or f"{size / 1024:.0f} KB"

    @staticmethod
    def _history_status_text(status: str) -> str:
        return {'success': '已完成', 'failed': '失败'}.get(status, status or '未知')

    def _refresh_history(self):
        """刷新下载历史列表"""
        if not hasattr(self, 'history_tree'):
            return
        for item in self.history_tree.get_children():
            self.history_tree.delete(item)
        self._history_by_iid = {}
        history = self._load_history()
        if not isinstance(history, list):
            history = []
        for idx, record in enumerate(history):
            if not isinstance(record, dict):
                continue
            path = record.get('path', '') or ''
            file_name = (record.get('file_name') or
                         (os.path.basename(path) if path else '') or
                         record.get('title') or '未知文件')
            size = record.get('size') or 0
            if not size and path:
                try:
                    if os.path.exists(path):
                        size = os.path.getsize(path)
                except Exception:
                    size = 0
            status = self._history_status_text(record.get('status', ''))
            time_text = (record.get('last_try') or record.get('time') or
                         record.get('added_on') or '')
            item_id = self.history_tree.insert(
                '', tk.END,
                values=(file_name, self._format_history_size_short(size), status, time_text, path),
                tags=('row_even' if idx % 2 == 0 else 'row_odd',))
            self._history_by_iid[item_id] = record
        if hasattr(self, 'history_count_lbl'):
            self.history_count_lbl.config(text=f"共 {len(self._history_by_iid)} 条")
        if hasattr(self, 'history_empty'):
            if self._history_by_iid:
                self.history_empty.place_forget()
            else:
                self.history_empty.place(relx=0.5, rely=0.5, anchor=tk.CENTER)

    def _on_history_double_click(self, event=None):
        """双击历史记录：打开下载属性窗口"""
        item = None
        if event is not None and hasattr(event, 'y'):
            item = self.history_tree.identify_row(event.y)
        if not item:
            selected = self.history_tree.selection()
            item = selected[0] if selected else None
        if not item:
            return
        self.history_tree.selection_set(item)
        record = self._history_by_iid.get(item)
        if record:
            self._show_history_properties(record)

    def _on_history_right_click(self, event):
        item = self.history_tree.identify_row(event.y)
        if not item:
            return
        self.history_tree.selection_set(item)
        record = self._history_by_iid.get(item)
        if not record:
            return
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="下载属性", command=lambda: self._show_history_properties(record))
        menu.add_command(label="打开文件", command=lambda: self._open_history_file(record))
        menu.add_command(label="打开所在文件夹", command=lambda: self._open_history_folder(record))
        menu.add_separator()
        menu.add_command(label="复制下载链接", command=lambda: self._copy_history_url(record))
        menu.add_command(label="复制保存路径", command=lambda: self._copy_history_path(record))
        menu.add_separator()
        menu.add_command(label="删除这条记录", command=lambda: self._delete_history_record(record))
        menu.tk_popup(event.x_root, event.y_root)

    def _open_history_file(self, record):
        path = record.get('path', '') or ''
        if not path or not os.path.exists(path):
            messagebox.showwarning("文件不存在", f"文件不存在或已被移动：\n{path}")
            return
        try:
            if sys.platform == 'win32':
                os.startfile(path)
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', path])
            else:
                subprocess.Popen(['xdg-open', path])
        except Exception as e:
            messagebox.showerror("打开失败", str(e))

    def _open_history_folder(self, record):
        path = record.get('path', '') or ''
        folder = os.path.dirname(os.path.abspath(path)) if path else ''
        if not folder or not os.path.isdir(folder):
            messagebox.showwarning("文件夹不存在", f"保存目录不存在：\n{folder}")
            return
        try:
            if sys.platform == 'win32':
                os.startfile(folder)
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', folder])
            else:
                subprocess.Popen(['xdg-open', folder])
        except Exception as e:
            messagebox.showerror("打开失败", str(e))

    def _open_history_page(self, record):
        url = record.get('page_url') or record.get('url') or ''
        if not url:
            messagebox.showinfo("提示", "没有可打开的页面地址")
            return
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception as e:
            messagebox.showerror("打开失败", str(e))

    def _copy_history_url(self, record):
        url = record.get('url', '') or record.get('page_url', '')
        if not url:
            return
        self.clipboard_clear()
        self.clipboard_append(url)
        self._last_clipboard = url
        self._log_message("已复制下载链接", "INFO")

    def _copy_history_path(self, record):
        path = record.get('path', '')
        if not path:
            return
        self.clipboard_clear()
        self.clipboard_append(path)
        self._last_clipboard = path
        self._log_message("已复制保存路径", "INFO")

    def _open_log_file(self):
        try:
            if os.path.exists(_log_file):
                if sys.platform == 'win32':
                    os.startfile(_log_file)
                elif sys.platform == 'darwin':
                    subprocess.Popen(['open', _log_file])
                else:
                    subprocess.Popen(['xdg-open', _log_file])
            else:
                messagebox.showinfo("提示", "日志文件尚未生成")
        except Exception as e:
            messagebox.showerror("打开失败", str(e))

    def _delete_history_record(self, record):
        if not messagebox.askyesno("确认", "确定删除这条下载历史吗？\n（不会删除已下载文件）"):
            return
        path = os.path.normcase(os.path.abspath(record.get('path', ''))) if record.get('path') else ''
        try:
            removed = False
            with self._history_lock:
                history = self._load_history()
                new_history = []
                for rec in history:
                    rec_path = rec.get('path', '') if isinstance(rec, dict) else ''
                    rec_norm = os.path.normcase(os.path.abspath(rec_path)) if rec_path else ''
                    if not removed and path and rec_norm == path:
                        removed = True
                        continue
                    new_history.append(rec)
                if removed:
                    self._write_history(new_history)
            self._refresh_history()
            if not removed:
                messagebox.showinfo("提示", "这条历史记录已不存在（可能已被删除或更新）")
        except Exception as e:
            messagebox.showerror("删除失败", str(e))

    def _clear_history(self):
        """清空下载历史（不删除文件）"""
        if not messagebox.askyesno("确认", "确定要清空所有下载历史吗？\n（不会删除已下载文件）"):
            return
        try:
            with self._history_lock:
                self._write_history([])
            self._refresh_history()
            self._log_message("下载历史已清空", "INFO")
        except Exception as e:
            messagebox.showerror("错误", str(e))

    def _show_history_properties(self, record):
        """显示类似 IDM 的下载属性窗口（浅色独立窗口，不受深色主题影响）"""
        path = record.get('path', '') or ''
        file_name = (record.get('file_name') or
                     (os.path.basename(path) if path else '') or
                     record.get('title') or '未知文件')
        file_exists = bool(path and os.path.exists(path))
        size = record.get('size') or 0
        if not size and file_exists:
            try:
                size = os.path.getsize(path)
            except Exception:
                size = 0
        status_raw = record.get('status', '')
        status_text = self._history_status_text(status_raw)
        if status_raw == 'success' and not file_exists:
            status_text = "已完成（文件不存在）"
        url = record.get('url', '') or ''
        page_url = record.get('page_url', '') or url
        page_title = (record.get('page_title', '') or record.get('title', '') or
                      os.path.splitext(file_name)[0])
        browser = record.get('browser') or record.get('engine') or '内置浏览器'
        added_on = record.get('added_on') or record.get('time', '') or ''
        last_try = record.get('last_try') or record.get('time', '') or ''

        BG = '#ffffff'
        HEAD_BG = '#f2f7ff'
        LABEL_FG = '#5c6bc0'
        VALUE_FG = '#1a237e'

        dlg = tk.Toplevel(self)
        dlg.title("下载属性")
        dlg.geometry("760x600")
        dlg.minsize(700, 560)
        dlg.transient(self)
        dlg.configure(bg=BG)

        # ---------- 顶部文件信息 ----------
        header = tk.Frame(dlg, bg=HEAD_BG, padx=20, pady=16)
        header.pack(fill=tk.X)
        ext = os.path.splitext(file_name)[1].lower()
        if ext in ('.mp4', '.mkv', '.webm', '.flv', '.ts', '.mov', '.avi', '.m4v'):
            icon_text, icon_color = "▶", "#1e88e5"
        elif ext in ('.mp3', '.m4a', '.aac', '.ogg', '.wav', '.flac'):
            icon_text, icon_color = "♪", "#8e24aa"
        elif ext in ('.zip', '.7z', '.rar'):
            icon_text, icon_color = "▤", "#ef6c00"
        else:
            icon_text, icon_color = "▣", "#546e7a"
        tk.Label(header, text=icon_text, font=("微软雅黑", 26, "bold"),
                 bg=HEAD_BG, fg=icon_color, width=2).pack(side=tk.LEFT, padx=(0, 12))
        tk.Label(header, text=file_name, font=("微软雅黑", 12, "bold"),
                 bg=HEAD_BG, fg=VALUE_FG, anchor=tk.W, justify=tk.LEFT,
                 wraplength=620).pack(side=tk.LEFT, fill=tk.X, expand=True)

        # ---------- 内容区 ----------
        body = tk.Frame(dlg, bg=BG, padx=20, pady=10)
        body.pack(fill=tk.BOTH, expand=True)
        body.grid_columnconfigure(1, weight=1)
        small = ("微软雅黑", 9)

        def label_row(row, label, value, color=None):
            tk.Label(body, text=label, font=small, bg=BG, fg=LABEL_FG,
                     anchor=tk.W, width=11).grid(row=row, column=0, sticky='w', pady=6)
            tk.Label(body, text=value or '-', font=small, bg=BG,
                     fg=color or VALUE_FG, anchor=tk.W, justify=tk.LEFT,
                     wraplength=520).grid(row=row, column=1, columnspan=2,
                                          sticky='we', pady=6)

        def entry_row(row, label, value, buttons=()):
            tk.Label(body, text=label, font=small, bg=BG, fg=LABEL_FG,
                     anchor=tk.W, width=11).grid(row=row, column=0, sticky='w', pady=6)
            var = tk.StringVar(value=value or '')
            entry = tk.Entry(body, textvariable=var, state='readonly', font=small,
                             bg='#f7f9fc', fg=VALUE_FG, readonlybackground='#f7f9fc',
                             relief=tk.SOLID, bd=1, highlightthickness=0)
            entry._history_var = var  # 防止 StringVar 被回收后内容变空
            entry.grid(row=row, column=1, sticky='we', pady=6, padx=(0, 8), ipady=4)
            btn_frame = tk.Frame(body, bg=BG)
            btn_frame.grid(row=row, column=2, sticky='e')
            for text, cmd in buttons:
                tk.Button(btn_frame, text=text, font=("微软雅黑", 8),
                          bg='#ffffff', fg='#1565c0', activebackground='#e3f2fd',
                          activeforeground='#0d47a1', relief=tk.SOLID, bd=1,
                          cursor='hand2', padx=8, pady=2,
                          command=cmd).pack(side=tk.LEFT, padx=(0, 4))

        label_row(0, "文件大小", self._format_history_size(size))
        status_color = '#2e7d32' if status_raw == 'success' else (
            '#c62828' if status_raw == 'failed' else '#ef6c00')
        label_row(1, "状态", status_text, status_color)
        entry_row(2, "保存到", path,
                  [("打开文件夹", lambda: self._open_history_folder(record))])
        entry_row(3, "下载 URL", url,
                  [("复制", lambda: self._copy_history_url(record))])
        entry_row(4, "页面 URL", page_url,
                  [("显示页面", lambda: self._open_history_page(record))])
        entry_row(5, "页面标题", page_title)
        label_row(6, "浏览器", browser)

        tk.Frame(body, bg='#e3eaf3', height=1).grid(
            row=7, column=0, columnspan=3, sticky='we', pady=10)
        label_row(8, "添加时间", added_on)
        label_row(9, "最近尝试", last_try)

        # ---------- 底部按钮 ----------
        footer = tk.Frame(dlg, bg=BG, padx=20, pady=12)
        footer.pack(fill=tk.X)

        def add_btn(text, cmd, primary=False):
            if primary:
                b = tk.Button(footer, text=text, font=("微软雅黑", 9, "bold"),
                              bg='#1e88e5', fg='white', activebackground='#1565c0',
                              activeforeground='white', relief=tk.FLAT,
                              cursor='hand2', padx=18, pady=6, command=cmd)
                b.pack(side=tk.RIGHT, padx=5)
            else:
                b = tk.Button(footer, text=text, font=("微软雅黑", 9),
                              bg='#ffffff', fg='#1565c0', activebackground='#e3f2fd',
                              activeforeground='#0d47a1', relief=tk.SOLID, bd=1,
                              cursor='hand2', padx=14, pady=6, command=cmd)
                b.pack(side=tk.LEFT, padx=5)
            return b

        add_btn("关闭", dlg.destroy, primary=True)
        add_btn("日志文件", self._open_log_file)
        add_btn("打开文件夹", lambda: self._open_history_folder(record))
        add_btn("打开文件", lambda: self._open_history_file(record))

        dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)
        dlg.update_idletasks()
        x = self.winfo_rootx() + max(0, (self.winfo_width() - dlg.winfo_width()) // 2)
        y = self.winfo_rooty() + max(0, (self.winfo_height() - dlg.winfo_height()) // 2)
        dlg.geometry(f"+{x}+{y}")
        try:
            dlg.grab_set()
            dlg.focus_force()
        except Exception:
            pass


    def _log_message(self, message: str, level: str = "INFO"):
        """日志输出（线程安全，转发到主线程执行）"""
        try:
            self.after(0, self._do_log_message, message, level)
        except Exception:
            pass

    def _do_log_message(self, message: str, level: str = "INFO"):
        """实际执行日志输出（精简版：DEBUG 只进文件、合并重复、只保留 1200 行）"""
        text = str(message).strip()
        if not text:
            return
        if level == "DEBUG":
            # 技术细节（导航、档位补齐等）只写文件，不刷界面
            logger.debug(text)
            return
        now = time.time()
        last_text = getattr(self, '_last_log_text', '')
        last_time = getattr(self, '_last_log_time', 0.0)
        if level == "INFO" and text == last_text and (now - last_time) < 3.0:
            return
        self._last_log_text = text
        self._last_log_time = now

        _level_map = {"INFO": logging.INFO, "WARNING": logging.WARNING, "ERROR": logging.ERROR}
        logger.log(_level_map.get(level, logging.INFO), text)
        color_map = {"INFO": '#d7dce5', "WARNING": '#ffcc80', "ERROR": '#ff8a80'}
        prefix = "" if level == "INFO" else ("⚠ " if level == "WARNING" else "✖ ")
        try:
            self.log_text.insert(tk.END, f"{prefix}{text}\n", level)
            self.log_text.tag_config(level, foreground=color_map.get(level, self.colors['text']))
            if float(self.log_text.index(tk.END)) > 1202:
                self.log_text.delete(1.0, f"{float(self.log_text.index(tk.END)) - 1202}.0")
            self.log_text.see(tk.END)
        except Exception:
            pass

    def _update_progress(self, value: float, message: str = ""):
        """更新进度条（线程安全，转发到主线程执行）"""
        try:
            self.after(0, lambda v=value: self.progress_var.set(v))
        except Exception:
            pass

# ==================== 启动程序 ====================
if __name__ == "__main__":
    if not REQUESTS_AVAILABLE:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("依赖缺失", "请安装依赖：pip install requests")
        sys.exit(1)

    if not CRYPTO_AVAILABLE:
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning("提示", "pycryptodome 未安装，AES-128 加密的 M3U8 无法本地解密。\n请执行: pip install pycryptodome")

    if not PLAYWRIGHT_AVAILABLE:
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning("提示", "Playwright 未安装，浏览器嗅探功能不可用。\n请执行: pip install playwright")

    app = VideoDownloaderApp()
    app.mainloop()
