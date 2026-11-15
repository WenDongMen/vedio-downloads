import importlib.util
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

V45 = r"F:\MenWenDong\视频下载器\V4.5.py"

DEFAULT_SETTINGS = {
    "skip_ad_count": "0", "max_retry": "3", "timeout": "30", "default_threads": "16",
    "ffmpeg_path": "ffmpeg", "auto_filter_ad": True, "auto_dedup": True,
    "scan_dom": True, "auto_headers": True, "headless_browser": True,
    "auto_open_folder": False, "speed_limit": "0", "save_history": True,
    "aria2_path": "", "use_aria2": False, "proxy": "", "proxy_enabled": False,
    "download_mode": "auto", "show_welcome": True, "clipboard_monitor": False,
    "close_action": "ask", "batch_save_dir": "", "batch_quality": "自动最佳",
    "batch_skip_existing": True, "ytdlp_cookie_path": "", "live_mode": False,
    # 背景效果必须进默认表，否则 load_settings 会把不认识的键过滤掉，重启就回默认
    "backdrop": "无", "radius": 8,
    "audio_only": False, "download_subtitles": False,
    # 更新检查：地址留空表示关闭（app.py 里还有一个内置常量兜底）
    "check_update": True, "update_url": "", "update_skip": "",
}


def _find_core():
    for p in (os.environ.get("VD_CORE", ""), V45, str(Path(__file__).with_name("V4.5.py"))):
        if p and os.path.isfile(p):
            return p
    raise FileNotFoundError("找不到 V4.5.py，请设置环境变量 VD_CORE 或放到 V5.0 目录")


def _backend():
    m = sys.modules.get("vd_core")
    if m:
        return m
    path = _find_core()
    spec = importlib.util.spec_from_file_location("vd_core", path)
    m = importlib.util.module_from_spec(spec)
    sys.modules["vd_core"] = m
    spec.loader.exec_module(m)
    return m


vd = _backend()

DATA_DIR = Path(vd._app_data_dir())
CONFIG = DATA_DIR / "downloader_config.json"
HISTORY = DATA_DIR / "download_history.json"

YTDLP_FIRST = vd.VideoDownloaderApp.YTDLP_FIRST_HOSTS
YTDLP_COOKIE_REQ = vd.VideoDownloaderApp.YTDLP_COOKIE_REQUIRED_HOSTS


def _host_in(url, domains):
    try:
        host = urlparse(url).netloc.lower().split(":")[0]
    except Exception:
        return False
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in domains)


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        if CONFIG.exists():
            data = json.loads(CONFIG.read_text("utf-8"))
            s.update({k: v for k, v in data.items() if k in s})
    except Exception:
        pass
    return s


def save_settings(s):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, CONFIG)


def load_history():
    try:
        if HISTORY.exists():
            data = json.loads(HISTORY.read_text("utf-8"))
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def _write_history(items):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = HISTORY.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, HISTORY)


_HIST_LOCK = threading.Lock()


def add_history(url, path, status="success", resource=None):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    abs_path = os.path.abspath(path) if path else ""
    size = 0
    try:
        if abs_path and os.path.exists(abs_path):
            size = os.path.getsize(abs_path)
    except Exception:
        size = 0
    title = getattr(resource, "title", "") if resource else ""
    record = {
        "url": getattr(resource, "page_url", "") or url or "",
        "page_url": getattr(resource, "page_url", "") or url or "",
        "page_title": getattr(resource, "page_title", "") or title,
        "title": title or os.path.basename(abs_path),
        "path": abs_path,
        "file_name": os.path.basename(abs_path),
        "size": size,
        "time": now,
        "added_on": now,
        "last_try": now,
        "status": status,
        "engine": "yt-dlp" if getattr(resource, "engine", "") == "ytdlp" else "内置下载器",
        "browser": "内置浏览器（Chromium）",
        "source": getattr(resource, "source", "") if resource else "",
    }
    # 主下载引擎和批量页是两个 Engine，各自开线程，都会写同一个 history 文件；
    # 读-改-写不串行的话并发时后写的会覆盖先写的，丢记录。
    with _HIST_LOCK:
        items = load_history()
        for i, old in enumerate(items):
            if abs_path and old.get("path") and os.path.normcase(os.path.abspath(old["path"])) == os.path.normcase(abs_path):
                # 只有同状态才覆盖：失败记录不能把同名的成功记录顶掉（反之亦然）
                if old.get("status", "success") != status:
                    continue
                record["added_on"] = old.get("added_on") or old.get("time") or now
                items[i] = record
                break
        else:
            items.insert(0, record)
        _write_history(items)
    return record


def delete_history(path):
    with _HIST_LOCK:
        items = [x for x in load_history()
                 if os.path.normcase(os.path.abspath(x.get("path", ""))) != os.path.normcase(os.path.abspath(path))]
        _write_history(items)


def clear_history():
    with _HIST_LOCK:
        _write_history([])


def resolve_ffmpeg(settings):
    r"""定位 ffmpeg.exe。

    除了设置里的路径、程序目录、当前工作目录，再兜底看「后端 V4.5 所在目录」和
    数据目录——ffmpeg.exe 实际就放在 V4.5 同目录下，旧实现只认
    V5.0 目录，配置一丢就会退回裸命令 ffmpeg（PATH 里通常没有），
    接着 HLS 合并 / 音视频合并 / 提取音轨全部失败。
    """
    raw = str(settings.get("ffmpeg_path", "ffmpeg") or "ffmpeg")
    if os.path.isfile(raw):
        return os.path.abspath(raw)
    candidates = []
    try:
        candidates.append(Path(os.getcwd()) / "ffmpeg.exe")
    except Exception:
        pass
    candidates.append(Path(__file__).with_name("ffmpeg.exe"))
    try:
        candidates.append(Path(_find_core()).with_name("ffmpeg.exe"))
    except Exception:
        pass
    candidates.append(DATA_DIR / "ffmpeg.exe")
    for p in candidates:
        try:
            if p.exists():
                return str(p)
        except Exception:
            continue
    found = shutil.which("ffmpeg")
    if found:
        return found
    return raw


def human_size(n):
    try:
        n = float(n)
    except Exception:
        return "未知"
    if n <= 0:
        return "未知"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


class Engine:
    def __init__(self, settings=None):
        self.settings = settings or load_settings()
        self.events = queue.Queue()
        self.sniffer = vd.SmartSniffer()
        self.monitor = vd.BrowserMonitor(self.sniffer, log_callback=self._log, update_callback=self._touch)
        self.downloader = None
        self.ytdlp = None
        self.proxy = None
        self.busy_sniff = False
        self.busy_dl = False
        self.current = None
        self._ext = []
        self._jobs = queue.Queue()
        self._worker = None
        # 每次嗅探发一个自增编号：用户点“停止”后，还在跑的嗅探线程（尤其是
        # yt-dlp 那条，没法中断）完成时会带着旧编号回来，直接丢弃结果，
        # 否则会把界面状态从“已停止”又改回“解析完成”。
        self._sniff_token = 0

    def _log(self, message, level="INFO"):
        self.events.put(("log", (str(level), str(message))))

    def _proxy(self):
        """代理地址：只有开了开关且填了地址才返回，否则空串（保持直连）。"""
        if self.settings.get("proxy_enabled") and self.settings.get("proxy"):
            return str(self.settings["proxy"])
        return ""

    def _touch(self):
        self.events.put(("resources", None))

    def _status(self, text):
        self.events.put(("status", text))

    def _ensure_worker(self):
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def _loop(self):
        while True:
            try:
                job = self._jobs.get(timeout=600)
            except queue.Empty:
                try:
                    self.monitor.close()
                except Exception:
                    pass
                continue
            if job is None:
                break
            try:
                job()
            except Exception as e:
                self._log(f"嗅探线程异常：{e}", "ERROR")

    def resources(self):
        base = []
        try:
            base = list(self.sniffer.get_sorted_resources())
        except Exception:
            base = []
        seen = {r.url for r in base}
        return base + [r for r in self._ext if r.url not in seen]

    def _mark(self, token=None):
        if token is not None and token != self._sniff_token:
            return
        self.events.put(("resources", None))
        n = len(self.resources())
        self._status("解析完成" if n else "未找到可下载资源")

    def _release_sniff(self, token=None):
        """只有当前这一代嗅探结束时才复位 busy 标记。"""
        if token is None or token == self._sniff_token:
            self.busy_sniff = False

    def sniff(self, url, headless=None):
        if self.busy_sniff:
            self._log("正在嗅探中，请先停止当前任务", "WARNING")
            return False
        if not url or not url.lower().startswith(("http://", "https://")):
            self._log("请输入有效的网页地址", "ERROR")
            return False
        if vd.WeChatChannelsResolver.is_channels_url(url):
            self.busy_sniff = True
            self.sniffer.clear()
            self._ext = []
            self._sniff_token += 1
            tok = self._sniff_token
            self.events.put(("resources", None))
            self._status("正在解析视频号…")
            self._log(f"开始解析视频号：{url}", "INFO")
            self._ensure_worker()
            self._jobs.put(lambda: self._wechat_job(url, tok))
            return True
        if headless is None:
            headless = bool(self.settings.get("headless_browser", True))
        self.busy_sniff = True
        self.sniffer.clear()
        self.current = None
        self._ext = []
        self._sniff_token += 1
        tok = self._sniff_token
        self.events.put(("resources", None))
        self._status("正在嗅探…")
        self._log(f"开始解析：{url}", "INFO")

        cookie = str(self.settings.get("ytdlp_cookie_path", "") or "")
        use_ytdlp = _host_in(url, YTDLP_FIRST) and (not _host_in(url, YTDLP_COOKIE_REQ) or os.path.isfile(cookie))
        if use_ytdlp:
            threading.Thread(target=self._ytdlp_job, args=(url, cookie, tok), daemon=True).start()
        else:
            self._ensure_worker()
            self._jobs.put(lambda u=url, h=headless, t=tok: self._sniff_job(u, h, t))
        return True

    def _sniff_job(self, url, headless, token=None):
        try:
            if token is not None and token != self._sniff_token:
                return
            self.sniffer.ad_filter_enabled = bool(self.settings.get("auto_filter_ad", True))
            self.sniffer.dedup_enabled = bool(self.settings.get("auto_dedup", True))
            self.monitor.scan_dom = bool(self.settings.get("scan_dom", True))
            self.monitor.start(url, headless=headless)
            title = self.monitor.get_page_title() or ""
            page_url = self.monitor.get_page_url() or url
            with self.sniffer.lock:
                for res in self.sniffer.captured_resources.values():
                    res.page_url = res.page_url or page_url
                    if title:
                        res.page_title = res.page_title or title
                        if not res.title:
                            res.title = title
            self._mark(token)
        finally:
            self._release_sniff(token)

    def _wechat_job(self, url, token=None):
        try:
            if token is not None and token != self._sniff_token:
                return
            resolver = vd.WeChatChannelsResolver(log_callback=self._log)
            resources = resolver.resolve(url)
            self._ext = resources
            self._log(f"视频号解析：{len(resources)} 个可下载地址", "INFO")
            self._mark(token)
        except Exception as e:
            self._log(f"视频号解析失败：{e}", "ERROR")
            if token is None or token == self._sniff_token:
                self._status("视频号解析失败")
        finally:
            self._release_sniff(token)

    def _ytdlp_job(self, url, cookie, token=None):
        try:
            if token is not None and token != self._sniff_token:
                return
            engine = vd.YTDLPEngine(ffmpeg_location=resolve_ffmpeg(self.settings), cookiefile=cookie,
                                    proxy=self._proxy(),
                                    log_callback=self._log, progress_callback=lambda v, m="": None)
            info = engine.extract_info(url)
            title, resources = vd.YTDLPEngine.build_video_resources(info, url)
            if resources:
                for res in resources:
                    res.quality_score = self.sniffer._calculate_quality_score(res)
                    res.page_title = res.page_title or title
                    res.page_url = res.page_url or url
                if token is not None and token != self._sniff_token:
                    return
                self._ext = resources
                self._log(f"yt-dlp 解析：{(title or '')[:48]}（{len(resources)} 个格式）", "INFO")
            self._mark(token)
        except Exception as e:
            self._log(f"yt-dlp 解析失败：{e}", "ERROR")
            if token is None or token == self._sniff_token:
                self._status("yt-dlp 未识别此页面")
        finally:
            self._release_sniff(token)

    def stop_sniff(self):
        self._sniff_token += 1  # 让还在跑的嗅探线程回来后作废
        try:
            self.monitor.close()
        except Exception:
            pass
        self.busy_sniff = False
        self._status("已停止嗅探")

    def direct_resource(self, url):
        path = urlparse(url).path.lower()
        if ".m3u8" in path:
            rtype = "m3u8"
        elif path.endswith((".mpd",)):
            rtype = "dash"
        else:
            ext = os.path.splitext(path)[1].lstrip(".")
            rtype = ext if ext in ("mp4", "webm", "flv", "ts") else "mp4"
        name = os.path.basename(path) or "direct"
        return vd.VideoResource(url=url, resource_type=rtype, source="direct", title=name)

    def import_m3u8(self, path):
        content = Path(path).read_text("utf-8", errors="ignore")
        base = Path(path).resolve().as_uri()
        streams = vd.M3U8Parser(base).parse(content, base)
        if not streams:
            return 0
        added = 0
        name = os.path.basename(path)
        if getattr(streams[0], "is_master", False):
            for s in streams:
                self._ext.append(vd.VideoResource(url=s.url, resource_type="m3u8",
                                                  title=f"本地M3U8 ({name}) {s.resolution}",
                                                  resolution=s.resolution, bandwidth=s.bandwidth,
                                                  source="manual"))
                added += 1
        else:
            self._ext.append(vd.VideoResource(url=path, resource_type="m3u8",
                                              title=f"本地M3U8 ({name})", source="manual"))
            added = 1
        self._mark()
        return added

    def start_proxy(self):
        if self.proxy is not None:
            return True
        try:
            import asyncio
            from mitmproxy import options
            from mitmproxy.tools.dump import DumpMaster
        except Exception as e:
            self._log(f"缺少 mitmproxy：{e}", "ERROR")
            return False
        sniffer = self.sniffer
        log = self._log
        state = {"master": None, "loop": None}

        class Addon:
            def response(self, flow):
                try:
                    url = flow.request.pretty_url
                    ctype = flow.response.headers.get("content-type", "")
                    low = url.lower()
                    hit = any(e in low and (e + "?" in low or e + "#" in low or low.endswith(e))
                              for e in (".mp4", ".m3u8", ".webm", ".flv", ".m4s", ".mpd", ".ts"))
                    if not hit:
                        hit = any(m in ctype.lower() for m in ("video/", "mpegurl", "mp2t", "dash"))
                    if not hit:
                        return
                    rtype = sniffer.detect_resource_type(url, ctype)
                    res = vd.VideoResource(
                        url=url, resource_type=rtype,
                        headers={"User-Agent": flow.request.headers.get("user-agent", ""),
                                 "Referer": flow.request.headers.get("referer", "")},
                        size=int(flow.response.headers.get("content-length", 0) or 0),
                        source="proxy")
                    res.resolution = sniffer.extract_resolution(url)
                    if sniffer.add_resource(res):
                        log(f"[代理] 捕获：{rtype} {url[:80]}", "INFO")
                except Exception:
                    pass

        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            state["loop"] = loop
            opts = options.Options(listen_host="127.0.0.1", listen_port=8888)
            master = DumpMaster(opts, loop=loop, with_termlog=False)
            master.addons.add(Addon())
            state["master"] = master
            try:
                loop.run_until_complete(master.run())
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()
        for _ in range(30):
            time.sleep(0.1)
            if state["master"] is not None:
                break
        if state["master"] is None:
            self._log("代理抓包启动失败", "ERROR")
            return False
        self.proxy = state
        self._log("代理抓包已开启：把浏览器代理设为 127.0.0.1:8888", "INFO")
        self._log("HTTPS 站点需装 mitmproxy 证书：http://mitm.it", "WARNING")
        threading.Thread(target=self._proxy_watch, daemon=True).start()
        return True

    def _proxy_watch(self):
        while self.proxy is not None:
            time.sleep(2)
            if self.proxy is not None:
                self.events.put(("resources", None))

    def stop_proxy(self):
        p, self.proxy = self.proxy, None
        if p:
            try:
                p["loop"].call_soon_threadsafe(p["master"].shutdown)
            except Exception:
                pass
        self._log("代理抓包已停止", "INFO")

    def export_har(self, path):
        reqs = list(getattr(self.monitor, "captured_requests", []) or [])
        if not reqs:
            return 0
        entries = []
        for r in reqs:
            entries.append({
                "startedDateTime": r.get("timestamp", ""),
                "time": 0,
                "request": {
                    "method": r.get("method", "GET"), "url": r.get("url", ""),
                    "httpVersion": "HTTP/1.1",
                    "headers": [{"name": k, "value": str(v)} for k, v in (r.get("headers") or {}).items()],
                    "queryString": [], "cookies": [], "headersSize": -1, "bodySize": -1,
                },
                "response": {
                    "status": r.get("status", 0), "statusText": "", "httpVersion": "HTTP/1.1",
                    "headers": [{"name": k, "value": str(v)} for k, v in (r.get("response_headers") or {}).items()],
                    "content": {"size": r.get("size", 0), "mimeType": r.get("content_type", "")},
                    "redirectURL": "", "headersSize": -1, "bodySize": r.get("size", 0),
                },
                "cache": {}, "timings": {"send": 0, "wait": 0, "receive": 0},
            })
        data = {"log": {"version": "1.2",
                        "creator": {"name": "VideoDownloader", "version": "5.0"},
                        "entries": entries}}
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        return len(entries)

    def export_resources(self, path):
        data = []
        for r in self.resources():
            data.append({
                "url": r.url, "type": r.resource_type, "resolution": r.resolution,
                "size": r.size, "title": r.title, "page_url": r.page_url,
                "engine": r.engine, "format_spec": r.format_spec,
            })
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        self._log(f"已导出 {len(data)} 条资源到 {path}", "INFO")
        return len(data)

    def save_playlist(self, resource, path):
        import requests
        resp = requests.get(resource.url, headers=dict(resource.headers or {}), timeout=20)
        resp.raise_for_status()
        text = resp.text
        Path(path).write_text(text, "utf-8")
        return len(re.findall(r"^[^#\s][^\r\n]*$", text, re.MULTILINE))

    def bilibili_parts(self, url):
        """B 站多 P 展开。兼容 BV 号与老式 av 号（av 走 aid 参数，旧版只认 BV）。"""
        m = re.search(r"(BV[0-9A-Za-z]{10})", url)
        bvid = m.group(1) if m else ""
        aid = ""
        if not bvid:
            m2 = re.search(r"(?i)av(\d+)", url)
            if m2:
                aid = m2.group(1)
        if not bvid and not aid:
            return []
        params = {"bvid": bvid} if bvid else {"aid": aid}
        import requests
        try:
            resp = requests.get("https://api.bilibili.com/x/player/pagelist",
                                params=params,
                                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                                         "Referer": "https://www.bilibili.com/"},
                                timeout=12)
            data = resp.json()
            pages = data.get("data") or []
            if data.get("code") != 0 or len(pages) <= 1:
                return []
            base = "https://www.bilibili.com/video/" + (bvid or ("av" + aid))
            out = []
            for item in pages:
                p = int(item.get("page") or 0)
                if p > 0:
                    out.append((base + "?p=%d" % p, str(item.get("part") or "").strip()))
            return out
        except Exception:
            return []

    # 只有这些形态的地址才值得去调播放列表接口，避免给每个单视频地址多付一次解析开销
    PLAYLIST_HINTS = ("list=", "/playlist", "/sets/", "/medialist/", "/favlist",
                      "/collection", "space.bilibili.com", "/play/")

    def expand_playlist(self, url, limit=50):
        """把播放列表/合集/多 P 链接展开成逐条视频地址。

        后端 YTDLPEngine 有 extract_playlist + playlist_entries（旧 UI 用它做批量），
        v5 之前完全没接。返回 [(url, title)]；不是列表或展开失败返回 []，调用方按单条处理。
        """
        low = (url or "").lower()
        if not any(h in low for h in self.PLAYLIST_HINTS):
            return []
        try:
            eng = vd.YTDLPEngine(ffmpeg_location=resolve_ffmpeg(self.settings),
                                 cookiefile=str(self.settings.get("ytdlp_cookie_path", "") or ""),
                                 proxy=self._proxy(),
                                 log_callback=lambda m, l="INFO": None,
                                 progress_callback=lambda v, m="": None)
            info = eng.extract_playlist(url, playlistend=limit)
            entries = vd.YTDLPEngine.playlist_entries(info)
        except Exception as e:
            self._log("合集展开失败，按单条处理：%s" % e, "WARNING")
            return []
        out = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            u = e.get("webpage_url") or e.get("url") or ""
            if not str(u).startswith("http"):
                continue
            out.append((str(u), str(e.get("title") or "").strip()))
        out = out[:limit]
        if len(out) <= 1:
            return []
        self._log("合集展开为 %d 条" % len(out), "INFO")
        return out

    def _make_downloader(self):
        d = vd.UniversalDownloader(ffmpeg_path=resolve_ffmpeg(self.settings))
        d.log_callback = self._log
        d.progress_callback = lambda v, m="": self.events.put(("progress", (float(v), str(m))))
        try:
            d.retry_times = max(1, min(10, int(self.settings.get("max_retry", 3))))
        except Exception:
            d.retry_times = 3
        try:
            d.max_workers = max(1, min(32, int(self.settings.get("default_threads", 16))))
        except Exception:
            d.max_workers = 8
        try:
            limit = int(self.settings.get("speed_limit", 0))
            if limit > 0:
                d.speed_limit = limit * 1024
        except Exception:
            pass
        d.download_mode = self.settings.get("download_mode", "auto")
        # 直播录制：后端 UniversalDownloader 支持 live_mode（走 _download_live，
        # 对 m3u8/ts/flv 实时拉到手动停止为止），v5 之前没把它传下去。
        try:
            d.live_mode = bool(self.settings.get("live_mode", False))
        except Exception:
            pass
        if self.settings.get("proxy_enabled") and self.settings.get("proxy"):
            p = str(self.settings["proxy"])
            d.session.proxies = {"http": p, "https": p}
        return d

    def download(self, resource, path, audio_only=False, subtitles=False):
        if self.busy_dl:
            self._log("已有下载任务在进行", "WARNING")
            return False
        if resource is None:
            self._log("请先选择一个清晰度", "ERROR")
            return False
        self.busy_dl = True
        self.current = resource
        self.events.put(("progress", (0.0, "准备下载")))
        threading.Thread(target=self._dl_job, args=(resource, path, audio_only, subtitles), daemon=True).start()
        return True

    def _dl_job(self, resource, path, audio_only, subtitles):
        ok = False
        actual = path
        try:
            if getattr(resource, "engine", "") == "ytdlp":
                base = os.path.splitext(path)[0]
                self.ytdlp = vd.YTDLPEngine(ffmpeg_location=resolve_ffmpeg(self.settings),
                                            cookiefile=str(self.settings.get("ytdlp_cookie_path", "") or ""),
                                            proxy=self._proxy(),
                                            log_callback=self._log,
                                            progress_callback=lambda v, m="": self.events.put(("progress", (float(v), str(m)))))
                ok = self.ytdlp.download(resource.page_url or resource.url, base,
                                         resource.format_spec or "bv*+ba/b",
                                         audio_only=audio_only, subtitles=subtitles)
                if self.ytdlp._last_file and os.path.exists(self.ytdlp._last_file):
                    actual = self.ytdlp._last_file
            else:
                self.downloader = self._make_downloader()
                ok = self.downloader.extract_audio(resource, path) if audio_only else self.downloader.download(resource, path)
        except Exception as e:
            self._log(f"下载异常：{e}", "ERROR")
        finally:
            self.busy_dl = False
            if ok:
                if self.settings.get("save_history", True):
                    try:
                        add_history(resource.url, actual, "success", resource=resource)
                    except Exception as e:
                        self._log(f"写入历史失败：{e}", "WARNING")
                self._status("下载完成")
            else:
                # 失败也记一笔（旧版只在成功时写，历史页的“失败”样式永远用不上，
                # 事后也没法回看哪条挂了）。路径用打算保存的位置，方便去目录里确认。
                if self.settings.get("save_history", True):
                    try:
                        add_history(resource.url, actual, "failed", resource=resource)
                    except Exception as e:
                        self._log(f"写入历史失败：{e}", "WARNING")
                self._status("下载失败")
            self.events.put(("download_done", (bool(ok), actual)))

    def stop(self):
        for obj in (self.downloader, self.ytdlp):
            try:
                if obj is not None:
                    obj.stop()
            except Exception:
                pass
        self._status("正在停止…")

    def close(self):
        try:
            self._jobs.put(None)
        except Exception:
            pass
        self.stop_proxy()
        try:
            self.monitor.close()
        except Exception:
            pass
