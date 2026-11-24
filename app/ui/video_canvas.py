"""On-screen video editor: the video and every object (logo, watermark, Khmer subtitle, text, blur area) live in ONE
QGraphicsScene whose coordinates are the OUTPUT FRAME in pixels.  Click an object to select it, drag to move, drag a
handle to resize, drag the round handle to rotate.  The numbers are stored normalised in studio.model.Item, so the
export (studio/render.py) draws exactly what you see here.

All maths lives in studio/geometry.py (unit-tested); this file only translates Qt events into those calls.
"""
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QRectF, QPointF, QSizeF
from PySide6.QtGui import (QColor, QPen, QBrush, QPainter, QPixmap, QFont, QFontDatabase, QFontMetricsF, QTransform,
                           QTextOption, QPainterPath, QCursor)
from PySide6.QtWidgets import (QGraphicsView, QGraphicsScene, QGraphicsObject, QGraphicsRectItem, QGraphicsItem, QMenu, QFrame, QSizePolicy)

try:
    from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
except Exception:  # noqa - QtMultimedia missing: the canvas still shows objects on a plain frame
    QGraphicsVideoItem = None

from ..studio import geometry as G
from ..studio.model import Scene, Item

ACCENT = "#3aa0ff"
LONG_SIDE = 1000.0          # scene units of the longer frame side


class ObjItem(QGraphicsObject):
    """Draws one model Item (no mouse handling: VideoCanvas does hit-testing through geometry.py)."""

    def __init__(self, item, canvas):
        super().__init__()
        self.item, self.canvas = item, canvas
        self.setAcceptedMouseButtons(Qt.NoButton)
        self.sync()

    # ---- geometry ----
    def sync(self):
        cv, it = self.canvas, self.item
        self.prepareGeometryChange()
        self.setPos(it.cx * cv.sw, it.cy * cv.sh)
        self.setRotation(it.rot)
        self.update()

    def boundingRect(self):
        cv, it = self.canvas, self.item
        w, h = it.w * cv.sw, it.h * cv.sh
        return QRectF(-w / 2 - 60, -h / 2 - 110, w + 120, h + 170)

    # ---- painting ----
    def paint(self, p, option, widget=None):
        cv, it = self.canvas, self.item
        selected = cv.selected_id == it.id
        live = it.visible_at(cv.time, cv.duration)
        if (not it.enabled or not live) and not selected:
            return
        w, h = it.w * cv.sw, it.h * cv.sh
        rect = QRectF(-w / 2, -h / 2, w, h)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.save()
        if not (it.enabled and live):
            p.setOpacity(0.35)                      # selected but not visible right now: ghost
        if it.kind in ("logo", "watermark"):
            pm = cv.pixmap(it.path)
            if pm is not None and not pm.isNull():
                p.setOpacity(p.opacity() * it.opacity)
                p.drawPixmap(rect, pm, QRectF(pm.rect()))
            else:
                p.setPen(QPen(QColor("#888"), 1, Qt.DashLine)); p.setBrush(QColor(255, 255, 255, 25))
                p.drawRect(rect); p.drawText(rect, Qt.AlignCenter, it.label())
        elif it.kind == "blur":
            p.setPen(QPen(QColor(ACCENT), 1.5, Qt.DashLine))
            p.setBrush(QBrush(QColor(255, 255, 255, 70), Qt.BDiagPattern))
            p.drawRect(rect)
            p.fillRect(rect, QColor(200, 220, 255, int(60 * max(0.2, it.opacity))))
            p.setPen(QColor("#ffffff")); f = QFont(); f.setPixelSize(max(9, int(12 / cv.view_scale()))); p.setFont(f)
            p.drawText(rect.adjusted(4, 2, -4, -2), Qt.AlignLeft | Qt.AlignTop, f"BLUR {it.blur_percent}%")
        else:
            self._paint_text(p, it, rect, w, h)
        p.restore()
        if selected:
            self._paint_handles(p, rect)

    def _paint_text(self, p, it, rect, w, h):
        cv = self.canvas
        txt = ""
        if it.kind == "subtitle":
            txt = cv.cue_text(cv.time)
            if not txt and cv.selected_id == it.id:
                txt = "ឃ្លាអក្សរខ្មែរ · Khmer subtitle"
        else:
            txt = it.text or ("Text" if cv.selected_id == it.id else "")
        if not txt:
            return
        font = cv.font_for(it)
        p.setFont(font)
        px = font.pixelSize()
        p.setOpacity(p.opacity() * it.opacity)
        opt = QTextOption(Qt.AlignCenter)
        opt.setWrapMode(QTextOption.NoWrap if (it.kind == "text" and it.scroll) else QTextOption.WrapAtWordBoundaryOrAnywhere)
        r = rect
        if it.kind == "text" and it.scroll:
            tw = QFontMetricsF(font).horizontalAdvance(txt) + 20
            span = cv.sw + tw
            prog = (cv.time * max(0.02, it.scroll_speed) * cv.sw) % span
            xc = cv.sw + tw / 2 - prog - it.cx * cv.sw
            r = QRectF(xc - tw / 2, -h / 2, tw, h)
        if it.outline:
            ow = max(1.0, px * 0.05)
            p.setPen(QColor(it.outline_color))
            for dx, dy in ((-1, -1), (1, -1), (-1, 1), (1, 1), (0, -1), (0, 1), (-1, 0), (1, 0)):
                p.drawText(r.translated(dx * ow, dy * ow), txt, opt)
        p.setPen(QColor(it.color))
        p.drawText(r, txt, opt)

    def _paint_handles(self, p, rect):
        cv = self.canvas
        k = 1.0 / cv.view_scale()
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(ACCENT), 1.5 * k))
        p.drawRect(rect)
        p.drawLine(QPointF(0, rect.top()), QPointF(0, rect.top() - G.ROT_OFFSET))
        r = 5.5 * k
        p.setBrush(QColor("#ffffff"))
        for sx, sy in ((-1, -1), (0, -1), (1, -1), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0)):
            p.drawRect(QRectF(sx * rect.width() / 2 - r, sy * rect.height() / 2 - r, 2 * r, 2 * r))
        p.setBrush(QColor(ACCENT))
        p.drawEllipse(QPointF(0, rect.top() - G.ROT_OFFSET), r * 1.2, r * 1.2)


class VideoCanvas(QGraphicsView):
    objectSelected = Signal(str)          # item id or ""
    objectEdited = Signal(str)            # while dragging / after a change (inspector refresh)
    editStarted = Signal()                # BEFORE the first change of a drag (history capture)
    editFinished = Signal()               # after the drag ended
    blurAreaDrawn = Signal(float, float, float, float)     # normalised left, top, w, h drawn with the Blur tool
    pictureEdited = Signal()              # picture zoom / offset changed by the mouse
    contextRequested = Signal(str, str)   # action, item id
    toolChanged = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("canvas")
        self.setFrameShape(QFrame.NoFrame)
        self.setBackgroundBrush(QColor("#0b0d12"))
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)      # fitted by default; zoom_view() turns them on
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setMinimumSize(160, 90)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.gs = QGraphicsScene(self)
        self.setScene(self.gs)

        self.model = Scene()
        self.src_w = self.src_h = 0
        self.duration = 0.0
        self.time = 0.0
        self.sw, self.sh = 1000.0, 562.5       # frame size in scene units
        self.selected_id = ""
        self.tool = "select"                   # select | picture | blur
        self.cue_text = lambda t: ""           # set by the page: text of the dialogue line at time t
        self._pm, self._fonts, self._objs = {}, {}, {}
        self._drag = None
        self._user_zoom = False

        self.frame_item = QGraphicsRectItem()
        self.frame_item.setPen(QPen(Qt.NoPen))
        self.frame_item.setFlag(QGraphicsItem.ItemClipsChildrenToShape, True)
        self.gs.addItem(self.frame_item)
        self.pic_root = QGraphicsRectItem(self.frame_item)       # carries the mirror transform
        self.pic_root.setPen(QPen(Qt.NoPen))
        self.pic_clip = QGraphicsRectItem(self.pic_root)         # clips to the cropped picture
        self.pic_clip.setPen(QPen(Qt.NoPen))
        self.pic_clip.setFlag(QGraphicsItem.ItemClipsChildrenToShape, True)
        self.video_item = None
        if QGraphicsVideoItem is not None:
            self.video_item = QGraphicsVideoItem(self.pic_clip)
            self.video_item.setAspectRatioMode(Qt.IgnoreAspectRatio)
            self.video_item.nativeSizeChanged.connect(self._native_size)
        self.obj_root = QGraphicsRectItem(self.frame_item)
        self.obj_root.setPen(QPen(Qt.NoPen))
        self.frame_border = QGraphicsRectItem()                  # thin outline of the frame (drawn above, not clipped)
        self.frame_border.setPen(QPen(QColor("#3a4150"), 0))
        self.frame_border.setBrush(Qt.NoBrush)
        self.frame_border.setZValue(5000)
        self.gs.addItem(self.frame_border)
        self.relayout()

    # ------------------------------------------------------------ model / layout
    def set_model(self, scene):
        self.model = scene
        self.selected_id = ""
        self.relayout()
        self.rebuild_objects()

    def set_source(self, w, h, duration=0.0):
        self.src_w, self.src_h, self.duration = int(w or 0), int(h or 0), float(duration or 0.0)
        self.relayout()

    def set_time(self, t):
        self.time = float(t or 0.0)
        self.gs.update()

    def view_scale(self):
        return max(0.05, self.transform().m11())

    def _native_size(self, size):
        nw, nh = int(size.width()), int(size.height())
        if nw <= 0 or nh <= 0:
            return
        if not self.src_w or not self.src_h:
            self.src_w, self.src_h = nw, nh
            self.relayout()
            return
        # The probed size can disagree with what the player really shows (e.g. phone videos with a rotation flag:
        # probe says 1920x1080, the picture is 1080x1920). The video item is stretched to the computed picture
        # rectangle, so a wrong aspect here would distort the preview -> follow the player's real aspect ratio.
        if abs((nw / nh) / (self.src_w / self.src_h) - 1.0) > 0.02:
            self.src_w, self.src_h = nw, nh
            self.relayout()

    def relayout(self):
        """Recompute frame size and place the picture (fit / fill / zoom / offset / crop / mirror)."""
        f = self.model.frame
        sw_, sh_ = (self.src_w or 1920), (self.src_h or 1080)
        FW, FH = f.out_size(sw_, sh_)
        k = LONG_SIDE / max(FW, FH)
        self.sw, self.sh = FW * k, FH * k
        self.frame_item.setRect(QRectF(0, 0, self.sw, self.sh))
        self.frame_item.setBrush(QColor(f.bg))
        self.frame_border.setRect(QRectF(0, 0, self.sw, self.sh))
        self.pic_root.setRect(QRectF(0, 0, self.sw, self.sh))
        self.obj_root.setRect(QRectF(0, 0, self.sw, self.sh))
        l, t, w, h = f.picture_rect(sw_, sh_)
        px, py, pw, ph = l * self.sw, t * self.sh, w * self.sw, h * self.sh
        self.pic_clip.setRect(QRectF(px, py, pw, ph))
        cw = max(1e-3, 1 - f.crop_l - f.crop_r)
        ch = max(1e-3, 1 - f.crop_t - f.crop_b)
        fw_, fh_ = pw / cw, ph / ch
        if self.video_item is not None:
            self.video_item.setSize(QSizeF(fw_, fh_))
            self.video_item.setPos(px - f.crop_l * fw_, py - f.crop_t * fh_)
        tr = QTransform()
        if self.model.mirror:
            tr.translate(self.sw, 0)
            tr.scale(-1, 1)
        self.pic_root.setTransform(tr)
        self.gs.setSceneRect(QRectF(-70, -120, self.sw + 140, self.sh + 240))
        for o in self._objs.values():
            o.sync()
        if not self._user_zoom:
            self.fit_view()
        self.gs.update()

    def fit_view(self):
        """Fit the whole frame into the viewport: one uniform scale (aspect ratio kept, nothing cropped or stretched),
        centred, with a small gutter. Scrollbars are off while fitted - they would shrink the viewport after the fit
        was computed and leave the picture off-centre; they come back only when the user zooms in."""
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        vp = self.viewport().size()
        k = G.fit_scale(vp.width(), vp.height(), self.sw, self.sh)
        self.resetTransform()
        self.scale(k, k)
        self.centerOn(self.sw / 2.0, self.sh / 2.0)
        self._user_zoom = False

    def zoom_view(self, factor):
        self._user_zoom = True
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scale(factor, factor)
        self.gs.update()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if not self._user_zoom:
            self.fit_view()          # follows every size change of the workspace, so the preview resizes smoothly

    def showEvent(self, e):
        super().showEvent(e)
        if not self._user_zoom:
            self.fit_view()          # first time the viewport has its real size (e.g. when the empty page is replaced)

    # ------------------------------------------------------------ objects
    def rebuild_objects(self):
        for o in list(self._objs.values()):
            self.gs.removeItem(o)
        self._objs = {}
        for i, it in enumerate(self.model.items):
            self._ensure_aspect(it)
            o = ObjItem(it, self)
            o.setParentItem(self.obj_root)
            o.setZValue(i)
            self._objs[it.id] = o
        self.gs.update()

    def refresh_object(self, item_id=None):
        """Call after the model changed from outside (inspector, legacy controls)."""
        if item_id is None or item_id not in self._objs or self.model.get(item_id) is None:
            self.rebuild_objects()
            return
        it = self.model.get(item_id)
        self._ensure_aspect(it)
        self._objs[item_id].sync()

    def _ensure_aspect(self, it):
        if it.kind in ("logo", "watermark"):
            pm = self.pixmap(it.path)
            if pm is not None and not pm.isNull():
                it.h = G.image_aspect_h(it.w, pm.width(), pm.height(), self.sw, self.sh)

    def pixmap(self, path):
        if not path:
            return None
        pm = self._pm.get(path)
        if pm is None:
            pm = QPixmap(str(path)) if Path(path).exists() else QPixmap()
            self._pm[path] = pm
        return pm

    def font_for(self, it):
        fam = it.font_name
        if it.font_file and Path(it.font_file).exists():
            fam = self._fonts.get(it.font_file)
            if fam is None:
                fid = QFontDatabase.addApplicationFont(str(it.font_file))
                names = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
                fam = names[0] if names else (it.font_name or "")
                self._fonts[it.font_file] = fam
        f = QFont(fam) if fam else QFont()
        f.setPixelSize(max(6, int(round(it.size * self.sh / 1080.0))))
        f.setBold(bool(it.bold))
        return f

    def selected(self):
        return self.model.get(self.selected_id) if self.selected_id else None

    def select(self, item_id, emit=True):
        if item_id == self.selected_id:
            return
        self.selected_id = item_id or ""
        self.gs.update()
        if emit:
            self.objectSelected.emit(self.selected_id)

    def set_tool(self, tool):
        self.tool = tool
        self.setCursor(Qt.CrossCursor if tool == "blur" else Qt.OpenHandCursor if tool == "picture" else Qt.ArrowCursor)
        self.toolChanged.emit(tool)

    # ------------------------------------------------------------ hit testing / mouse
    def _pos(self, e):
        p = self.mapToScene(e.position().toPoint())
        return p.x(), p.y()

    def _hit_radius(self):
        return 11.0 / self.view_scale()

    def _item_at(self, x, y):
        for it in reversed(self.model.items):                       # topmost first
            if it.enabled and it.visible_at(self.time, self.duration) and G.contains(it, x, y, self.sw, self.sh, pad=2.0 / self.view_scale()):
                return it
        return None

    def mousePressEvent(self, e):
        self.setFocus()
        if e.button() != Qt.LeftButton:
            return super().mousePressEvent(e)
        x, y = self._pos(e)
        if self.tool == "blur":
            self._drag = {"mode": "blur", "x0": x, "y0": y, "band": None}
            return
        if self.tool == "picture":
            f = self.model.frame
            self._drag = {"mode": "picture", "x0": x, "y0": y, "ox": f.off_x, "oy": f.off_y, "started": False}
            self.setCursor(Qt.ClosedHandCursor)
            return
        sel = self.selected()
        if sel is not None and sel.enabled:
            h = G.hit_handle(sel, x, y, self.sw, self.sh, self._hit_radius(), allow_edges=True)
            if h:
                self._drag = {"mode": "rot" if h == "rot" else "resize", "handle": h, "started": False}
                return
        hit = self._item_at(x, y)
        if hit is not None:
            self.select(hit.id)
            self._drag = {"mode": "move", "x0": x, "y0": y, "started": False, "cx": hit.cx, "cy": hit.cy}
            return
        self.select("")

    def mouseMoveEvent(self, e):
        x, y = self._pos(e)
        d = self._drag
        if d is None:
            self._hover_cursor(x, y)
            return super().mouseMoveEvent(e)
        mods = e.modifiers()
        if d["mode"] == "blur":
            if d["band"] is None:
                d["band"] = QGraphicsRectItem()
                d["band"].setPen(QPen(QColor(ACCENT), 1.5 / self.view_scale(), Qt.DashLine))
                d["band"].setBrush(QColor(58, 160, 255, 50))
                d["band"].setZValue(6000)
                self.gs.addItem(d["band"])
            d["band"].setRect(QRectF(QPointF(d["x0"], d["y0"]), QPointF(x, y)).normalized())
            return
        if d["mode"] == "picture":
            f = self.model.frame
            if not d["started"]:
                self.editStarted.emit(); d["started"] = True
            f.off_x = d["ox"] + (x - d["x0"]) / self.sw
            f.off_y = d["oy"] + (y - d["y0"]) / self.sh
            self.relayout(); self.pictureEdited.emit()
            return
        it = self.selected()
        if it is None:
            return
        if not d["started"]:
            self.editStarted.emit(); d["started"] = True
        if d["mode"] == "move":
            it.cx, it.cy = d["cx"] + (x - d["x0"]) / self.sw, d["cy"] + (y - d["y0"]) / self.sh
            tol = 6.0 / self.view_scale()
            if not (mods & Qt.AltModifier):                         # magnet: frame centre lines
                if abs(it.cx * self.sw - self.sw / 2) < tol:
                    it.cx = 0.5
                if abs(it.cy * self.sh - self.sh / 2) < tol:
                    it.cy = 0.5
        elif d["mode"] == "resize":
            keep = it.kind in ("logo", "watermark") or bool(mods & Qt.ShiftModifier)
            G.resize(it, d["handle"], x, y, self.sw, self.sh, keep_aspect=keep, from_center=bool(mods & Qt.AltModifier))
        elif d["mode"] == "rot":
            G.rotate_to(it, x, y, self.sw, self.sh, snap=bool(mods & Qt.ShiftModifier))
        it.clamp()
        self._objs[it.id].sync()
        self.objectEdited.emit(it.id)

    def mouseReleaseEvent(self, e):
        d, self._drag = self._drag, None
        if d is None:
            return super().mouseReleaseEvent(e)
        if d["mode"] == "blur":
            x, y = self._pos(e)
            if d["band"] is not None:
                self.gs.removeItem(d["band"])
            l, t, w, h = G.rect_from_drag(d["x0"], d["y0"], x, y, self.sw, self.sh)
            self.set_tool("select")
            if w > 0.01 and h > 0.01:
                self.blurAreaDrawn.emit(l, t, w, h)
            return
        if d["mode"] == "picture":
            self.setCursor(Qt.OpenHandCursor)
        if d.get("started"):
            self.editFinished.emit()

    def _hover_cursor(self, x, y):
        if self.tool != "select":
            return
        sel = self.selected()
        cur = Qt.ArrowCursor
        if sel is not None and sel.enabled:
            h = G.hit_handle(sel, x, y, self.sw, self.sh, self._hit_radius())
            if h == "rot":
                cur = Qt.PointingHandCursor
            elif h in ("tl", "br"):
                cur = Qt.SizeFDiagCursor
            elif h in ("tr", "bl"):
                cur = Qt.SizeBDiagCursor
            elif h in ("t", "b"):
                cur = Qt.SizeVerCursor
            elif h in ("l", "r"):
                cur = Qt.SizeHorCursor
            elif self._item_at(x, y) is not None:
                cur = Qt.SizeAllCursor
        elif self._item_at(x, y) is not None:
            cur = Qt.SizeAllCursor
        self.setCursor(cur)

    def wheelEvent(self, e):
        dy = e.angleDelta().y()
        if self.tool == "picture":
            f = self.model.frame
            self.editStarted.emit()
            f.zoom = max(0.1, min(8.0, f.zoom * (1.1 if dy > 0 else 1 / 1.1)))
            self.relayout(); self.pictureEdited.emit(); self.editFinished.emit()
            return
        if e.modifiers() & Qt.ControlModifier:
            self.zoom_view(1.15 if dy > 0 else 1 / 1.15)
            return
        super().wheelEvent(e)

    def keyPressEvent(self, e):
        it = self.selected()
        k, mods = e.key(), e.modifiers()
        if it is not None and k in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down):
            step = (10 if mods & Qt.ShiftModifier else 1)
            dx = {Qt.Key_Left: -step, Qt.Key_Right: step}.get(k, 0)
            dy = {Qt.Key_Up: -step, Qt.Key_Down: step}.get(k, 0)
            self.editStarted.emit()
            G.move(it, dx, dy, self.sw, self.sh)
            self._objs[it.id].sync(); self.objectEdited.emit(it.id); self.editFinished.emit()
            return
        if it is not None and k in (Qt.Key_Delete, Qt.Key_Backspace):
            self.contextRequested.emit("delete", it.id); return
        if it is not None and k == Qt.Key_D and (mods & Qt.ControlModifier):
            self.contextRequested.emit("duplicate", it.id); return
        if k == Qt.Key_Escape:
            self.set_tool("select"); self.select("")
            return
        super().keyPressEvent(e)

    def contextMenuEvent(self, e):
        p = self.mapToScene(e.pos())
        hit = self._item_at(p.x(), p.y())
        if hit is None:
            return
        self.select(hit.id)
        m = QMenu(self)
        acts = {}
        for key, label in (("duplicate", "Duplicate (copy)"), ("delete", "Delete"), ("front", "Bring forward"), ("back", "Send backward"),
                           ("toggle", "Hide / show")):
            acts[m.addAction(label)] = key
        a = m.exec(e.globalPos())
        if a in acts:
            self.contextRequested.emit(acts[a], hit.id)
