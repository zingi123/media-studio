"""CapCut-style multi-track timeline.

Tracks (top to bottom):  VIDEO · KHMER (dialogue lines) · AI VOICE · SFX · MUSIC
 * dialogue / voice blocks ARE the page's cue list (one source of truth): moving a block changes the line's Start/End everywhere
 * SFX / MUSIC blocks are audio clips (studio.audio.AudioClip) with their own file, trim and volume
 * drag = move, drag an edge = trim, click = select (Shift / Ctrl add), click anywhere = seek, Ctrl+wheel = zoom, wheel = scroll
 * Split at the playhead, Duplicate, Delete, Merge, Snap, Undo / Redo, real waveforms

No data is owned here; every edit calls the pure functions of studio/tracks.py and then emits `edited`.
"""
import queue
import threading

import numpy as np
from PySide6.QtCore import Qt, Signal, QRectF, QPointF, QObject, QTimer
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QPolygonF, QFontMetrics
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QScrollBar, QMenu, QSizePolicy, QLabel, QFrame)

from ..studio import tracks as TR
from ..studio import audio as AU
from .theme import T

GUT = 78
RULER = 24
GAP = 4
TRACKS = (("video", "VIDEO", 34), ("dialogue", "KHMER", 44), ("orig_vocals", "ORIGINAL", 36),
          ("voice", "KHMER VOICE", 44), ("ai_voice", "AI VOICE", 36), ("orig_bg", "BACKGROUND", 36),
          ("sfx", "SFX", 36), ("bgm", "MUSIC", 36))
COLORS = {"video": "#26324f", "dialogue": T["tl_text"], "dialogue_empty": T["tl_text_empty"], "voice": T["tl_audio"],
          "orig_vocals": "#526b91", "orig_bg": "#397c83",
          "sfx": "#c9782a", "bgm": "#2f9e6b", "ai_voice": "#7a5cc9"}
EDGE = 7
MIN_PPS, MAX_PPS = 2.0, 600.0


class PeakLoader(QObject):
    """Computes waveforms on a background thread; `ready` fires when something new can be drawn."""
    ready = Signal()

    def __init__(self):
        super().__init__()
        self.cache, self._q, self._pending, self._th = {}, queue.Queue(), set(), None

    def get(self, path):
        if not path:
            return None
        a = self.cache.get(path)
        if a is None and path not in self._pending:
            self._pending.add(path)
            self._q.put(path)
            if self._th is None or not self._th.is_alive():
                self._th = threading.Thread(target=self._run, daemon=True)
                self._th.start()
        return a

    def _run(self):
        while True:
            try:
                path = self._q.get(timeout=3)
            except queue.Empty:
                return
            try:
                arr = AU.waveform_peaks(path)
            except Exception:  # noqa - a file without audio simply has no waveform
                arr = np.zeros(1, dtype=np.float32)
            self.cache[path] = arr
            self._pending.discard(path)
            self.ready.emit()

    def forget(self, path):
        self.cache.pop(path, None)


class _Canvas(QWidget):
    seekRequested = Signal(float)
    cueClicked = Signal(int)
    selectionChanged = Signal()
    editStarted = Signal()
    edited = Signal(str)
    contextRequested = Signal(object)

    def __init__(self, owner):
        super().__init__()
        self.o = owner
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumHeight(RULER + sum(h for _, _, h in TRACKS) + GAP * (len(TRACKS) + 1))
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._drag = None
        self._snap_t = None

    # ---------------------------------------------------------- geometry
    def x_of(self, t):
        return GUT + (t - self.o.t0) * self.o.pps

    def t_of(self, x):
        return self.o.t0 + (x - GUT) / self.o.pps

    def track_y(self, key):
        y = RULER + GAP
        for k, _, h in TRACKS:
            if k == key:
                return y, h
            y += h + GAP
        return 0, 0

    def track_at(self, y):
        yy = RULER + GAP
        for k, _, h in TRACKS:
            if yy <= y <= yy + h:
                return k
            yy += h + GAP
        return None

    def blocks(self):
        """Every block on screen: dict(track, kind 'cue'|'clip'|'video', ref (index / id), t0, t1)."""
        o, out = self.o, []
        dur = o.span()
        if o.video_path:
            out.append({"track": "video", "kind": "video", "ref": 0, "t0": 0.0, "t1": max(o.duration, 0.1)})
            # The two source-derived tracks are shown separately from the editable user clips.
            # They are controlled (gain/mute/solo/enable) in the Mixer; source timing remains locked to the video.
            for track, path in getattr(o, "_source_audio_paths", {}).items():
                if path and _exists(path):
                    out.append({"track": track, "kind": "source", "ref": track, "path": path,
                                "t0": 0.0, "t1": max(o.duration, 0.1)})
        for i, c in enumerate(o.cues()):
            out.append({"track": "dialogue", "kind": "cue", "ref": i, "t0": c.start, "t1": max(c.end, c.start + 0.05)})
            if c.translation.strip():
                length = c.voice_dur if getattr(c, "voice_dur", 0) > 0 else (c.end - c.start)
                out.append({"track": "voice", "kind": "cue", "ref": i, "t0": c.start, "t1": c.start + max(length, 0.05)})
        for k in o.clips():
            out.append({"track": k.track if k.track in ("sfx", "bgm", "ai_voice") else "sfx", "kind": "clip", "ref": k.id, "t0": k.start, "t1": k.end})
        return out

    def hit(self, x, y):
        track = self.track_at(y)
        if track is None or x < GUT:
            return None
        t = self.t_of(x)
        best = None
        for b in self.blocks():
            if b["track"] == track and b["kind"] not in ("video", "source") and b["t0"] <= t <= b["t1"]:
                best = b
        if best is None:
            return None
        x0, x1 = self.x_of(best["t0"]), self.x_of(best["t1"])
        zone = "move"
        edge = min(EDGE, max(2.0, (x1 - x0) / 3))
        if best["track"] != "voice":
            if x - x0 <= edge:
                zone = "trim_l"
            elif x1 - x <= edge:
                zone = "trim_r"
        return dict(best, zone=zone)

    # ---------------------------------------------------------- painting
    def paintEvent(self, _e):
        o = self.o
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor(T["tl_bg"]))
        W = self.width()
        t_left, t_right = self.t_of(GUT), self.t_of(W)
        # ruler
        step = next((s for s in (0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600) if s * o.pps >= 70), 3600)
        p.setPen(QPen(QColor(T["border_default"]), 1))
        p.drawLine(GUT, RULER, W, RULER)
        t = int(t_left / step) * step
        while t <= t_right + step:
            x = self.x_of(t)
            if x >= GUT - 1:
                p.setPen(QPen(QColor(T["border_default"]), 1)); p.drawLine(int(x), RULER - 6, int(x), RULER)
                p.setPen(QColor(T["text_muted"])); p.drawText(QRectF(x + 3, 1, 70, RULER - 4), Qt.AlignLeft | Qt.AlignVCenter, _fmt(t))
            t += step
        # lanes
        for key, label, h in TRACKS:
            y, _ = self.track_y(key)
            p.fillRect(QRectF(GUT, y, W - GUT, h), QColor(T["tl_lane"]))
            p.setPen(QColor(T["text_muted"]))
            p.drawText(QRectF(6, y, GUT - 10, h), Qt.AlignLeft | Qt.AlignVCenter, label)
        # blocks
        cues = o.cues()
        for b in self.blocks():
            x0, x1 = self.x_of(b["t0"]), self.x_of(b["t1"])
            if x1 < GUT or x0 > W:
                continue
            y, h = self.track_y(b["track"])
            r = QRectF(max(x0, GUT), y + 2, max(3.0, min(x1, W) - max(x0, GUT)), h - 4)
            self._paint_block(p, b, r, x0, x1)
        # snap guide + playhead
        if self._snap_t is not None:
            x = self.x_of(self._snap_t)
            p.setPen(QPen(QColor("#fbbf24"), 1, Qt.DashLine)); p.drawLine(QPointF(x, RULER), QPointF(x, self.height()))
        x = self.x_of(o.playhead)
        if GUT <= x <= W:
            p.setPen(QPen(QColor(T["tl_playhead"]), 2)); p.drawLine(QPointF(x, RULER - 2), QPointF(x, self.height() - 2))
            p.setBrush(QColor(T["tl_playhead"])); p.setPen(Qt.NoPen)
            p.drawPolygon(QPolygonF([QPointF(x - 6, 1), QPointF(x + 6, 1), QPointF(x, 11)]))
        p.end()

    def _paint_block(self, p, b, r, x0, x1):
        o = self.o
        track, kind, ref = b["track"], b["kind"], b["ref"]
        sel = (kind == "cue" and ref in o.sel_cues) or (kind == "clip" and ref in o.sel_clips)
        peaks, win, label, dashed, bad = None, (0.0, 0.0), "", False, False
        if kind == "video":
            col = COLORS["video"]; peaks = o.peaks.get(o.video_path); win = (self.t_of(r.left()), self.t_of(r.right())); label = "Video"
        elif kind == "source":
            col = COLORS[track]; peaks = o.peaks.get(b.get("path", ""))
            win = (0.0, max(0.05, o.duration)); label = "Original audio" if track == "orig_vocals" else "Demucs background"
        elif track == "dialogue":
            c = o.cues()[ref]
            col = COLORS["dialogue"] if c.translation.strip() else COLORS["dialogue_empty"]
            label = (c.translation if c.translation.strip() else c.text).replace("\n", " ")
        elif track == "voice":
            c = o.cues()[ref]
            ok = bool(c.audio) and _exists(c.audio)
            col = COLORS["voice"]; dashed = not ok
            if ok:
                peaks = o.peaks.get(c.audio); win = (0.0, max(c.voice_dur, 0.05))
            st = o.report.get(ref)
            bad = st in ("long", "too_long")
            label = "" if ok else "voice not generated"
        else:
            k = o.clip_by_id(ref)
            col = COLORS[track]; label = k.name if k else ""
            if k is not None:
                peaks = o.peaks.get(k.path); win = (k.src_in, k.src_in + k.dur)
                if k.mute:
                    col = "#555b68"
        if dashed:
            p.setPen(QPen(QColor(T["warning"]), 1, Qt.DashLine)); p.setBrush(Qt.NoBrush)
        else:
            p.setPen(QPen(QColor(T["tl_selected"]), 2) if sel else Qt.NoPen); p.setBrush(QColor(col))
        p.drawRoundedRect(r, 5, 5)
        if bad:
            p.setPen(QPen(QColor(T["danger"]), 2)); p.setBrush(Qt.NoBrush); p.drawRoundedRect(r, 5, 5)
        if peaks is not None and len(peaks) > 1 and r.width() > 6:
            cols = max(1, int(r.width() / 2))
            if kind in ("video", "source"):
                arr = AU.peaks_for_window(peaks, win[0], win[1], cols)
            else:
                full = max(0.05, x1 - x0)
                f0, f1 = (r.left() - x0) / full, (r.right() - x0) / full       # visible fraction of the block
                w0, w1 = win[0] + (win[1] - win[0]) * f0, win[0] + (win[1] - win[0]) * f1
                arr = AU.peaks_for_window(peaks, w0, w1, cols)
            mx = max(0.02, float(arr.max()) if len(arr) else 0.02)
            mid, hh = r.center().y(), r.height() * 0.42
            p.setPen(QPen(QColor(255, 255, 255, 150), 1))
            for i, a in enumerate(arr):
                x = r.left() + (i + 0.5) * r.width() / cols
                d = max(0.5, float(a) / max(mx, 0.3) * hh)
                p.drawLine(QPointF(x, mid - d), QPointF(x, mid + d))
        if label and r.width() > 26:
            p.setPen(QColor("#ffffff"))
            fm = QFontMetrics(p.font())
            p.drawText(r.adjusted(5, 0, -3, 0), Qt.AlignLeft | Qt.AlignVCenter if kind != "video" and track != "voice" else Qt.AlignLeft | Qt.AlignTop,
                       fm.elidedText(label, Qt.ElideRight, int(r.width() - 8)))

    # ---------------------------------------------------------- mouse
    def mousePressEvent(self, e):
        self.setFocus()
        x, y = e.position().x(), e.position().y()
        o = self.o
        if e.button() == Qt.RightButton:
            h = self.hit(x, y)
            if h is not None and not o.is_selected(h):
                o.select_block(h, False)
            self.contextRequested.emit(self.mapToGlobal(e.position().toPoint()))
            return
        if e.button() != Qt.LeftButton:
            return
        t = max(0.0, self.t_of(x))
        h = self.hit(x, y) if (y > RULER and x >= GUT) else None
        if h is None:
            if not (e.modifiers() & (Qt.ShiftModifier | Qt.ControlModifier)):
                o.clear_selection()
            self._drag = {"mode": "seek"}
            self.seekRequested.emit(min(t, o.span()))
            return
        add = bool(e.modifiers() & (Qt.ShiftModifier | Qt.ControlModifier))
        o.select_block(h, add)
        self.seekRequested.emit(min(t, o.span()))
        items = o.selected_refs()
        self._drag = {"mode": h["zone"], "t": t, "items": items, "primary": h, "started": False,
                      "orig": {("cue", r): (o.cues()[r].start, o.cues()[r].end) for k, r in items if k == "cue"}
                      | {("clip", r): (o.clip_by_id(r).start, o.clip_by_id(r).dur, o.clip_by_id(r).src_in) for k, r in items if k == "clip" and o.clip_by_id(r)}}

    def mouseMoveEvent(self, e):
        x, y = e.position().x(), e.position().y()
        d = self._drag
        o = self.o
        if d is None:
            h = self.hit(x, y)
            self.setCursor(Qt.SizeHorCursor if (h and h["zone"] in ("trim_l", "trim_r")) else Qt.PointingHandCursor if h else Qt.ArrowCursor)
            return
        t = max(0.0, self.t_of(x))
        if d["mode"] == "seek":
            self.seekRequested.emit(min(t, o.span()))
            return
        if abs(t - d["t"]) * o.pps < 3 and not d["started"]:
            return
        if not d["started"]:
            self.editStarted.emit()
            d["started"] = True
        tol = 8.0 / o.pps if o.snap_on else 0.0
        pts = TR.snap_points(o.cues(), o.clips(), o.playhead, o.span(), exclude_cues={r for k, r in d["items"] if k == "cue"},
                             exclude_clips={r for k, r in d["items"] if k == "clip"}) if o.snap_on else []
        self._snap_t = None
        dt = t - d["t"]
        pk, pr = d["primary"]["kind"], d["primary"]["ref"]
        if d["mode"] == "move":
            orig = d["orig"][(pk, pr)]
            length = (orig[1] - orig[0]) if pk == "cue" else orig[1]
            new_start = max(0.0, orig[0] + dt)
            if o.snap_on:
                s2 = TR.snap_span(new_start, length, pts, tol)
                if s2 != new_start:
                    self._snap_t = s2 if abs(s2 - new_start) <= abs((s2 + length) - (new_start + length)) else s2 + length
                new_start = s2
            dt = new_start - orig[0]
            lo = max((-v[0] for v in d["orig"].values()), default=0.0)       # nobody may start before 0
            dt = max(dt, -min(v[0] for v in d["orig"].values()))
            for (kind, ref), v in d["orig"].items():
                if kind == "cue":
                    c = o.cues()[ref]; ln = v[1] - v[0]; c.start, c.end = v[0] + dt, v[0] + dt + ln
                else:
                    k = o.clip_by_id(ref)
                    if k:
                        k.start = v[0] + dt
        else:
            tt = t
            if o.snap_on:
                s2 = TR.snap(tt, pts, tol)
                if s2 != tt:
                    self._snap_t = s2
                tt = s2
            if pk == "cue":
                if d["mode"] == "trim_l":
                    TR.cue_trim_start(o.cues(), pr, tt)
                else:
                    TR.cue_trim_end(o.cues(), pr, tt, o.duration or None)
            else:
                k = o.clip_by_id(pr)
                if k:
                    o_ = d["orig"][("clip", pr)]
                    k.start, k.dur, k.src_in = o_
                    TR.clip_trim_start(k, tt) if d["mode"] == "trim_l" else TR.clip_trim_end(k, tt)
        o.refresh()

    def mouseReleaseEvent(self, e):
        d, self._drag = self._drag, None
        self._snap_t = None
        if d is None or d["mode"] == "seek" or not d.get("started"):
            self.update()
            return
        o = self.o
        cue_refs = [r for k, r in d["items"] if k == "cue"]
        if cue_refs:                                              # keep the list in time order; keep the selection on the same lines
            keep = [o.cues()[r] for r in cue_refs]
            o.sel_cues = set(TR.sort_cues(o.cues(), keep))
        self.edited.emit("both" if cue_refs and any(k == "clip" for k, _ in d["items"]) else "cues" if cue_refs else "clips")
        o.refresh()

    def wheelEvent(self, e):
        dy = e.angleDelta().y() or e.angleDelta().x()
        if e.modifiers() & Qt.ControlModifier:
            self.o.zoom_at(e.position().x(), 1.25 if dy > 0 else 0.8)
        else:
            self.o.scroll_by(-dy / 120.0 * 60.0 / self.o.pps * 3)
        e.accept()

    def keyPressEvent(self, e):
        k, m = e.key(), e.modifiers()
        o = self.o
        if k in (Qt.Key_Delete, Qt.Key_Backspace):
            o.delete_selected()
        elif k == Qt.Key_S and not (m & Qt.ControlModifier):
            o.split_at_playhead()
        elif k == Qt.Key_D and (m & Qt.ControlModifier):
            o.duplicate_selected()
        elif k == Qt.Key_M and not (m & Qt.ControlModifier):
            o.merge_selected()
        elif k == Qt.Key_Z and (m & Qt.ControlModifier):
            o.undoRequested.emit()
        elif k == Qt.Key_Y and (m & Qt.ControlModifier):
            o.redoRequested.emit()
        elif k == Qt.Key_Space:
            o.playPauseRequested.emit()
        elif k in (Qt.Key_Left, Qt.Key_Right):
            step = (1.0 if m & Qt.ShiftModifier else 0.04) * (1 if k == Qt.Key_Right else -1)
            self.seekRequested.emit(max(0.0, min(o.span(), o.playhead + step)))
        elif k == Qt.Key_Home:
            self.seekRequested.emit(0.0)
        else:
            super().keyPressEvent(e)


def _fmt(t):
    if t < 60:
        return f"{t:.1f}s" if t != int(t) else f"{int(t)}s"
    t = int(t)
    return f"{t // 60}:{t % 60:02d}" if t < 3600 else f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}"


def _exists(p):
    from pathlib import Path
    try:
        return Path(p).exists()
    except OSError:
        return False


class StudioTimeline(QWidget):
    """Toolbar + canvas + scrollbar.  Same small API as the old CueTimeline (set_duration / set_playhead / set_selected / set_state,
    cueClicked / seekRequested) plus the editing signals."""
    cueClicked = Signal(int)
    seekRequested = Signal(float)
    selectionChanged = Signal()
    editStarted = Signal()
    edited = Signal(str)
    undoRequested = Signal()
    redoRequested = Signal()
    addAudioRequested = Signal(str)
    regenerateRequested = Signal()
    voiceDeleteRequested = Signal(list)
    playPauseRequested = Signal()

    def __init__(self, get_cues, get_clips, parent=None):
        super().__init__(parent)
        self._get_cues, self._get_clips = get_cues, get_clips
        self.duration = 0.0
        self.playhead = 0.0
        self.video_path = ""
        self._source_audio_paths = {}
        self.t0, self.pps = 0.0, 40.0
        self.snap_on = True
        self.sel_cues, self.sel_clips = set(), set()
        self.report = {}
        self.peaks = PeakLoader()
        self._repaint = QTimer(self); self._repaint.setSingleShot(True); self._repaint.setInterval(120)
        self._repaint.timeout.connect(self.refresh)
        self.peaks.ready.connect(lambda: self._repaint.start())

        lay = QVBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(2)
        bar = QHBoxLayout(); bar.setContentsMargins(4, 2, 4, 0); bar.setSpacing(4)
        self.btn = {}
        for key, text, tip, fn in (("undo", "↶", "Undo (Ctrl+Z)", self.undoRequested.emit), ("redo", "↷", "Redo (Ctrl+Y)", self.redoRequested.emit),
                                   ("split", "✂ Split", "Split the selected block (or the one under the playhead) at the playhead (S)", self.split_at_playhead),
                                   ("dup", "⧉ Duplicate", "Duplicate the selected blocks (Ctrl+D)", self.duplicate_selected),
                                   ("del", "✕ Delete", "Delete the selected blocks (Delete)", self.delete_selected),
                                   ("merge", "⇄ Merge", "Merge the selected neighbouring blocks (M)", self.merge_selected)):
            b = QPushButton(text); b.setToolTip(tip); b.clicked.connect(lambda _=False, f=fn: f()); b.setFixedHeight(26)
            if len(text) == 1:
                b.setFixedWidth(34)
            bar.addWidget(b); self.btn[key] = b
        self.snap_btn = QPushButton("Snap"); self.snap_btn.setCheckable(True); self.snap_btn.setChecked(True); self.snap_btn.setFixedHeight(26)
        self.snap_btn.setToolTip("Blocks stick to other blocks, the playhead and the video start / end")
        self.snap_btn.toggled.connect(lambda on: setattr(self, "snap_on", bool(on)))
        bar.addWidget(self.snap_btn)
        bar.addStretch(1)
        for text, tip, fn in (("−", "Zoom out (Ctrl+wheel)", lambda: self.zoom_at(None, 0.8)), ("+", "Zoom in (Ctrl+wheel)", lambda: self.zoom_at(None, 1.25)),
                              ("Fit", "Show the whole video", self.fit_all)):
            b = QPushButton(text); b.setToolTip(tip); b.setFixedHeight(26); b.clicked.connect(lambda _=False, f=fn: f())
            if len(text) == 1:
                b.setFixedWidth(30)
            bar.addWidget(b)
        bar.addStretch(1)
        for text, tr in (("+ AI Voice", "ai_voice"), ("+ SFX", "sfx"), ("+ Music", "bgm")):
            b = QPushButton(text); b.setFixedHeight(26); b.setToolTip("Add an audio file to this track at the playhead")
            b.clicked.connect(lambda _=False, t=tr: self.addAudioRequested.emit(t)); bar.addWidget(b)
        self.regen_btn = QPushButton("⟳ Regenerate edited"); self.regen_btn.setFixedHeight(26)
        self.regen_btn.setToolTip("Generate the Khmer voice again ONLY for lines whose text / voice / emotion / speed changed")
        self.regen_btn.clicked.connect(lambda: self.regenerateRequested.emit()); bar.addWidget(self.regen_btn)
        lay.addLayout(bar)

        self.canvas = _Canvas(self)
        self.canvas.seekRequested.connect(self.seekRequested)
        self.canvas.cueClicked.connect(self.cueClicked)
        self.canvas.editStarted.connect(self.editStarted)
        self.canvas.edited.connect(self.edited)
        self.canvas.contextRequested.connect(self._menu)
        lay.addWidget(self.canvas, 1)
        self.scroll = QScrollBar(Qt.Horizontal)
        self.scroll.valueChanged.connect(self._scrolled)
        lay.addWidget(self.scroll)
        self.setMinimumHeight(250)
        self._update_scroll()

    # ---------------------------------------------------------- data access
    def cues(self):
        return self._get_cues() or []

    def clips(self):
        return self._get_clips() or []

    def clip_by_id(self, cid):
        return next((k for k in self.clips() if k.id == cid), None)

    def span(self):
        d = self.duration
        cs = self.cues()
        if cs:
            d = max(d, max(c.end for c in cs))
        ks = self.clips()
        if ks:
            d = max(d, max(k.end for k in ks))
        return max(d, 1.0)

    def view_seconds(self):
        return max(0.5, (self.canvas.width() - GUT - 8) / self.pps)

    # ---------------------------------------------------------- state from the page (old CueTimeline API)
    def set_duration(self, d):
        self.duration = float(d or 0.0)
        self._update_scroll()
        self.canvas.update()

    def set_playhead(self, t):
        self.playhead = float(t or 0.0)
        vis = self.view_seconds()
        if self.playhead > self.t0 + vis * 0.95 or self.playhead < self.t0:
            if self._drag_free():
                self.t0 = max(0.0, self.playhead - vis * 0.15)
                self._update_scroll()
        self.canvas.update()

    def _drag_free(self):
        return self.canvas._drag is None

    def set_selected(self, rows):
        self.sel_cues = set(rows)
        self.canvas.update()

    def set_state(self, audio_ok):          # kept for compatibility: the page now tells us through the cues themselves
        self.canvas.update()

    def _refresh_source_audio_paths(self):
        paths = {}
        if self.video_path:
            original = self.video_path
            background = ""
            try:
                if AU.have_stems(self.video_path):
                    stems = AU.stem_paths(self.video_path)
                    original = str(stems["vocals"])
                    background = str(stems["no_vocals"])
            except Exception:
                pass
            paths["orig_vocals"] = original
            if background:
                paths["orig_bg"] = background
        self._source_audio_paths = paths
        for path in paths.values():
            self.peaks.get(path)

    def set_video(self, path):
        self.video_path = str(path or "")
        self._refresh_source_audio_paths()
        if self.video_path:
            self.peaks.get(self.video_path)
        self.canvas.update()

    def set_report(self, report):
        self.report = dict(report or {})
        self.canvas.update()

    def set_history_state(self, can_undo, can_redo):
        self.btn["undo"].setEnabled(can_undo); self.btn["redo"].setEnabled(can_redo)

    def refresh(self):
        # Demucs may have completed since the last refresh; discover its stems and draw the new source lane.
        self._refresh_source_audio_paths()
        for c in self.cues():                                # queue waveforms of the voice clips that exist
            if c.audio and _exists(c.audio):
                self.peaks.get(c.audio)
        for k in self.clips():
            self.peaks.get(k.path)
        self._update_scroll()
        self.canvas.update()

    # ---------------------------------------------------------- selection
    def is_selected(self, b):
        return (b["kind"] == "cue" and b["ref"] in self.sel_cues) or (b["kind"] == "clip" and b["ref"] in self.sel_clips)

    def select_block(self, b, add):
        if not add:
            self.sel_cues, self.sel_clips = set(), set()
        s = self.sel_cues if b["kind"] == "cue" else self.sel_clips
        if b["kind"] not in ("cue", "clip"):
            return
        if add and b["ref"] in s:
            s.discard(b["ref"])
        else:
            s.add(b["ref"])
        if b["kind"] == "cue":
            self.cueClicked.emit(b["ref"])
        self.selectionChanged.emit()
        self.canvas.update()

    def clear_selection(self):
        if self.sel_cues or self.sel_clips:
            self.sel_cues, self.sel_clips = set(), set()
            self.selectionChanged.emit()
        self.canvas.update()

    def selected_refs(self):
        return [("cue", r) for r in sorted(self.sel_cues)] + [("clip", r) for r in sorted(self.sel_clips)]

    # ---------------------------------------------------------- zoom / scroll
    def _update_scroll(self):
        vis = self.view_seconds()
        mx = max(0, int((self.span() + 2 - vis) * 100))
        self.scroll.blockSignals(True)
        self.scroll.setRange(0, mx)
        self.scroll.setPageStep(int(vis * 100))
        self.scroll.setSingleStep(max(1, int(vis * 10)))
        self.t0 = max(0.0, min(self.t0, mx / 100.0))
        self.scroll.setValue(int(self.t0 * 100))
        self.scroll.blockSignals(False)

    def _scrolled(self, v):
        self.t0 = v / 100.0
        self.canvas.update()

    def scroll_by(self, seconds):
        self.t0 = max(0.0, self.t0 + seconds)
        self._update_scroll(); self.canvas.update()

    def zoom_at(self, x, factor):
        x = x if x is not None else GUT + (self.canvas.width() - GUT) / 2
        anchor = self.t0 + (x - GUT) / self.pps
        self.pps = max(MIN_PPS, min(MAX_PPS, self.pps * factor))
        self.t0 = max(0.0, anchor - (x - GUT) / self.pps)
        self._update_scroll(); self.canvas.update()

    def fit_all(self):
        self.pps = max(MIN_PPS, min(MAX_PPS, (self.canvas.width() - GUT - 12) / max(1.0, self.span())))
        self.t0 = 0.0
        self._update_scroll(); self.canvas.update()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._update_scroll()

    # ---------------------------------------------------------- editing commands (all go through studio/tracks.py)
    def _edit(self, fn, what):
        self.editStarted.emit()
        res = fn()
        self.edited.emit(what)
        self.refresh()
        return res

    def split_at_playhead(self):
        t = self.playhead
        cues, clips = self.cues(), self.clips()
        cue_rows = sorted(self.sel_cues) or [i for i, c in enumerate(cues) if c.start < t < c.end]
        clip_ids = list(self.sel_clips) or ([] if self.sel_cues else [k.id for k in clips if k.start < t < k.end])
        if not cue_rows and not clip_ids:
            return

        def do():
            new_sel = set()
            for r in sorted(cue_rows, reverse=True):
                res = TR.cue_split(cues, r, t)
                if res:
                    new_sel.update({res[0], res[1]})
            for cid in clip_ids:
                k = next((j for j, c in enumerate(clips) if c.id == cid), None)
                if k is not None:
                    TR.clip_split(clips, k, t)
            if cue_rows:
                self.sel_cues = set(i for i in range(len(cues)) if any(abs(cues[i].start - t) < 1e-3 or abs(cues[i].end - t) < 1e-3 for _ in [0]))
        self._edit(do, "both" if cue_rows and clip_ids else "cues" if cue_rows else "clips")

    def duplicate_selected(self):
        cues, clips = self.cues(), self.clips()
        rows, ids = sorted(self.sel_cues), list(self.sel_clips)
        if not rows and not ids:
            return

        def do():
            if rows:
                self.sel_cues = set(TR.cue_duplicate(cues, rows))
            if ids:
                idx = [i for i, k in enumerate(clips) if k.id in ids]
                new = TR.clip_duplicate(clips, idx)
                self.sel_clips = {clips[i].id for i in new}
        self._edit(do, "both" if rows and ids else "cues" if rows else "clips")

    def delete_selected(self):
        cues, clips = self.cues(), self.clips()
        rows, ids = sorted(self.sel_cues), list(self.sel_clips)
        if not rows and not ids:
            return

        def do():
            TR.cue_delete(cues, rows)
            TR.clip_delete(clips, [i for i, k in enumerate(clips) if k.id in ids])
            self.sel_cues, self.sel_clips = set(), set()
        self._edit(do, "both" if rows and ids else "cues" if rows else "clips")

    def merge_selected(self):
        cues, clips = self.cues(), self.clips()
        rows, ids = sorted(self.sel_cues), list(self.sel_clips)
        if len(rows) < 2 and len(ids) < 2:
            return

        def do():
            if len(rows) >= 2:
                i = TR.cue_merge(cues, rows)
                self.sel_cues = {i} if i is not None else set()
            if len(ids) >= 2:
                k = TR.clip_merge(clips, [i for i, c in enumerate(clips) if c.id in ids])
                self.sel_clips = {clips[k].id} if k is not None else set()
        self._edit(do, "both" if len(rows) >= 2 and len(ids) >= 2 else "cues" if len(rows) >= 2 else "clips")

    def set_clip_volume(self, cid, volume=None, mute=None):
        k = self.clip_by_id(cid)
        if k is None:
            return
        def do():
            if volume is not None:
                k.volume = int(volume)
            if mute is not None:
                k.mute = bool(mute)
        self._edit(do, "clips")

    def _menu(self, gpos):
        m = QMenu(self)
        acts = {}
        for key, label in (("split", "Split at playhead"), ("dup", "Duplicate"), ("merge", "Merge"), ("del", "Delete")):
            acts[m.addAction(label)] = key
        if self.sel_cues:
            m.addSeparator()
            acts[m.addAction("Delete generated voice only")] = "voice"
            acts[m.addAction("Regenerate edited lines")] = "regen"
        if len(self.sel_clips) >= 1:
            m.addSeparator()
            acts[m.addAction("Mute / unmute clip")] = "mute"
            acts[m.addAction("Volume 50%")] = "v50"
            acts[m.addAction("Volume 100%")] = "v100"
            acts[m.addAction("Volume 150%")] = "v150"
        a = m.exec(gpos)
        key = acts.get(a)
        if key == "split":
            self.split_at_playhead()
        elif key == "dup":
            self.duplicate_selected()
        elif key == "merge":
            self.merge_selected()
        elif key == "del":
            self.delete_selected()
        elif key == "voice":
            self.voiceDeleteRequested.emit(sorted(self.sel_cues))
        elif key == "regen":
            self.regenerateRequested.emit()
        elif key == "mute":
            for cid in list(self.sel_clips):
                k = self.clip_by_id(cid)
                if k:
                    self.set_clip_volume(cid, mute=not k.mute)
        elif key in ("v50", "v100", "v150"):
            for cid in list(self.sel_clips):
                self.set_clip_volume(cid, volume=int(key[1:]))
