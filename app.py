import html
import os
import queue
import re
import sys
import json
import threading
import time
import urllib.request
import webbrowser

# 高 DPI：在 Qt 初始化前把进程 DPI 上下文设为 PER_MONITOR_AWARE_V2（-4）。
# 后端 core 会加载 V4.5.py，那里用旧接口调用 SetProcessDpiAwareness(2)，
# 两者撞车时 Qt 会向 stderr 打印 "SetProcessDpiAwarenessContext() failed: 拒绝访问"，
# 并且界面的缩放会按旧的 per-monitor(v1) 走，高分屏上容易糊。
if sys.platform == "win32":
    try:
        import ctypes as _ct
        _ct.windll.user32.SetProcessDpiAwarenessContext(_ct.c_void_p(-4))
    except Exception:
        pass

from PyQt6.QtCore import Qt, QTimer, QRectF, QObject, QEvent
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QFont, QImage, QPixmap, QPen
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QLabel, QScrollArea,
    QGraphicsDropShadowEffect, QFileDialog, QGridLayout, QDialog, QSystemTrayIcon,
    QMenu, QCheckBox, QMessageBox, QSizePolicy, QStackedWidget, QFrame
)
from PyQt6.QtGui import QIcon, QAction
# qfluentwidgets 在 import 时会往 stdout 打印一条 Pro 广告（common/config.py 的 ALERT），
# 和本程序无关；这里在导入期间临时吞掉 stdout，免得混进控制台或导出的日志。
import contextlib as _ctxlib
import io as _io

with _ctxlib.redirect_stdout(_io.StringIO()):
    from qfluentwidgets import (
        FluentWindow, FluentIcon as FIF, NavigationItemPosition, PrimaryPushButton, PushButton,
        TransparentPushButton, LineEdit, ComboBox, SwitchButton, TitleLabel, SubtitleLabel,
        BodyLabel, CaptionLabel, StrongBodyLabel, IconWidget, InfoBar, InfoBarPosition,
        ProgressBar, setTheme, Theme, setThemeColor, SegmentedWidget,
        SmoothScrollArea
    )
import core

HERE = os.path.dirname(os.path.abspath(__file__))
GROUP_QQ = "1067019270"
GROUP_TG = "spxzqdwsbhq"
SPONSOR_DIR = r"F:\MenWenDong\视频下载器"

# ---------------- 更新检查 ----------------
# version.json 放在 EdgeOne Pages 静态站根目录；地址硬编码在此，用户不能改。
# 注意：客户端要的是根目录的 version.json（JSON），不是 /updata 那个管理页（HTML）。
APP_VERSION = "5.0"
UPDATE_URL = "https://spxzq.fun/version.json"


def _vtuple(s):
    """把 "5.0.1" / "5.1" 这类版本号变成可比较的元组，位数不同也不会崩。"""
    parts = re.findall(r"\d+", str(s))
    return tuple(int(p) for p in parts) or (0,)


def fetch_update(url, timeout=8):
    """拉取 version.json，返回 (ok, dict 或 错误说明)。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "vd5-update-check"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(200000).decode("utf-8", "replace")
        data = json.loads(raw)
        if not isinstance(data, dict) or not str(data.get("version") or "").strip():
            return False, "version.json 缺少 version 字段"
        return True, data
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)

STATE = {"radius": 8, "backdrop": "无", "theme": "浅色"}
CARDS = []
WALL = {}

LIGHT = {
    "base": "#F6F5F0",
    "blobs": [("#EDEEE4", 235), ("#EAF4D2", 245), ("#E8E5D9", 230), ("#DDD3D5", 220), ("#F1F2E4", 200)],
    "card": 255, "alpha": 226, "border": 30,
}
DARK = {
    "base": "#1B1B1D",
    "blobs": [("#242A24", 210), ("#1E2A23", 215), ("#2A2622", 205), ("#2A2227", 200), ("#202A28", 180)],
    "card": 48, "alpha": 226, "border": 46,
}
LOG_COLORS = {"DEBUG": "#8A8A8A", "INFO": "#5B5B5B", "WARNING": "#A65B00", "ERROR": "#C42B1C", "OK": "#1E7A34"}


def gray_icon(ficon, size, color):
    """把 Fluent 图标重绘成单一灰色（空态用），避免纯黑图钉在白卡上太扎眼。"""
    pm = ficon.icon().pixmap(size, size)
    out = QPixmap(pm.size())
    out.fill(Qt.GlobalColor.transparent)
    pnt = QPainter(out)
    pnt.drawPixmap(0, 0, pm)
    pnt.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    pnt.fillRect(out.rect(), QColor(color))
    pnt.end()
    return out


def fluent_scroll(parent=None):
    """细条滚动区：Fluent 自带滚动条（无箭头、悬停才出轨道），替代系统那条粗的。"""
    s = SmoothScrollArea(parent) if parent is not None else SmoothScrollArea()
    s.setWidgetResizable(True)
    s.setFrameShape(QScrollArea.Shape.NoFrame)
    s.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    s.setStyleSheet("QScrollArea{background:transparent;} QScrollArea>QWidget>QWidget{background:transparent;}")
    return s


def pal():
    return DARK if STATE["theme"] == "深色" else LIGHT


def wallpaper(w, h):
    key = (w, h, STATE["theme"])
    if key in WALL:
        return WALL[key]
    if len(WALL) > 4:
        # 每个尺寸一张位图（1080x740 约 3MB），拖动窗口会不断产生新尺寸，
        # 不设上限内存会一直涨，这里只保留最近几个尺寸。
        WALL.clear()
    from PIL import Image, ImageDraw, ImageFilter
    dark = STATE["theme"] == "深色"
    base = (27, 27, 29) if dark else (248, 247, 242)
    im = Image.new("RGB", (w, h), base)
    ov = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    if dark:
        blobs = [(0.10, 0.10, 0.60, "#2B3129"), (0.95, 0.05, 0.55, "#21302A"),
                 (0.90, 0.95, 0.55, "#302A24"), (0.10, 0.95, 0.58, "#2E2429")]
        alpha = 200
    else:
        blobs = [(0.10, 0.10, 0.58, "#EDEEE4"), (0.94, 0.04, 0.52, "#E6F3C6"),
                 (0.92, 0.96, 0.50, "#E9E6DA"), (0.08, 0.96, 0.54, "#EBDCE0")]
        alpha = 232
    for cx, cy, r, col in blobs:
        c = QColor(col)
        d.ellipse([(cx - r) * w, (cy - r) * h, (cx + r) * w, (cy + r) * h],
                  fill=(c.red(), c.green(), c.blue(), alpha))
    ov = ov.filter(ImageFilter.GaussianBlur(max(40, int(min(w, h) * 0.14))))
    im = Image.alpha_composite(im.convert("RGBA"), ov).convert("RGBA")
    data = im.tobytes("raw", "BGRA")
    qi = QImage(data, w, h, QImage.Format.Format_ARGB32)
    pix = QPixmap.fromImage(qi)
    WALL[key] = pix
    return pix


class Card(QWidget):
    def __init__(self, parent=None, radius=None, pad=16):
        super().__init__(parent)
        self._radius = radius
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        CARDS.append(self)
        sh = QGraphicsDropShadowEffect(self)
        sh.setBlurRadius(34)
        sh.setOffset(0, 3)
        sh.setColor(QColor(0, 0, 0, 40))
        self.setGraphicsEffect(sh)
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(pad, pad, pad, pad)
        self.box.setSpacing(10)

    def radius(self):
        return STATE["radius"] if self._radius is None else self._radius

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = pal()
        r = self.radius()
        path = QPainterPath()
        path.addRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), r, r)
        p.setPen(QPen(QColor(c["card"], c["card"], c["card"], c["border"]), 1))
        p.setBrush(QColor(c["card"], c["card"], c["card"], c["alpha"]))
        p.drawPath(path)
        p.end()


def repaint_cards():
    alive = []
    for c in CARDS:
        try:
            c.update()
        except RuntimeError:
            # 对话框关掉后，它里面的 Card 已经被销毁，CARDS 里留下的是失效包装，
            # 对它们调 update() 会抛 RuntimeError（"wrapped C/C++ object deleted"），
            # 顺手把死引用清出列表，否则列表只会一直变长。
            continue
        alive.append(c)
    CARDS[:] = alive


class Page(QWidget):
    def resizeEvent(self, e):
        # 卡片填充是半透明的（alpha 226），旧字迹没被整页重绘覆盖就会透出来变成重影
        super().resizeEvent(e)
        self.update()

    def showEvent(self, e):
        super().showEvent(e)
        self.update()

    def paintEvent(self, e):
        if STATE["backdrop"] != "无":
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.drawPixmap(self.rect(), wallpaper(max(1, self.width()), max(1, self.height())))
        p.end()


def clear_layout(lay):
    """清空布局，包括用 addLayout() 嵌套进来的子布局。

    旧写法只看 item.widget()；历史记录的行是 addLayout() 加进来的，
    取出来是 None 就被跳过，行里的控件仍留在界面上继续绘制，
    于是新行叠在旧行上（历史页前三行文字/按钮打叠就是这个原因）。
    """
    while lay.count():
        item = lay.takeAt(0)
        w = item.widget()
        if w is not None:
            w.hide()
            w.setParent(None)
            w.deleteLater()
            continue
        sub = item.layout()
        if sub is not None:
            clear_layout(sub)
            sub.deleteLater()


class Elider(QObject):
    """给标签挂一个 resize 监听，宽度变化时按像素做省略号截断。

    不用子类化 BodyLabel：qfluentwidgets 的 Label.__init__ 是重载分发的，
    子类再调用 super().__init__(text, parent) 会绕回自己的 __init__ 递归爆栈。
    原来是 name[:26] 硬截断，窗口一窄长标题就顶到右边的状态和按钮上。
    """
    def __init__(self, label):
        super().__init__(label)
        self.label = label
        self.full = ""
        label.installEventFilter(self)

    def set_text(self, text):
        self.full = text or ""
        self.label.setToolTip(self.full)
        self.apply()

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.Resize:
            self.apply()
        return False

    def apply(self):
        lb = self.label
        w = lb.width() - 2
        if w < 40:
            # 还没被布局安排宽度时先用字数截断，保证首次绘制的高度正常
            s = self.full[:26] + ("…" if len(self.full) > 26 else "")
        else:
            s = lb.fontMetrics().elidedText(self.full, Qt.TextElideMode.ElideRight, w)
        if s != lb.text():
            lb.setText(s)


def free_path(path):
    """同名文件已存在时，依次试「名字 (2).ext」「名字 (3).ext」…，避免覆盖原文件。"""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    for i in range(2, 100):
        cand = "%s (%d)%s" % (base, i, ext)
        if not os.path.exists(cand):
            return cand
    return path


def safe_name(text, fallback="video"):
    text = (text or "").strip() or fallback
    text = re.sub(r'[\\/:*?"<>|\r\n]', "_", text)
    return text[:100]


class LinkCard(Card):
    def __init__(self, title, desc, qr_path, value, link=""):
        super().__init__(pad=14)
        h = QHBoxLayout()
        h.setSpacing(16)
        img = QLabel()
        img.setFixedSize(132, 132)
        img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pix = QPixmap(qr_path) if qr_path and os.path.isfile(qr_path) else QPixmap()
        if not pix.isNull():
            img.setPixmap(pix.scaled(132, 132, Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation))
            img.setCursor(Qt.CursorShape.PointingHandCursor)
            img.setToolTip("点一下看大图，方便扫码")
            img.mousePressEvent = lambda e, p=qr_path: os.startfile(p)
        else:
            img.setText("暂无二维码")
            img.setStyleSheet("color:#9A9A9A;border:1px dashed #D6D6D6;border-radius:8px;")
        col = QVBoxLayout()
        col.setSpacing(4)
        col.addWidget(StrongBodyLabel(title))
        d = CaptionLabel(desc)
        d.setTextColor("#707070", "#A0A0A0")
        col.addWidget(d)
        v = BodyLabel(value)
        v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        col.addWidget(v)
        row = QHBoxLayout()
        row.setSpacing(8)
        if value and not value.startswith("暂未"):
            b_copy = PushButton(FIF.COPY, "复制")
            b_copy.clicked.connect(lambda _=False, t=value: self.copy(t))
            row.addWidget(b_copy)
        if link:
            b_open = PushButton(FIF.GLOBE, "打开链接")
            b_open.clicked.connect(lambda _=False, u=link: self.open(u))
            row.addWidget(b_open)
        if qr_path and os.path.isfile(qr_path):
            b_view = PushButton(FIF.ZOOM, "查看大图")
            b_view.clicked.connect(lambda _=False, p=qr_path: os.startfile(p))
            row.addWidget(b_view)
        row.addStretch(1)
        col.addLayout(row)
        col.addStretch(1)
        h.addWidget(img, 0, Qt.AlignmentFlag.AlignTop)
        h.addLayout(col, 1)
        self.box.addLayout(h)

    def copy(self, text):
        QApplication.clipboard().setText(text)
        InfoBar.success("已复制", text, parent=self.window(), position=InfoBarPosition.TOP_RIGHT, duration=1800)

    def open(self, url):
        os.startfile(url)


class GroupDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("加入群聊")
        self.setFixedSize(640, 620)
        self.setStyleSheet("QDialog{background:#F7F7F5;}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 18)
        lay.setSpacing(12)
        lay.addWidget(TitleLabel("加入群聊"))
        tip = CaptionLabel("遇到问题、想提需求，或者想看更新，都在群里")
        tip.setTextColor("#707070", "#A0A0A0")
        lay.addWidget(tip)
        lay.addWidget(LinkCard("QQ 群", "手机 QQ 扫码，或搜索群号加入",
                              os.path.join(HERE, "QQ.jpg"), GROUP_QQ))
        lay.addWidget(LinkCard("Telegram 频道", "手机 Telegram 扫码，或点链接加入",
                              os.path.join(HERE, "tg.jpg"), "@" + GROUP_TG,
                              "https://t.me/" + GROUP_TG))
        lay.addWidget(LinkCard("GitHub", "仓库地址待补充，先看群里的发布页", "", "暂未提供"))
        lay.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        b = PushButton("关闭")
        b.clicked.connect(self.close)
        row.addWidget(b)
        lay.addLayout(row)


class SponsorDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("赞助")
        self.setFixedSize(620, 500)
        self.setStyleSheet("QDialog{background:#F7F7F5;}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 18)
        lay.setSpacing(12)
        lay.addWidget(TitleLabel("赞助"))
        t = CaptionLabel("软件完全免费，赞助随意，不影响任何功能")
        t.setTextColor("#707070", "#A0A0A0")
        lay.addWidget(t)
        h = QHBoxLayout()
        h.setSpacing(14)
        for name, file in (("微信", "wx.jpg"), ("支付宝", "zfb.jpg")):
            card = Card(pad=10)
            img = QLabel()
            img.setFixedSize(235, 330)
            img.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pix = QPixmap(os.path.join(SPONSOR_DIR, file))
            if not pix.isNull():
                img.setPixmap(pix.scaled(235, 330, Qt.AspectRatioMode.KeepAspectRatio,
                                         Qt.TransformationMode.SmoothTransformation))
                img.setCursor(Qt.CursorShape.PointingHandCursor)
                img.setToolTip("点一下看大图")
                img.mousePressEvent = lambda e, p=os.path.join(SPONSOR_DIR, file): os.startfile(p)
            else:
                img.setText("图片缺失")
            card.box.addWidget(img, 0, Qt.AlignmentFlag.AlignHCenter)
            h.addWidget(card)
        lay.addLayout(h)
        hint = CaptionLabel("如果你觉得这个作品帮助到你了，欢迎扫码请作者喝杯咖啡")
        hint.setTextColor("#707070", "#A0A0A0")
        lay.addWidget(hint)


class HomePage(Page):
    def __init__(self, win):
        super().__init__(win)
        self.setObjectName("homePage")
        self.win = win
        self.rows = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 14, 26, 20)
        lay.setSpacing(14)

        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(TitleLabel("视频下载器"))
        v = CaptionLabel("v5.0")
        v.setTextColor("#6B6B6B", "#B7B7B7")
        head.addSpacing(8)
        head.addWidget(v)
        head.addStretch(1)
        self.status = CaptionLabel("就绪")
        self.status.setTextColor("#6B6B6B", "#B7B7B7")
        head.addWidget(self.status)
        b_group = TransparentPushButton(FIF.PEOPLE, "加入群聊")
        b_group.clicked.connect(lambda: GroupDialog(win).exec())
        head.addWidget(b_group)
        b_help = TransparentPushButton(FIF.QUESTION, "使用说明")
        b_help.clicked.connect(lambda: HelpDialog(win).exec())
        head.addWidget(b_help)
        lay.addLayout(head)

        c1 = Card()
        row = QHBoxLayout()
        row.setSpacing(10)
        self.url = LineEdit()
        self.url.setPlaceholderText("粘贴网页或视频地址，例如 https://v.douyin.com/…")
        self.url.setClearButtonEnabled(True)
        self.url.setFixedHeight(36)
        b_paste = PushButton(FIF.PASTE, "粘贴")
        b_paste.setFixedHeight(36)
        b_paste.clicked.connect(self.paste)
        self.btn = PrimaryPushButton(FIF.SEARCH, "开始嗅探")
        self.btn.setFixedHeight(36)
        self.btn.clicked.connect(self.toggle)
        row.addWidget(self.url, 1)
        row.addWidget(b_paste)
        row.addWidget(self.btn)
        c1.box.addLayout(row)

        row2 = QHBoxLayout()
        row2.setSpacing(10)
        b_direct = PushButton(FIF.LINK, "直链下载")
        b_direct.clicked.connect(self.direct)
        b_m3u8 = PushButton(FIF.DOCUMENT, "导入 M3U8")
        b_m3u8.clicked.connect(self.import_m3u8)
        self.b_proxy = PushButton(FIF.WIFI, "代理抓包")
        self.b_proxy.clicked.connect(self.toggle_proxy)
        self.b_export = b_export = PushButton(FIF.SAVE, "导出资源")
        self.b_export.setEnabled(False)
        b_export.clicked.connect(self.export_res)
        b_har = PushButton(FIF.CODE, "导出 HAR")
        b_har.clicked.connect(self.export_har)
        row2.addWidget(b_direct)
        row2.addWidget(b_m3u8)
        row2.addWidget(self.b_proxy)
        row2.addWidget(b_export)
        row2.addWidget(b_har)
        row2.addStretch(1)
        c1.box.addLayout(row2)
        lay.addWidget(c1)

        c2 = Card()
        h2 = QHBoxLayout()
        h2.setSpacing(8)
        h2.addWidget(SubtitleLabel("嗅探结果"))
        self.count = CaptionLabel("暂无资源")
        self.count.setTextColor("#6B6B6B", "#B7B7B7")
        h2.addWidget(self.count)
        h2.addStretch(1)
        self.b_save = b_save = PushButton(FIF.SAVE, "下载最佳")
        self.b_save.setEnabled(False)
        b_save.clicked.connect(self.save_best)
        h2.addWidget(b_save)
        c2.box.addLayout(h2)
        self.res_box = QVBoxLayout()
        self.res_box.setSpacing(6)
        c2.box.addLayout(self.res_box)
        self.res_box.addWidget(self._res_empty())
        lay.addWidget(c2)

        c2b = Card(pad=10)
        prow = QHBoxLayout()
        prow.setSpacing(8)
        self.prog = ProgressBar()
        self.prog.setValue(0)
        self.prog.setFixedHeight(6)
        prow.addWidget(self.prog, 1, Qt.AlignmentFlag.AlignVCenter)
        self.prog_text = CaptionLabel("空闲")
        self.prog_text.setTextColor("#8A8A8A", "#9A9A9A")
        prow.addWidget(self.prog_text, 0, Qt.AlignmentFlag.AlignVCenter)
        self.stop_btn = TransparentPushButton(FIF.CANCEL, "停止")
        self.stop_btn.clicked.connect(self.stop)
        prow.addWidget(self.stop_btn)
        c2b.box.addLayout(prow)
        res_group = QWidget()
        gv = QVBoxLayout(res_group)
        gv.setContentsMargins(0, 0, 0, 0)
        gv.setSpacing(10)
        gv.addWidget(c2)
        gv.addWidget(c2b)
        lay.addWidget(res_group)

        c3 = Card()
        h3 = QHBoxLayout()
        h3.addWidget(SubtitleLabel("运行日志"))
        h3.addStretch(1)
        b_fold = TransparentPushButton("收起日志")
        self.logbox = QTextEdit()
        self.logbox.setReadOnly(True)
        self.logbox.setFont(QFont("Consolas", 10))
        self.logbox.setStyleSheet("QTextEdit{background:transparent;border:none;}")
        self.log_shown = True

        def toggle_log():
            self.log_shown = not self.log_shown
            self.logbox.setVisible(self.log_shown)
            b_fold.setText("收起日志" if self.log_shown else "展开日志")
            if self.log_shown:
                c3.setMaximumHeight(16777215)
                lay.setStretchFactor(c3, 1)
            else:
                c3.setMaximumHeight(72)
                lay.setStretchFactor(c3, 0)

        b_fold.clicked.connect(toggle_log)
        b_log = TransparentPushButton(FIF.SAVE, "导出日志")
        b_log.clicked.connect(self.export_log)
        clr = TransparentPushButton("清空")
        clr.clicked.connect(lambda: self.logbox.clear())
        h3.addWidget(b_fold)
        h3.addWidget(b_log)
        h3.addWidget(clr)
        c3.box.addLayout(h3)
        c3.box.addWidget(self.logbox)
        lay.addWidget(c3, 1)

        self.append_log("INFO", "就绪：粘贴网页或视频地址，点击「开始嗅探」")

    def paste(self):
        text = QApplication.clipboard().text().strip()
        if text:
            self.url.setText(text)
            self.append_log("INFO", "已从剪贴板粘贴地址")

    def toggle(self):
        eng = self.win.engine
        if eng.busy_sniff:
            eng.stop_sniff()
            return
        url = self.url.text().strip()
        if not url:
            self.append_log("ERROR", "请输入地址")
            return
        if "douyin.com" in url.lower() and not str(eng.settings.get("ytdlp_cookie_path", "") or "").strip():
            self.append_log("WARNING", "抖音未指定 cookies.txt，将走浏览器嗅探，清晰度可能不全（设置页可指定）")
        self.btn.setText("停止嗅探")
        self.btn.setIcon(FIF.CANCEL)
        self.prog.setValue(0)
        self.prog_text.setText("正在解析…")
        eng.sniff(url)

    def stop(self):
        self.win.engine.stop()

    def set_status(self, text):
        self.status.setText(text)
        if text.startswith(("解析完成", "未找到", "已停止", "yt-dlp 未识别")):
            self.prog_text.setText(text)
            self.btn.setText("开始嗅探")
            self.btn.setIcon(FIF.SEARCH)

    def _res_empty(self, main="还没有嗅探到资源", tip="粘贴网页或视频地址，点「开始嗅探」"):
        """嗅探结果的空态：图标 + 主文案 + 副说明，整块居中。"""
        w = QWidget()
        w.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 6, 0, 12)
        v.setSpacing(6)
        ic = QLabel()
        ic.setPixmap(gray_icon(FIF.SEARCH, 34, "#AFAFAF"))
        ic.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        v.addWidget(ic)
        t = StrongBodyLabel(main)
        t.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        v.addWidget(t)
        d = CaptionLabel(tip)
        d.setTextColor("#8A8A8A", "#9A9A9A")
        d.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        v.addWidget(d)
        return w

    def rebuild(self, resources):
        clear_layout(self.res_box)
        self.rows = resources or []
        if not resources:
            self.res_box.addWidget(self._res_empty("没有捕获到资源", "换个地址重试，或稍等几秒再嗅探"))
            self.count.setText("暂无资源")
            self.count.setTextColor("#6B6B6B", "#B7B7B7")
            self.b_save.setEnabled(False)
            self.b_export.setEnabled(False)
            return
        self.count.setText("%d 个资源" % len(resources))
        self.count.setTextColor("#0F6CBD", "#4CC2FF")
        self.b_save.setEnabled(True)
        self.b_export.setEnabled(True)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(4)
        for col, name in enumerate(("清晰度", "格式", "大小", "说明")):
            h = CaptionLabel(name)
            h.setTextColor("#8A8A8A", "#9A9A9A")
            grid.addWidget(h, 0, col)
        grid.setColumnStretch(3, 1)
        for i, res in enumerate(resources[:12], start=1):
            q = res.resolution or res.display_format or res.resource_type.upper()
            fmt = res.ext.upper() if res.ext else (res.display_format or res.resource_type.upper())
            if res.codecs:
                fmt = "%s %s" % (fmt, res.codecs.split(".")[0])
            size = core.human_size(res.size)
            note = res.note or ("%d kbps" % (res.bandwidth // 1000) if res.bandwidth else "")
            if getattr(res, "is_drm", False):
                # 后端嗅探到 EME 加密会给资源打 is_drm，旧版界面据此提示，
                # v5 之前完全不读，用户选中后只会拿到一个笼统的失败。
                note = "⚠ DRM 受保护，无法下载"
            b = TransparentPushButton(FIF.SAVE, "下载")
            b.clicked.connect(lambda _=False, r=res: self.save_one(r))
            b_copy = TransparentPushButton(FIF.COPY, "复制")
            b_copy.clicked.connect(lambda _=False, u=res.url: self.copy_url(u))
            grid.addWidget(StrongBodyLabel(q), i, 0)
            grid.addWidget(BodyLabel(fmt), i, 1)
            grid.addWidget(BodyLabel(size), i, 2)
            nl = CaptionLabel(note[:60])
            nl.setTextColor("#8A8A8A", "#9A9A9A")
            grid.addWidget(nl, i, 3)
            grid.addWidget(b, i, 4)
            grid.addWidget(b_copy, i, 5)
            if res.resource_type == "m3u8":
                b_list = TransparentPushButton(FIF.DOCUMENT, "清单")
                b_list.clicked.connect(lambda _=False, r=res: self.save_playlist(r))
                grid.addWidget(b_list, i, 6)
        holder = QWidget()
        holder.setLayout(grid)
        holder.setMinimumWidth(560)
        scroll = fluent_scroll()
        scroll.setMaximumHeight(300)
        scroll.setWidget(holder)
        self.res_box.addWidget(scroll)

    def copy_url(self, url):
        QApplication.clipboard().setText(url)
        InfoBar.success("已复制链接", url[:70], parent=self.win, position=InfoBarPosition.TOP_RIGHT, duration=1800)

    def save_playlist(self, res):
        path, _ = QFileDialog.getSaveFileName(self, "导出 M3U8 清单", "playlist.m3u8",
                                             "M3U8 文件 (*.m3u8);;所有文件 (*)")
        if not path:
            return
        try:
            n = self.win.engine.save_playlist(res, path)
            InfoBar.success("已导出清单", "共 %d 条分片引用" % n, parent=self.win,
                            position=InfoBarPosition.TOP_RIGHT, duration=2500)
        except Exception as ex:
            self.append_log("ERROR", "导出清单失败：%s" % ex)

    def _target(self, res):
        title = res.title or res.page_title or self.win.last_title or "video"
        ext = res.ext or (".mp4" if res.resource_type != "m3u8" else ".mp4")
        if not ext.startswith("."):
            ext = "." + ext
        default_dir = self.win.engine.settings.get("batch_save_dir") or os.path.join(os.path.expanduser("~"), "Desktop")
        return os.path.join(default_dir, safe_name(title) + ext)

    def save_one(self, res):
        if getattr(res, "is_drm", False):
            InfoBar.error("无法下载", "该资源受 DRM 加密保护（EME），本程序不能解密",
                          parent=self.win, position=InfoBarPosition.TOP_RIGHT, duration=4000)
            self.append_log("ERROR", "资源受 DRM 保护，已跳过：%s" % (getattr(res, "url", "") or "")[:60])
            return
        path, _ = QFileDialog.getSaveFileName(self, "保存视频", self._target(res), "视频文件 (*.mp4 *.mkv *.webm *.ts *.mp3);;所有文件 (*)")
        if not path:
            return
        self.prog.setValue(0)
        self._prog_floor = 0
        self.append_log("INFO", "开始下载：%s" % os.path.basename(path))
        self.win.engine.download(res, path,
                                 audio_only=bool(self.win.engine.settings.get("audio_only")),
                                 subtitles=bool(self.win.engine.settings.get("download_subtitles")))

    def save_best(self):
        res = self.win.best_resource()
        if res is None:
            self.append_log("ERROR", "没有可下载的资源")
            return
        self.save_one(res)

    def direct(self):
        url = self.url.text().strip()
        if not url.lower().startswith(("http://", "https://")):
            self.append_log("ERROR", "直链下载需要先粘贴一个以 http(s) 开头的视频地址")
            return
        res = self.win.engine.direct_resource(url)
        path, _ = QFileDialog.getSaveFileName(self, "保存视频", self._target(res),
                                             "视频文件 (*.mp4 *.ts *.mkv *.mp3);;所有文件 (*)")
        if not path:
            return
        self.prog.setValue(0)
        self._prog_floor = 0
        self.append_log("INFO", "直链下载：%s" % url[:90])
        self.win.engine.download(res, path)

    def import_m3u8(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择本地 M3U8 文件", "",
                                             "M3U8 文件 (*.m3u8);;所有文件 (*)")
        if not path:
            return
        try:
            n = self.win.engine.import_m3u8(path)
            if n:
                self.append_log("INFO", "已导入 %d 个流：%s" % (n, os.path.basename(path)))
            else:
                self.append_log("WARNING", "文件里没有可用的流")
        except Exception as ex:
            self.append_log("ERROR", "导入失败：%s" % ex)

    def toggle_proxy(self):
        eng = self.win.engine
        if eng.proxy is not None:
            eng.stop_proxy()
            self.b_proxy.setText("代理抓包")
            return
        if eng.start_proxy():
            self.b_proxy.setText("停止抓包")
        else:
            InfoBar.warning("代理抓包不可用", "需要 mitmproxy 与管理员权限", parent=self.win,
                            position=InfoBarPosition.TOP_RIGHT, duration=3000)

    def export_res(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出资源列表", "resources.json",
                                             "JSON (*.json);;所有文件 (*)")
        if path:
            try:
                self.win.engine.export_resources(path)
            except Exception as ex:
                self.append_log("ERROR", "导出资源失败：%s" % ex)
                InfoBar.error("导出失败", str(ex), parent=self.win,
                              position=InfoBarPosition.TOP_RIGHT, duration=3000)
                return
            InfoBar.success("已导出", path, parent=self.win, position=InfoBarPosition.TOP_RIGHT, duration=2500)

    def export_har(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出 HAR 抓包数据", "sniff.har",
                                             "HAR 文件 (*.har);;JSON (*.json);;所有文件 (*)")
        if not path:
            return
        n = self.win.engine.export_har(path)
        if n:
            InfoBar.success("已导出 %d 条请求" % n, path, parent=self.win,
                            position=InfoBarPosition.TOP_RIGHT, duration=2500)
        else:
            InfoBar.warning("没有抓包数据", "先执行一次浏览器嗅探再导出", parent=self.win,
                            position=InfoBarPosition.TOP_RIGHT, duration=2500)

    def export_log(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出日志", "video_downloader_ui.log",
                                             "日志 (*.log *.txt);;所有文件 (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.logbox.toPlainText())
        except Exception as ex:
            InfoBar.error("日志导出失败", str(ex), parent=self.win,
                          position=InfoBarPosition.TOP_RIGHT, duration=3000)
            return
        InfoBar.success("日志已导出", path, parent=self.win, position=InfoBarPosition.TOP_RIGHT, duration=2500)

    def set_progress(self, value, text=""):
        value = max(getattr(self, "_prog_floor", 0), float(value))
        self._prog_floor = value
        self.prog.setValue(int(max(0, min(100, value))))
        self.prog_text.setText((text or "")[:60] or "下载中")

    def on_done(self, ok, path):
        self.prog.setValue(100 if ok else 0)
        self.prog_text.setText("完成" if ok else "失败")
        if ok:
            InfoBar.success("下载完成", os.path.basename(path), parent=self.win, position=InfoBarPosition.TOP_RIGHT, duration=3000)
            self.win.history.refresh()
            if self.win.engine.settings.get("auto_open_folder"):
                try:
                    os.startfile(os.path.dirname(path))
                except Exception:
                    pass
        else:
            InfoBar.error("下载失败", "详见日志", parent=self.win, position=InfoBarPosition.TOP_RIGHT, duration=3000)

    def append_log(self, level, text):
        text = str(text)
        if text == getattr(self, "_last_log", None):
            return
        self._last_log = text
        color = LOG_COLORS.get(level.upper(), "#5B5B5B")
        ts = time.strftime("%H:%M:%S")
        self.logbox.append('<span style="color:#9A9A9A">[%s]</span> <span style="color:%s">%s</span>'
                           % (ts, color, html.escape(str(text), quote=False)))
        doc = self.logbox.document()
        if doc.blockCount() > 200:
            cursor = self.logbox.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            cursor.select(cursor.SelectionType.BlockUnderCursor)
            cursor.removeSelectedText()
            cursor.deleteChar()


class BoxedArea(QFrame):
    """给多行输入套一层圆角细边框。Fluent 的 TextEdit 自带一条底部强调线，
    放在整块输入框上看着像边框没画完，所以这里改用原生 QTextEdit + 自绘外框。"""

    BORDER = "#E2E2E2"
    BORDER_FOCUS = "#0F6CBD"
    FILL = "#FBFBFB"

    def __init__(self, edit, height=None, pad=(12, 8, 8, 8), radius=8):
        super().__init__()
        self._radius = radius
        self.setObjectName("boxedArea")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        if height:
            self.setFixedHeight(height)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(*pad)
        lay.setSpacing(0)
        lay.addWidget(edit)
        edit.installEventFilter(self)
        self._apply(self.BORDER)

    def _apply(self, border):
        self.setStyleSheet(
            "QFrame#boxedArea{background:%s;border:1px solid %s;border-radius:%dpx;}"
            % (self.FILL, border, self._radius)
        )

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t == QEvent.Type.FocusIn:
            self._apply(self.BORDER_FOCUS)
        elif t == QEvent.Type.FocusOut:
            self._apply(self.BORDER)
        return super().eventFilter(obj, ev)


class BatchPage(Page):
    def __init__(self, win):
        super().__init__(win)
        self.setObjectName("batchPage")
        self.win = win
        self.events = queue.Queue()
        self.engine = None
        self.thread = None
        self.running = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 14, 26, 20)
        lay.setSpacing(14)
        lay.addWidget(TitleLabel("批量下载"))

        c = Card()
        c.box.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(6)
        head.setContentsMargins(2, 0, 0, 0)
        ic = QLabel()
        ic.setPixmap(gray_icon(FIF.LINK, 15, "#5C6BC0"))
        ic.setFixedSize(15, 15)
        head.addWidget(ic, 0, Qt.AlignmentFlag.AlignVCenter)
        lab = CaptionLabel("合集 / 播放列表 / 视频地址（每行一个）")
        lab.setTextColor("#5C6BC0", "#8A96D8")
        lab.setStyleSheet("CaptionLabel{font-size:13px;}")
        head.addWidget(lab, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addStretch(1)
        c.box.addLayout(head)
        self.urls = QTextEdit()
        self.urls.setPlaceholderText("https://…\n每行粘贴一个地址，B 站多 P 会自动展开")
        self.urls.setFrameShape(QFrame.Shape.NoFrame)
        self.urls.setStyleSheet(
            "QTextEdit{background:transparent;border:none;padding:0px;}"
            "QScrollBar:vertical{background:transparent;width:8px;margin:0px;}"
            "QScrollBar::handle:vertical{background:rgba(0,0,0,55);border-radius:4px;min-height:22px;}"
            "QScrollBar::handle:vertical:hover{background:rgba(0,0,0,90);}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0px;background:none;}"
            "QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:none;}"
        )
        self.url_box = BoxedArea(self.urls, height=132)
        c.box.addWidget(self.url_box)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.dir = LineEdit()
        self.dir.setPlaceholderText("保存目录（默认桌面）")
        self.dir.setText(str(core.load_settings().get("batch_save_dir", "") or ""))
        self.dir.setToolTip(self.dir.text())
        self.dir.setFixedHeight(34)
        b_dir = PushButton(FIF.FOLDER, "选择目录")
        b_dir.setFixedHeight(34)
        b_dir.clicked.connect(self.choose_dir)
        self.qual = ComboBox()
        self.qual.addItems(["最佳画质", "1080P", "720P", "480P", "360P", "仅音频"])
        self.qual.setCurrentText(str(core.load_settings().get("batch_quality", "最佳画质")))
        self.qual.setFixedWidth(118)
        self.qual.setFixedHeight(34)
        self.btn = PrimaryPushButton(FIF.PLAY, "开始批量")
        self.btn.setFixedHeight(34)
        self.btn.clicked.connect(self.start)
        row.addWidget(self.dir, 1)
        row.addWidget(self.qual)
        row.addWidget(b_dir)
        row.addWidget(self.btn)
        c.box.addLayout(row)
        lay.addWidget(c)

        c2 = Card(pad=10)
        c2.box.addWidget(SubtitleLabel("任务队列"))
        scroll = fluent_scroll()
        holder = QWidget()
        holder.setStyleSheet("background:transparent;")
        self.rows_box = QVBoxLayout(holder)
        self.rows_box.setContentsMargins(8, 4, 8, 8)
        self.rows_box.setSpacing(6)
        scroll.setWidget(holder)
        c2.box.addWidget(scroll)
        self.hint = CaptionLabel("暂无任务\n粘贴地址后点「开始批量」，B 站多 P 会自动展开成多行")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.hint.setTextColor("#8A8A8A", "#9A9A9A")
        self.rows_box.addStretch(1)
        self.rows_box.addWidget(self.hint, 0, Qt.AlignmentFlag.AlignHCenter)
        self.rows_box.addStretch(1)
        lay.addWidget(c2, 1)

    def choose_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择保存目录", self.dir.text() or os.path.expanduser("~"))
        if d:
            self.dir.setText(d)

    def add_row(self, name):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 2, 0, 2)
        t = BodyLabel("")
        t.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        Elider(t).set_text(name)
        st = CaptionLabel("等待")
        st.setFixedWidth(96)
        pb = ProgressBar()
        pb.setValue(0)
        pb.setFixedWidth(170)
        h.addWidget(t, 1)
        h.addWidget(pb)
        h.addWidget(st)
        if self.hint is not None and self.hint.parent() is not None:
            self.hint.hide()
        self.rows_box.addWidget(w)
        return pb, st

    def start(self):
        if self.running:
            self.running = False
            if self.engine:
                self.engine.stop_sniff()
                self.engine.stop()
            self.btn.setText("开始批量")
            return
        urls = [x.strip() for x in self.urls.toPlainText().splitlines() if x.strip()]
        if not urls:
            self.win.home.append_log("ERROR", "批量任务没有地址")
            return
        clear_layout(self.rows_box)
        self.hint = CaptionLabel("正在展开地址…")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.hint.setTextColor("#8A8A8A", "#9A9A9A")
        self.rows_box.addStretch(1)
        self.rows_box.addWidget(self.hint, 0, Qt.AlignmentFlag.AlignHCenter)
        self.rows_box.addStretch(1)
        save_dir = self.dir.text().strip() or os.path.join(os.path.expanduser("~"), "Desktop")
        try:
            os.makedirs(save_dir, exist_ok=True)
        except Exception as ex:
            self.win.home.append_log("ERROR", "保存目录不可用：%s" % ex)
            return
        # 画质只在这里读一次：QComboBox 是 GUI 对象，下载线程里读它是跨线程访问控件。
        quality = self.qual.currentText()
        settings = dict(self.win.engine.settings)
        settings["batch_save_dir"] = save_dir
        settings["batch_quality"] = quality
        core.save_settings(settings)
        self.engine = core.Engine(settings)
        self.running = True
        self.btn.setText("停止")
        # B 站多 P 展开要发网络请求（bilibili_parts 单次超时 12 秒），
        # 放在 GUI 线程会让整个窗口冻住，交给工作线程，展开完再回主线程建行。
        threading.Thread(target=self.prepare, args=(urls, save_dir, quality), daemon=True).start()

    def prepare(self, urls, save_dir, quality):
        expanded = []
        for u in urls:
            if not self.running:
                self.events.put(("done", 0))
                return
            parts = self.win.engine.bilibili_parts(u) if "bilibili.com" in u else []
            if parts:
                self.events.put(("log", ("INFO", "B站多P：%s 展开为 %d 集" % (u[:50], len(parts)))))
                expanded.extend([p[0] for p in parts])
            else:
                # 播放列表 / 合集（YouTube list=、B站收藏夹、合集等）交给后端 extract_playlist
                # 展开；不是列表或展开失败会自动返回空，回到单条处理。
                plist = self.win.engine.expand_playlist(u)
                if plist:
                    self.events.put(("log", ("INFO", "合集：%s 展开为 %d 条" % (u[:50], len(plist)))))
                    expanded.extend([p[0] for p in plist])
                else:
                    expanded.append(u)
        if not self.running:
            self.events.put(("done", 0))
            return
        self.events.put(("prepared", (expanded, save_dir, quality)))

    def begin(self, urls, save_dir, quality):
        """在 GUI 线程按展开后的地址重建任务行，再启动真正干活的线程。"""
        clear_layout(self.rows_box)
        self.hint = None
        rows = [self.add_row(u) for u in urls]
        self.rows_box.addStretch(1)
        self.thread = threading.Thread(target=self.run, args=(urls, rows, save_dir, quality),
                                       daemon=True)
        self.thread.start()

    def drain_logs(self):
        """把引擎里已排队、但主循环没机会取的日志转发给界面。"""
        while True:
            try:
                k, p = self.engine.events.get_nowait()
            except queue.Empty:
                return
            if k == "log":
                self.events.put(("log", p))

    def run(self, urls, rows, save_dir, quality):
        total = len(urls)
        for i, url in enumerate(urls):
            if not self.running:
                break
            pb, st = rows[i]
            self.events.put(("row", (pb, st, "解析中", 0)))
            if not self.engine.sniff(url):
                self.drain_logs()
                self.events.put(("row", (pb, st, "地址无效", 0)))
                continue
            t0 = time.time()
            res = []
            while time.time() - t0 < 60 and self.running:
                try:
                    k, p = self.engine.events.get(timeout=0.4)
                except queue.Empty:
                    continue
                if k == "resources":
                    res = self.engine.resources()
                elif k == "log":
                    self.events.put(("log", p))
                elif k == "status" and ("解析完成" in p or "未找到" in p or "未识别" in p):
                    break
            res = self.engine.resources() or res
            if not res:
                self.events.put(("row", (pb, st, "未找到资源", 0)))
                continue
            audio_only = quality == "仅音频"
            cands = res
            if quality not in ("最佳画质", "仅音频"):
                picked = [r for r in res if quality.replace("P", "") in (getattr(r, "resolution", "") or "")]
                if picked:
                    cands = picked
            best = max(cands, key=lambda r: (r.quality_score, r.size))
            title = best.title or best.page_title or "video%d" % (i + 1)
            ext = ".mp3" if audio_only else (best.ext or ".mp4")
            if not ext.startswith("."):
                ext = "." + ext
            path = os.path.join(save_dir, safe_name(title) + ext)
            skip_existing = bool(self.engine.settings.get("batch_skip_existing", True))
            if os.path.exists(path):
                if skip_existing:
                    self.events.put(("row", (pb, st, "已存在", 100)))
                    continue
                # 设置里关掉“跳过已存在”时才走这里：另存为，不覆盖原文件
                path = free_path(path)
                self.events.put(("log", ("INFO", "同名文件已存在，改存为：%s" % os.path.basename(path))))
            self.events.put(("row", (pb, st, "下载中", 0)))
            self.engine.download(best, path, audio_only=audio_only)
            t0 = time.time()
            while time.time() - t0 < 600 and self.running:
                try:
                    k, p = self.engine.events.get(timeout=0.4)
                except queue.Empty:
                    continue
                if k == "progress":
                    self.events.put(("row", (pb, st, "下载中", p[0])))
                elif k == "log":
                    self.events.put(("log", p))
                elif k == "download_done":
                    self.events.put(("row", (pb, st, "完成" if p[0] else "失败", 100 if p[0] else 0)))
                    break
        self.events.put(("done", total))
        self.running = False

    def pump(self):
        while True:
            try:
                k, p = self.events.get_nowait()
            except queue.Empty:
                return
            if k == "row":
                pb, st, text, value = p
                try:
                    st.setText(text)
                    if value:
                        pb.setValue(int(value))
                except Exception:
                    pass
            elif k == "done":
                self.btn.setText("开始批量")
                self.win.home.append_log("INFO", "批量任务结束（%d 个地址）" % p)
            elif k == "prepared":
                self.begin(*p)
            elif k == "log":
                self.win.home.append_log(p[0], p[1])


class HistoryPage(Page):
    def __init__(self, win):
        super().__init__(win)
        self.setObjectName("historyPage")
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 14, 26, 20)
        lay.setSpacing(14)
        head = QHBoxLayout()
        head.addWidget(TitleLabel("历史记录"))
        head.addStretch(1)
        b_ref = PushButton(FIF.SYNC, "刷新")
        b_ref.clicked.connect(self.refresh)
        self.b_clear = b_clear = TransparentPushButton(FIF.DELETE, "清空")
        b_clear.clicked.connect(self.clear)
        head.addWidget(b_ref)
        head.addWidget(b_clear)
        lay.addLayout(head)
        c = Card(pad=10)
        scroll = fluent_scroll()
        holder = QWidget()
        holder.setStyleSheet("background:transparent;")
        self.box = QVBoxLayout(holder)
        self.box.setContentsMargins(8, 8, 8, 8)
        self.box.setSpacing(6)
        scroll.setWidget(holder)
        c.box.addWidget(scroll)
        lay.addWidget(c, 1)
        self.refresh()

    def _empty_state(self):
        """空列表占位：图标 + 标题 + 一行说明，垂直居中，不像一条孤零零的灰字。"""
        w = QWidget()
        w.setStyleSheet("background:transparent;")
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        icon = QLabel()
        icon.setPixmap(gray_icon(FIF.HISTORY, 46, "#9A9A9A"))
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet("background:transparent;")
        v.addWidget(icon, 0, Qt.AlignmentFlag.AlignHCenter)

        title = StrongBodyLabel("还没有下载记录")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(title, 0, Qt.AlignmentFlag.AlignHCenter)

        tip = CaptionLabel("下载完成的文件会在这里列出文件名、状态、大小和时间")
        tip.setTextColor("#8A8A8A", "#9A9A9A")
        tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(tip, 0, Qt.AlignmentFlag.AlignHCenter)
        return w

    def refresh(self):
        clear_layout(self.box)
        self.update()
        items = core.load_history()
        if hasattr(self, "b_clear"):
            self.b_clear.setEnabled(bool(items))
        if not items:
            self.box.addStretch(1)
            self.box.addWidget(self._empty_state(), 0, Qt.AlignmentFlag.AlignHCenter)
            self.box.addStretch(1)
            self.update()
            return
        # 固定列宽 + 名称列吃剩余宽度：每行的状态/大小/时间/按钮因此能对齐，
        # 窗口变窄时只有名称列收缩（自动省略号），不会把按钮挤到一起。
        ST_W, SIZE_W, TIME_W = 46, 92, 96
        BTN_W, BTN_H = 54, 26
        for rec in items[:40]:
            row = QHBoxLayout()
            row.setSpacing(8)
            name = rec.get("file_name") or rec.get("title") or "未命名"
            when = (rec.get("time") or "")[5:16]
            size = core.human_size(rec.get("size", 0))
            ok = rec.get("status") == "success"

            t = BodyLabel("")
            t.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            Elider(t).set_text(name)

            st = CaptionLabel("完成" if ok else "失败")
            st.setTextColor("#1E7A34" if ok else "#C42B1C", "#7CC98B")
            st.setFixedWidth(ST_W)

            sz = CaptionLabel(size)
            sz.setFixedWidth(SIZE_W)
            sz.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

            tm = CaptionLabel(when)
            tm.setFixedWidth(TIME_W)
            tm.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

            def _mk(text, tip, handler, r=rec):
                b = TransparentPushButton(text)
                b.setToolTip(tip)
                b.setFixedSize(BTN_W, BTN_H)
                # 14px 下微软雅黑走 ClearType 次像素抗锯齿，链/删这类笔画密的字形
                # 会出现彩色边和"像是画了两遍"的观感；改成 15px + 灰度抗锯齿就干净了。
                f = QFont("Microsoft YaHei UI")
                f.setPixelSize(15)
                f.setStyleStrategy(QFont.StyleStrategy.NoSubpixelAntialias)
                b.setFont(f)
                b.clicked.connect(lambda _=False: handler(r))
                return b

            row.addWidget(t, 1)
            row.addWidget(st, 0)
            row.addWidget(sz, 0)
            row.addWidget(tm, 0)
            row.addSpacing(4)
            row.addWidget(_mk("链接", "复制原视频链接", self.copy_url))
            row.addWidget(_mk("打开", "打开文件", self.open_file))
            row.addWidget(_mk("目录", "打开所在文件夹", self.open_folder))
            row.addWidget(_mk("删除", "从历史里删除这条记录", self.delete))
            self.box.addLayout(row)
        self.box.addStretch(1)

    def copy_url(self, rec):
        url = rec.get("url", "")
        if url:
            QApplication.clipboard().setText(url)
            InfoBar.success("已复制链接", url[:70], parent=self.win, position=InfoBarPosition.TOP_RIGHT, duration=1800)

    def open_file(self, rec):
        p = rec.get("path", "")
        if p and os.path.exists(p):
            os.startfile(p)
        else:
            InfoBar.warning("文件不存在", p, parent=self.win, position=InfoBarPosition.TOP_RIGHT, duration=3000)

    def open_folder(self, rec):
        p = rec.get("path", "")
        d = os.path.dirname(p) if p else ""
        if d and os.path.isdir(d):
            os.startfile(d)

    def delete(self, rec):
        name = rec.get("title") or rec.get("file_name") or "这条记录"
        ok = QMessageBox.question(
            self, "删除记录",
            "确定从历史里删除「%s」吗？\n只删记录，不会动磁盘上的文件。" % name[:40],
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ok != QMessageBox.StandardButton.Yes:
            return
        core.delete_history(rec.get("path", ""))
        self.refresh()

    def clear(self):
        try:
            n = len(core.load_history())
        except Exception:
            n = 0
        ok = QMessageBox.question(
            self, "清空历史",
            "确定清空全部 %d 条历史记录吗？\n只删记录，不会动磁盘上的文件。" % n,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ok != QMessageBox.StandardButton.Yes:
            return
        core.clear_history()
        self.refresh()


class RowCard(Card):
    def __init__(self, icon, title, desc):
        super().__init__(pad=0)
        self.setMinimumHeight(56)
        self.box.setContentsMargins(18, 10, 14, 10)
        self.box.setSpacing(0)
        self.h = QHBoxLayout()
        self.h.setContentsMargins(0, 0, 0, 0)
        self.h.setSpacing(16)
        ic = IconWidget(icon)
        ic.setFixedSize(20, 20)
        col = QVBoxLayout()
        col.setSpacing(0)
        tl = BodyLabel(title)
        tl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        tl.setToolTip(title)
        col.addWidget(tl)
        d = CaptionLabel(desc)
        d.setTextColor("#707070", "#A0A0A0")
        d.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        d.setToolTip(desc)
        col.addWidget(d)
        self.h.addWidget(ic, 0, Qt.AlignmentFlag.AlignVCenter)
        self.h.addLayout(col, 1)
        self.box.addLayout(self.h)

    def add_control(self, w):
        self.h.addWidget(w, 0, Qt.AlignmentFlag.AlignVCenter)


class Tile(Card):
    """设置行：图标 + 标题/说明 + 右侧控件，横向铺满一行。开关类整行可点，不再描主题色边框。"""

    def __init__(self, icon, title, desc="", control=None, switch=None, span=False, height=66):
        super().__init__(pad=0)
        self.setMinimumHeight(height)
        self.setMaximumHeight(height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.span = span
        self._sw = switch
        self.box.setContentsMargins(16, 8, 12, 8)
        self.box.setSpacing(0)
        h = QHBoxLayout()
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(12)
        ic = IconWidget(icon)
        ic.setFixedSize(18, 18)
        h.addWidget(ic, 0, Qt.AlignmentFlag.AlignVCenter)
        # 文字列包一层容器并限宽：标签是 Ignored 策略，不限宽会被右侧输入框挤成 0 宽
        tb = QWidget()
        tb.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        tb.setMinimumWidth(240)
        tb.setMaximumWidth(360)
        col = QVBoxLayout(tb)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)
        tl = BodyLabel(title)
        tl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        tl.setToolTip(title)
        col.addWidget(tl)
        d = CaptionLabel(desc or "")
        d.setTextColor("#707070", "#A0A0A0")
        d.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        d.setToolTip(desc or "")
        col.addWidget(d)
        h.addWidget(tb, 0)
        # 有能撑满的控件（输入框/按钮组）就让它吃掉剩余宽度；否则用弹簧把控件顶到最右
        expands = control is not None and control.maximumWidth() > 10000
        if expands:
            h.addWidget(control, 1, Qt.AlignmentFlag.AlignVCenter)
        else:
            h.addStretch(1)
            if control is not None:
                h.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
        if switch is not None:
            h.addWidget(switch, 0, Qt.AlignmentFlag.AlignVCenter)
        self.box.addLayout(h)
        if switch is not None:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            switch.checkedChanged.connect(lambda _v: self.update())

    def mousePressEvent(self, e):
        # 点磁贴任意位置都等于切开关；点到开关本身时事件被子控件吃掉，不会重复触发
        if self._sw is not None and e.button() == Qt.MouseButton.LeftButton:
            self._sw.setChecked(not self._sw.isChecked())
            e.accept()
            return
        super().mousePressEvent(e)


class TileGrid(QWidget):
    """设置列表：一行一个选项，横向铺满整行，不再分列。"""

    def __init__(self, tiles, parent=None):
        super().__init__(parent)
        self.tiles = list(tiles)
        self._cols = 1
        self.grid = QVBoxLayout(self)
        self.grid.setContentsMargins(0, 6, 18, 10)
        self.grid.setSpacing(10)
        for t in self.tiles:
            self.grid.addWidget(t)
        self.grid.addStretch(1)


class SettingsPage(Page):
    """顶部一排分段选项，点一下切到那组设置；下面是「一行一个选项」的横向列表。"""

    TABS = [("beh", "行为", "SETTING"), ("look", "外观", "BACKGROUND_FILL"),
            ("dl", "下载", "DOWNLOAD"), ("site", "站点", "GLOBE")]

    def __init__(self, win):
        super().__init__(win)
        self.setObjectName("settingsPage")
        self.win = win
        self.s = dict(win.engine.settings)
        self.controls = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(26, 14, 20, 16)
        outer.setSpacing(8)
        head = QHBoxLayout()
        head.addWidget(TitleLabel("设置"))
        head.addStretch(1)
        b_save = PrimaryPushButton(FIF.SAVE, "保存设置")
        b_save.clicked.connect(self.save)
        head.addWidget(b_save)
        outer.addLayout(head)

        self.stack = QStackedWidget(self)
        self._pages = {}
        self._build_pages(outer)

        seg = SegmentedWidget(self)
        for key, text, icon_name in self.TABS:
            # itemClicked(bool) 会把 True 当参数塞进来，签名必须留出这个位置参数，否则 key 变成 True
            seg.addItem(key, text, onClick=lambda _checked=False, k=key: self._tab(k),
                        icon=getattr(FIF, icon_name))
        seg.setCurrentItem("beh")
        self.stack.setCurrentIndex(0)
        # 分段条不铺满整行：4 项各占 130px，比等分铺满更像“选项”
        seg.setFixedWidth(520)
        seg_row = QHBoxLayout()
        seg_row.setContentsMargins(0, 0, 0, 0)
        seg_row.addWidget(seg)
        seg_row.addStretch(1)
        outer.addLayout(seg_row)
        outer.addWidget(self.stack, 1)

    # ---------------- 页面拼装 ----------------
    def _build_pages(self, outer):
        beh = []
        beh.append(self._switch_tile(FIF.GLOBE, "无头浏览器",
                                     "关闭后可在浏览器里手动登录/播放", "headless_browser"))
        beh.append(self._switch_tile(FIF.FOLDER, "完成后打开文件夹", "下载结束时打开保存目录", "auto_open_folder"))
        beh.append(self._switch_tile(FIF.HISTORY, "保存下载历史", "把下载记录写入 download_history.json", "save_history"))
        beh.append(self._switch_tile(FIF.COPY, "剪贴板监听", "复制网页链接后自动填入地址栏", "clipboard_monitor"))
        beh.append(self._switch_tile(FIF.MUSIC, "仅下载音频", "从视频中提取音轨（yt-dlp 站点有效）", "audio_only"))
        beh.append(self._switch_tile(FIF.FONT, "下载字幕", "抓取站点字幕（仅 yt-dlp 站点生效）", "download_subtitles"))
        beh.append(self._switch_tile(FIF.VIDEO, "直播录制", "直播流边下边录，点停止才结束", "live_mode"))
        beh.append(self._switch_tile(FIF.FILTER, "自动过滤广告", "丢弃广告资源与已知广告分片", "auto_filter_ad"))
        beh.append(self._switch_tile(FIF.DELETE, "自动去重", "同一地址只保留一条资源", "auto_dedup"))
        beh.append(self._switch_tile(FIF.SEARCH, "扫描 DOM", "额外抓取页面里的 video/source 标签", "scan_dom"))
        beh.append(self._switch_tile(FIF.SAVE, "批量跳过已存在",
                                     "已存在就跳过；关闭则另存为 (2)", "batch_skip_existing"))

        cb_close = ComboBox()
        cb_close.addItems(["询问", "最小化到托盘", "直接退出"])
        mapping = {"ask": "询问", "tray": "最小化到托盘", "exit": "直接退出"}
        cb_close.setCurrentText(mapping.get(self.s.get("close_action", "ask"), "询问"))
        cb_close.setFixedWidth(150)
        self.cb_close = cb_close
        beh.append(Tile(FIF.CLOSE, "关闭窗口时", "询问 / 最小化到托盘 / 直接退出",
                        control=cb_close, span=True, height=66))


        cb_bd = ComboBox()
        cb_bd.addItems(["无", "亚克力", "云母"])
        cb_bd.setCurrentText(STATE["backdrop"])
        cb_bd.setFixedWidth(150)
        cb_bd.currentTextChanged.connect(self.win.set_backdrop)
        self.cb_backdrop = cb_bd
        cb_radius = ComboBox()
        cb_radius.addItems(["8", "12", "16"])
        cb_radius.setCurrentText(str(STATE["radius"]))
        cb_radius.setFixedWidth(90)
        cb_radius.currentTextChanged.connect(self._set_radius)
        look = [Tile(FIF.BACKGROUND_FILL, "背景效果", "亚克力 / 云母 / 无（会记住，重启不丢）",
                     control=cb_bd, span=True, height=66),
                Tile(FIF.ZOOM, "窗口圆角", "卡片圆角半径（8 / 12 / 16）", control=cb_radius)]

        dl = []
        self.ed_ff = LineEdit()
        self.ed_ff.setText(str(self.s.get("ffmpeg_path", "") or ""))
        self.ed_ff.setFixedHeight(32)
        self.ed_ff.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.controls["ffmpeg_path"] = self.ed_ff
        b_ff = PushButton("选择")
        b_ff.setFixedHeight(32)
        b_ff.clicked.connect(self.pick_ffmpeg)
        box_ff = QWidget()
        h_ff = QHBoxLayout(box_ff)
        h_ff.setContentsMargins(0, 0, 0, 0)
        h_ff.setSpacing(8)
        h_ff.addWidget(self.ed_ff, 1)
        h_ff.addWidget(b_ff, 0)
        dl.append(Tile(FIF.CODE, "FFmpeg 路径",
                       "留空则自动查找：程序目录 / V4.5 目录 / PATH",
                       control=box_ff, span=True, height=74))

        cb_mode = ComboBox()
        cb_mode.addItems(["auto", "local", "ffmpeg"])
        cb_mode.setCurrentText(self.s.get("download_mode", "auto") or "auto")
        cb_mode.setFixedWidth(110)
        self.cb_mode = cb_mode
        dl.append(Tile(FIF.CLOUD_DOWNLOAD, "下载模式", "本地分片优先", control=cb_mode))

        for icon, title, desc, key, val, width in [
            (FIF.SYNC, "重试次数", "失败自动重试", "max_retry", "3", 64),
            (FIF.APPLICATION, "并发线程", "分片并发 1-32", "default_threads", "16", 64),
            (FIF.SPEED_MEDIUM, "限速", "KB/s，0 不限速", "speed_limit", "0", 64),
            (FIF.STOP_WATCH, "请求超时", "单次请求秒数", "timeout", "30", 64),
        ]:
            ed = LineEdit()
            ed.setText(str(self.s.get(key, val) or val))
            ed.setFixedWidth(width)
            ed.setFixedHeight(32)
            self.controls[key] = ed
            dl.append(Tile(icon, title, desc, control=ed))

        site = []
        self.ed_cookie = LineEdit()
        self.ed_cookie.setText(str(self.s.get("ytdlp_cookie_path", "") or ""))
        self.ed_cookie.setFixedHeight(32)
        self.ed_cookie.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.controls["ytdlp_cookie_path"] = self.ed_cookie
        b_ck = PushButton("选择")
        b_ck.setFixedHeight(32)
        b_ck.clicked.connect(self.pick_cookie)
        box_ck = QWidget()
        h_ck = QHBoxLayout(box_ck)
        h_ck.setContentsMargins(0, 0, 0, 0)
        h_ck.setSpacing(8)
        h_ck.addWidget(self.ed_cookie, 1)
        h_ck.addWidget(b_ck, 0)
        site.append(Tile(FIF.CERTIFICATE, "yt-dlp Cookie 文件",
                         "抖音 / B站等站点解析需要 cookies.txt",
                         control=box_ck, span=True, height=74))

        self.sw_proxy = self._switch_tile(FIF.WIFI, "下载走代理",
                                          "嗅探走系统代理，其余走此处", "proxy_enabled")
        site.append(self.sw_proxy)

        self.ed_proxy = LineEdit()
        self.ed_proxy.setText(str(self.s.get("proxy", "") or ""))
        self.ed_proxy.setFixedHeight(32)
        self.ed_proxy.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.controls["proxy"] = self.ed_proxy
        site.append(Tile(FIF.CONNECT, "代理地址", "例如 http://127.0.0.1:7890",
                         control=self.ed_proxy, span=True, height=66))

        for i, (key, tiles) in enumerate((("beh", beh), ("look", look), ("dl", dl), ("site", site))):
            page = self._page(tiles)
            self._pages[key] = i
            self.stack.addWidget(page)

    def _page(self, tiles):
        scroll = fluent_scroll(self)
        scroll.setWidget(TileGrid(tiles, scroll))
        return scroll

    def _tab(self, key):
        idx = self._pages.get(key)
        if idx is not None:
            self.stack.setCurrentIndex(idx)
            self.stack.update()
            self.update()

    def _switch_tile(self, icon, title, desc, key):
        sw = SwitchButton()
        sw.setOnText("")
        sw.setOffText("")
        sw.setChecked(bool(self.s.get(key, False)))
        sw.checkedChanged.connect(lambda v, k=key: self.s.__setitem__(k, bool(v)))
        self.controls[key] = sw
        return Tile(icon, title, desc, switch=sw)

    def _set_radius(self, text):
        try:
            STATE["radius"] = int(text)
        except Exception:
            return
        try:
            self.win.engine.settings["radius"] = STATE["radius"]
            core.save_settings(self.win.engine.settings)
        except Exception:
            pass
        repaint_cards()
        for p in (self.win.home, self.win.batch, self.win.history, self.win.settings, self.win.about):
            p.update()

    def pick_ffmpeg(self):
        p, _ = QFileDialog.getOpenFileName(self, "选择 ffmpeg.exe", self.ed_ff.text() or "",
                                          "可执行文件 (*.exe);;所有文件 (*)")
        if p:
            self.ed_ff.setText(p)

    def pick_cookie(self):
        p, _ = QFileDialog.getOpenFileName(self, "选择 cookies.txt", "", "文本文件 (*.txt);;所有文件 (*)")
        if p:
            self.ed_cookie.setText(p)

    def save(self):
        self.s["ffmpeg_path"] = self.ed_ff.text().strip() or "ffmpeg"
        self.s["ytdlp_cookie_path"] = self.ed_cookie.text().strip()
        self.s["proxy"] = self.ed_proxy.text().strip()
        self.s["update_url"] = UPDATE_URL  # 固定值，界面上不可编辑
        self.s["download_mode"] = self.cb_mode.currentText()
        rev = {"询问": "ask", "最小化到托盘": "tray", "直接退出": "exit"}
        self.s["close_action"] = rev.get(self.cb_close.currentText(), "ask")
        # 数值项：只有填了纯数字才覆盖，避免误清成空串
        for key in ("max_retry", "default_threads", "speed_limit", "timeout"):
            w = self.controls.get(key)
            text = w.text().strip() if w is not None else ""
            if text.isdigit():
                self.s[key] = text
        core.save_settings(self.s)
        self.win.engine.settings = dict(self.s)
        InfoBar.success("设置已保存", "写入 downloader_config.json", parent=self.win,
                        position=InfoBarPosition.TOP_RIGHT, duration=2500)


class HelpDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("使用说明")
        self.resize(620, 560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 16)
        lay.setSpacing(10)
        lay.addWidget(TitleLabel("使用说明"))
        scroll = fluent_scroll(self)
        body = QWidget()
        body.setStyleSheet("background:transparent;")
        box = QVBoxLayout(body)
        box.setContentsMargins(0, 4, 8, 4)
        box.setSpacing(8)
        for title, text in [
            ("1. 单个视频", "把网页或视频地址粘到地址栏，点「开始嗅探」。等结果出来后，挑一个清晰度点「下载」；也可以直接点「下载最佳」让程序自己选最高档。"),
            ("2. 抖音 / B站", "抖音复制分享链接即可，程序会自动跳到视频页并补齐清晰度档位。B站支持多 P 视频，批量页会自动展开成多集。需要登录才能看的内容，去设置里把「无头浏览器」关掉，在弹出的浏览器里手动登录后再嗅探。"),
            ("3. 批量下载", "批量页每行填一个地址，选好保存目录点「开始批量」，任务会逐条执行，队列里能看到进度和状态。"),
            ("4. 直链 / M3U8", "手里已经是 mp4 直链或 m3u8 时，用「直链下载」和「导入 M3U8」。M3U8 的多档主清单会列出来让你选，选完再下载。"),
            ("5. 代理抓包", "点「代理抓包」后，把浏览器或手机代理设成 127.0.0.1:8888 再刷新页面，抓到的视频会回到资源列表。抓 HTTPS 需要先访问 http://mitm.it 装证书。"),
            ("6. 历史与设置", "下载成功的文件会记进历史页，可以打开文件、打开所在文件夹、复制原链接。设置页里能改下载目录、并发、限速、超时、FFmpeg 路径和 Cookie。"),
            ("7. 免责声明", "本工具仅用于个人学习、备份自有版权内容。用户应自行承担使用本工具产生的法律责任；软件不提供、不存储、不分发任何受版权保护的影音资源。\n请只下载自己有权保存的内容，尊重版权。"),
        ]:
            c = Card(pad=12)
            c.box.addWidget(StrongBodyLabel(title))
            lb = BodyLabel(text)
            lb.setWordWrap(True)
            c.box.addWidget(lb)
            box.addWidget(c)
        box.addStretch(1)
        scroll.setWidget(body)
        lay.addWidget(scroll, 1)
        close = PrimaryPushButton("知道了")
        close.clicked.connect(self.accept)
        rowb = QHBoxLayout()
        rowb.addStretch(1)
        rowb.addWidget(close)
        lay.addLayout(rowb)


class AboutPage(Page):
    def __init__(self, win):
        super().__init__(win)
        self.setObjectName("aboutPage")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(26, 14, 20, 20)
        outer.setSpacing(10)
        outer.addWidget(TitleLabel("关于"))
        scroll = fluent_scroll(self)
        body = QWidget()
        body.setStyleSheet("background:transparent;")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 6, 6, 6)
        lay.setSpacing(12)
        c = Card()
        c.box.addWidget(SubtitleLabel("视频下载器 v5.0"))
        c.box.addWidget(BodyLabel("核心：Python（yt-dlp / playwright / ffmpeg）"))
        c.box.addWidget(BodyLabel("界面：PyQt6 + PyQt6-Fluent-Widgets"))
        c.box.addWidget(CaptionLabel("核心复用 V4.5 后端类，未改动原工程代码"))
        lay.addWidget(c)
        c2 = Card()
        c2.box.addWidget(SubtitleLabel("功能模块"))
        for name, desc in [
            ("嗅探", "Playwright 捕获真实请求，抖音含清晰度档位补齐"),
            ("下载", "yt-dlp 与 ffmpeg 直链下载，支持 HLS 分片"),
            ("合并", "音视频分离流自动合并为 MP4"),
            ("站点", "抖音 / B站 / 通用网页"),
            ("历史", "本地 JSON 记录下载历史与配置"),
            ("群聊", "QQ 群 %s 与 Telegram @%s，带二维码和复制" % (GROUP_QQ, GROUP_TG)),
            ("抓包", "内置 mitmproxy 代理嗅探，浏览器走 8888 端口"),
            ("导出", "资源列表导出 JSON，运行日志导出文本"),
        ]:
            row = QHBoxLayout()
            n = StrongBodyLabel(name)
            n.setFixedWidth(48)
            row.addWidget(n)
            row.addWidget(BodyLabel(desc), 1)
            c2.box.addLayout(row)
        lay.addWidget(c2)
        c3 = Card()
        row3 = QHBoxLayout()
        row3.setSpacing(10)
        row3.addWidget(StrongBodyLabel("交流与支持"))
        row3.addStretch(1)
        b_help = PushButton(FIF.QUESTION, "使用说明")
        b_help.clicked.connect(lambda: HelpDialog(win).exec())
        b_group = PrimaryPushButton(FIF.PEOPLE, "加入群聊")
        b_group.clicked.connect(lambda: GroupDialog(win).exec())
        b_sponsor = PushButton(FIF.HEART, "赞助")
        b_sponsor.clicked.connect(lambda: SponsorDialog(win).exec())
        b_update = PushButton(FIF.UPDATE, "检查更新")
        b_update.clicked.connect(lambda: win.check_update(manual=True))
        row3.addWidget(b_help)
        row3.addWidget(b_update)
        row3.addWidget(b_group)
        row3.addWidget(b_sponsor)
        c3.box.addLayout(row3)
        g = CaptionLabel("QQ 群 %s · Telegram @%s" % (GROUP_QQ, GROUP_TG))
        g.setTextColor("#707070", "#A0A0A0")
        c3.box.addWidget(g)
        lay.addWidget(c3)
        g2 = CaptionLabel("本工具仅用于个人学习与备份自有版权内容，使用风险自负；详见「使用说明」第 7 条免责声明。")
        g2.setTextColor("#707070", "#A0A0A0")
        g2.setWordWrap(True)
        lay.addWidget(g2)
        lay.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)


class UpdateDialog(QDialog):
    """「发现新版本」弹窗：当前版本 / 最新版本 / 更新日志 / 立即更新·打开下载页·稍后。

    url  = 「立即更新」打开的地址（安装包直链或 Releases/latest）
    page = 可选的发布页地址；没填就不显示「打开下载页」按钮
    """

    def __init__(self, parent, cur_ver, new_ver, notes, url, page="", newer=True):
        super().__init__(parent)
        self.choice = "later"
        self._url = str(url or "").strip()
        self._page = str(page or "").strip()
        self.setWindowTitle("发现新版本" if newer else "版本不一致")
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setModal(True)
        self.setMinimumWidth(520)
        border = "rgba(255,255,255,0.14)" if STATE["theme"] == "深色" else "rgba(0,0,0,0.10)"

        out = QVBoxLayout(self)
        out.setContentsMargins(24, 20, 24, 18)
        out.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(TitleLabel("发现新版本" if newer else "版本不一致"))
        head.addStretch(1)
        out.addLayout(head)

        info = QFrame()
        info.setObjectName("updInfo")
        info.setStyleSheet("#updInfo{border:1px solid %s; border-radius:8px; background:transparent;}" % border)
        il = QVBoxLayout(info)
        il.setContentsMargins(14, 12, 14, 12)
        il.setSpacing(6)
        il.addWidget(BodyLabel("当前版本：%s" % cur_ver))
        il.addWidget(BodyLabel("最新版本：%s" % new_ver))
        out.addWidget(info)

        out.addWidget(StrongBodyLabel("更新日志"))
        logbox = QFrame()
        logbox.setObjectName("updLog")
        logbox.setStyleSheet("#updLog{border:1px solid %s; border-radius:8px; background:transparent;}" % border)
        ll = QVBoxLayout(logbox)
        ll.setContentsMargins(14, 12, 14, 12)
        ll.setSpacing(6)
        text = (notes or "").strip() or "这一版没有写更新说明。"
        scroll = fluent_scroll(self)
        inner = QWidget()
        inner.setStyleSheet("background:transparent;")
        ib = QVBoxLayout(inner)
        ib.setContentsMargins(0, 0, 6, 0)
        ib.setSpacing(6)
        body = BodyLabel(text)
        body.setWordWrap(True)
        body.setTextColor("#4B4B4B", "#C8C8C8")
        ib.addWidget(body)
        ib.addStretch(1)
        scroll.setWidget(inner)
        scroll.setMinimumHeight(150)
        scroll.setMaximumHeight(230)
        ll.addWidget(scroll)
        out.addWidget(logbox, 1)

        bf = QHBoxLayout()
        bf.setSpacing(10)
        b_go = PrimaryPushButton("立即更新" if newer else "打开下载页")
        b_go.setMinimumWidth(110)
        b_page = PushButton("打开下载页")
        b_page.setMinimumWidth(120)
        b_later = PushButton("稍后")
        b_later.setMinimumWidth(92)
        if self._url:
            b_go.clicked.connect(self._go)
        else:
            b_go.setEnabled(False)
        if self._page:
            b_page.clicked.connect(self._open_page)
        else:
            b_page.hide()
        b_later.clicked.connect(self.reject)
        bf.addWidget(b_go)
        bf.addWidget(b_page)
        bf.addStretch(1)
        bf.addWidget(b_later)
        out.addLayout(bf)

    def _go(self):
        if self._url.lower().startswith(("http://", "https://")):
            webbrowser.open(self._url)
            self.choice = "go"
        self.accept()

    def _open_page(self):
        if self._page.lower().startswith(("http://", "https://")):
            webbrowser.open(self._page)
            self.choice = "page"


def _force_exit(delay=4.0):
    """兜底：quit() 万一卡在原生层，到点直接结束进程，别留下没有窗口的后台进程。

    实测正常退出（Qt 拆控件树）要 2 秒上下，兜底时间留足，免得把正常退出打断
    （打断会出现 QThreadStorage 那类 stderr 噪声）。
    """
    time.sleep(delay)
    os._exit(0)


class MainWindow(FluentWindow):
    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.update()

    def __init__(self, quiet=False):
        super().__init__()
        self.setWindowTitle("视频下载器 v5.0")
        self.resize(1080, 740)
        self.setMinimumSize(760, 560)
        self.setCustomBackgroundColor(QColor("#F3F3F3"), QColor("#202020"))
        self.engine = core.Engine()
        self.engine.settings = core.load_settings()
        # 背景效果要落盘：先按配置初始化 STATE，等窗口显示后再真正应用效果
        STATE["backdrop"] = str(self.engine.settings.get("backdrop", "无") or "无")
        try:
            STATE["radius"] = int(self.engine.settings.get("radius", 8))
        except Exception:
            STATE["radius"] = 8
        self.last_title = ""
        self._clip_last = ""
        nav = self.navigationInterface
        nav.setExpandWidth(200)
        nav.setMinimumExpandWidth(1600)
        nav.setMenuButtonVisible(True)
        self.home = HomePage(self)
        self.batch = BatchPage(self)
        self.history = HistoryPage(self)
        self.settings = SettingsPage(self)
        self.about = AboutPage(self)
        self.addSubInterface(self.home, FIF.DOWNLOAD, "视频下载")
        self.addSubInterface(self.batch, FIF.LIBRARY, "批量下载")
        self.addSubInterface(self.history, FIF.HISTORY, "历史记录")
        self.addSubInterface(self.settings, FIF.SETTING, "设置")
        self.addSubInterface(self.about, FIF.INFO, "关于", NavigationItemPosition.BOTTOM)
        self.apply_radius()
        QTimer.singleShot(0, lambda: self.set_backdrop(STATE["backdrop"], persist=False))
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.pump)
        self.timer.start(150)
        self.clip_timer = QTimer(self)
        self.clip_timer.timeout.connect(self.check_clipboard)
        self.clip_timer.start(1000)
        self.setup_tray()
        self.upd_events = queue.Queue()
        if self.engine.settings.get("check_update", True):
            QTimer.singleShot(3000, lambda: self.check_update(manual=False))
        if self.engine.settings.get("show_welcome", True) and not quiet:
            QTimer.singleShot(400, self.show_welcome)

    def setup_tray(self):
        icon = QIcon(os.path.join(HERE, "app.ico")) if os.path.isfile(os.path.join(HERE, "app.ico")) else FIF.DOWNLOAD.icon()
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip("视频下载器 v5.0")
        menu = QMenu()
        a_show = QAction("显示窗口", self)
        a_show.triggered.connect(self.show_normal)
        a_quit = QAction("退出", self)
        a_quit.triggered.connect(self.quit_app)
        menu.addAction(a_show)
        menu.addSeparator()
        menu.addAction(a_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.show_normal()
                                    if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.show()

    def show_normal(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit_app(self):
        try:
            self.engine.close()
        except Exception:
            pass
        try:
            self.tray.hide()
        except Exception:
            pass
        # 实测：窗口可见时直接 QApplication.quit() 会卡在原生层不返回（进程留在后台，
        # 关窗口/托盘退出/--shot 都能复现），先 hide() 再 quit 就稳定；再加一个兜底线程。
        self.hide()
        threading.Thread(target=_force_exit, daemon=True).start()
        QApplication.quit()

    def show_welcome(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("欢迎使用")
        dlg.setFixedSize(520, 340)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(22, 18, 22, 18)
        lay.setSpacing(10)
        lay.addWidget(TitleLabel("视频下载器 v5.0"))
        for text in ("粘贴网页或视频地址，点「开始嗅探」即可拿到全部清晰度",
                     "抖音、B站、YouTube 等站点会优先走 yt-dlp，解析更快",
                     "设置里改 FFmpeg 路径、Cookie 文件和下载模式，点「保存设置」生效"):
            lay.addWidget(BodyLabel("· " + text))
        lay.addWidget(CaptionLabel("QQ 群 %s · Telegram @%s" % (GROUP_QQ, GROUP_TG)))
        lay.addStretch(1)
        chk = QCheckBox("不再显示这个提示")
        lay.addWidget(chk)
        row = QHBoxLayout()
        row.addStretch(1)
        b = PrimaryPushButton("开始使用")
        def close_it():
            if chk.isChecked():
                s = dict(self.engine.settings)
                s["show_welcome"] = False
                core.save_settings(s)
                self.engine.settings = s
            dlg.accept()
        b.clicked.connect(close_it)
        row.addWidget(b)
        lay.addLayout(row)
        dlg.exec()

    def closeEvent(self, e):
        action = self.engine.settings.get("close_action", "ask")
        if action == "tray":
            self.hide()
            self.tray.showMessage("视频下载器", "已最小化到托盘，点击图标可以恢复", QSystemTrayIcon.MessageIcon.Information, 2500)
            e.ignore()
            return
        if action == "exit":
            self.quit_app()
            e.ignore()
            return
        box = QMessageBox(self)
        box.setWindowTitle("关闭窗口")
        box.setText("要最小化到托盘，还是直接退出？")
        b_tray = box.addButton("最小化到托盘", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("直接退出", QMessageBox.ButtonRole.DestructiveRole)
        b_cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        chk = QCheckBox("记住我的选择")
        box.setCheckBox(chk)
        box.exec()
        clicked = box.clickedButton()
        if clicked is b_cancel:
            e.ignore()
            return
        choice = "tray" if clicked is b_tray else "exit"
        if chk.isChecked():
            s = dict(self.engine.settings)
            s["close_action"] = choice
            core.save_settings(s)
            self.engine.settings = s
        if choice == "tray":
            self.hide()
            e.ignore()
        else:
            self.quit_app()
            e.ignore()

    def best_resource(self):
        res = self.engine.resources()
        if not res:
            return None
        return max(res, key=lambda r: (r.quality_score, r.size))

    def update_url(self):
        """固定地址：硬编码的 UPDATE_URL 永远优先，设置里的值不再生效。"""
        u = str(UPDATE_URL or "").strip()
        # 防御：万一常量被写成 /updata（那是给人看的 HTML 管理页），自动换成同目录 version.json
        low = u.rstrip("/").lower()
        if low.endswith("/updata"):
            u = u.rstrip("/")[:-len("updata")] + "version.json"
        return u

    def toast(self, kind, title, body, duration=3000):
        """同一个位置只留一条提示：新的出现前先收掉上一条。

        连点两次「检查更新」时，两次结果各自弹一条 InfoBar 会上下叠成两条一样的，
        收掉旧的就不会出现这种重影。
        """
        old = getattr(self, "_toast", None)
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        fn = {"success": InfoBar.success, "warning": InfoBar.warning,
              "error": InfoBar.error, "info": InfoBar.info}.get(kind, InfoBar.info)
        bar = fn(title, body, parent=self, position=InfoBarPosition.TOP_RIGHT, duration=duration)
        self._toast = bar
        return bar

    def check_update(self, manual=False):
        """manual=True 走「设置/关于」按钮，失败原因会弹出来；启动检查只静默记录日志。"""
        url = self.update_url()
        if not url:
            if manual:
                self.toast("warning", "还没设置更新地址", "更新地址写死在程序里，当前为空", 4000)
            return
        if not url.lower().startswith(("http://", "https://")):
            if manual:
                self.toast("error", "地址不合法", "更新地址必须是 http(s) 开头的完整 URL", 4000)
            return
        if getattr(self, "_upd_busy", False):
            if manual:
                self.toast("info", "正在检查", "上一次检查还没返回，稍等一下", 2500)
            return
        self._upd_busy = True
        threading.Thread(target=self._upd_worker, args=(url, manual), daemon=True).start()

    def _upd_worker(self, url, manual):
        ok, data = fetch_update(url)
        self.upd_events.put((ok, data, manual))

    def _on_update(self, ok, data, manual):
        self._upd_busy = False
        if not ok:
            self.home.append_log("warning", "检查更新失败：%s" % data)
            if manual:
                self.toast("warning", "检查更新失败", str(data)[:120], 5000)
            return
        ver = str(data.get("version") or "").strip()
        # 判定规则（按用户要求）：远端 == 本地 才算“已是最新”，
        # 只要不相等就弹窗；远端比本地旧也弹，标题改「版本不一致」。
        if _vtuple(ver) == _vtuple(APP_VERSION):
            if manual:
                self.toast("success", "已是最新版本", "当前 v%s" % APP_VERSION, 3000)
            return
        newer = _vtuple(ver) > _vtuple(APP_VERSION)
        dlg = UpdateDialog(self, APP_VERSION, ver,
                           str(data.get("notes") or "").strip(),
                           str(data.get("url") or "").strip(),
                           str(data.get("page") or "").strip(), newer=newer)
        dlg.exec()

    def pump(self):
        self.batch.pump()
        while True:
            try:
                ok, data, manual = self.upd_events.get_nowait()
            except queue.Empty:
                break
            self._on_update(ok, data, manual)
        while True:
            try:
                kind, payload = self.engine.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.home.append_log(payload[0], payload[1])
            elif kind == "resources":
                res = self.engine.resources()
                key = tuple(id(r) for r in res)
                if key != getattr(self, "_fp", None):
                    self._fp = key
                    self.home.rebuild(res)
                    if res:
                        self.last_title = res[0].page_title or res[0].title
            elif kind == "status":
                self.home.set_status(payload)
                if "解析完成" in payload or "未找到" in payload or "未识别" in payload:
                    self.home.btn.setText("开始嗅探")
                    self.home.btn.setIcon(FIF.SEARCH)
            elif kind == "progress":
                self.home.set_progress(payload[0], payload[1])
            elif kind == "download_done":
                self.home.on_done(payload[0], payload[1])

    def check_clipboard(self):
        if not self.engine.settings.get("clipboard_monitor"):
            return
        text = QApplication.clipboard().text().strip()
        if len(text) > 400 or not text.lower().startswith(("http://", "https://")):
            return
        if text == self._clip_last:
            return
        self._clip_last = text
        if not self.home.url.text().strip():
            self.home.url.setText(text)
            self.home.append_log("INFO", "剪贴板检测到链接，已填入地址栏")

    def apply_radius(self):
        repaint_cards()
        for name in ("home", "batch", "history", "settings", "about"):
            p = getattr(self, name, None)
            if p is not None:
                p.update()

    def set_backdrop(self, mode, persist=True):
        STATE["backdrop"] = mode
        if persist:
            # 下拉框只改内存里的 STATE，重启就丢了；这里顺手写回配置文件
            try:
                self.engine.settings["backdrop"] = mode
                core.save_settings(self.engine.settings)
            except Exception:
                pass
        try:
            if mode == "云母":
                self.setMicaEffectEnabled(True)
            else:
                self.setMicaEffectEnabled(False)
                if mode == "亚克力":
                    self.windowEffect.setAcrylicEffect(int(self.winId()), "99FFFFFF", True)
        except Exception as ex:
            InfoBar.warning("背景效果", "应用失败，已回退纯色：%s" % ex, parent=self,
                            position=InfoBarPosition.TOP_RIGHT, duration=3000)
        for p in (self.home, self.batch, self.history, self.settings, self.about):
            p.update()

    def set_theme(self, text):
        STATE["theme"] = "浅色"
        setTheme(Theme.LIGHT)
        for p in (self.home, self.batch, self.history, self.settings, self.about):
            p.update()
        repaint_cards()

    def toggle_theme(self):
        self.set_theme("浅色" if STATE["theme"] == "深色" else "深色")


def main():
    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei UI", 10))
    setTheme(Theme.LIGHT)
    setThemeColor("#0F6CBD")
    page = "home"
    out = r"C:\Users\Administrator\Desktop\V5.0\preview.png"
    url = ""
    for a in sys.argv:
        if a.startswith("--page="):
            page = a.split("=", 1)[1]
        if a.startswith("--out="):
            out = a.split("=", 1)[1]
        if a.startswith("--sniff="):
            url = a.split("=", 1)[1]
    w = MainWindow(quiet=("--shot" in sys.argv or bool(url)))
    w.show()
    if url:
        w.home.url.setText(url)
        QTimer.singleShot(500, w.home.toggle)
    targets = {"home": w.home, "batch": w.batch, "history": w.history, "settings": w.settings, "about": w.about}
    if page in targets and page != "home":
        QTimer.singleShot(300, lambda: w.switchTo(targets[page]))
    if "--shot" in sys.argv:
        def _shot_quit():
            try:
                w.grab().save(out)
            finally:
                w.hide()
                app.quit()
        QTimer.singleShot(1300, _shot_quit)
    elif url:
        QTimer.singleShot(25000, lambda: (w.hide(), app.quit()))
        QTimer.singleShot(6000, lambda: w.grab().save(out))
    app.exec()
    sys.exit(0)


if __name__ == "__main__":
    main()
