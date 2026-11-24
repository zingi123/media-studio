"""Small UI building blocks for the redesigned Translator workspace.

Pure presentation: nothing here owns data. CueTimeline only *draws* the page's cue list
(the cue table / S.Cue objects stay the single source of truth).
"""
from PySide6.QtCore import Qt, Signal, QRectF, QPointF
from PySide6.QtGui import QPainter, QColor, QPen, QPolygonF
from PySide6.QtCore import QSize
from PySide6.QtWidgets import QWidget, QFrame, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QToolButton, QSizePolicy
from .theme import T


def card(title=""):
    """A dark rounded panel. Returns (frame, layout)."""
    f = QFrame()
    f.setObjectName("card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(12, 10, 12, 12)
    lay.setSpacing(8)
    if title:
        t = QLabel(title)
        t.setObjectName("cardTitle")
        lay.addWidget(t)
    return f, lay


def plain(w):
    """Transparent container (so it blends into the card it sits in)."""
    w.setObjectName("plain")
    return w


class Collapsible(QWidget):
    """'▸ Title' button that shows / hides an inner widget (used for Advanced settings)."""

    def __init__(self, title, inner, open=False, parent=None):
        super().__init__(parent)
        self.setObjectName("plain")
        self._title = title
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.btn = QToolButton()
        self.btn.setObjectName("collapse")
        self.btn.setCheckable(True)
        self.btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.btn.setText("▸  " + title)
        self.inner = inner
        inner.setVisible(False)
        self.btn.toggled.connect(self._toggled)
        lay.addWidget(self.btn)
        lay.addWidget(inner)
        if open:
            self.btn.setChecked(True)

    def _toggled(self, on):
        self.btn.setText(("▾  " if on else "▸  ") + self._title)
        self.inner.setVisible(on)


class SectionBox(QFrame):
    """One of the six workflow groups (Media / Script / Translate / Voice / Style / Export) - Figma 'SectionBox'.

    Drop-in for ``Collapsible`` as far as TranslatorPage is concerned: it exposes the same ``.btn`` (a checkable
    button; ``setChecked(True)`` opens the group), ``.inner`` (the live body widget) and ``_title``. Collapsing only
    calls ``setVisible`` on the body, so every control keeps its value, signals and connections.

    The header (icon box, title + one-line description, status dot, chevron) is drawn *inside* ``.btn`` so the whole
    header row is the click target. ``set_status`` only changes the colour of the dot (display only).
    """
    STATES = ("idle", "ready", "running", "warning", "error")

    def __init__(self, title, inner, subtitle="", icon="", open=False, parent=None):
        super().__init__(parent)
        self.setObjectName("sectionBox")
        self._title = title
        self.inner = inner
        lay = QVBoxLayout(self)
        lay.setContentsMargins(1, 1, 1, 1)
        lay.setSpacing(0)

        self.btn = QToolButton()
        self.btn.setObjectName("sectionHeader")
        self.btn.setCheckable(True)
        self.btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.btn.setFixedHeight(int(T["section_header_h"]))
        self.btn.setCursor(Qt.PointingHandCursor)
        self.btn.setAccessibleName(title)
        h = QHBoxLayout(self.btn)
        h.setContentsMargins(12, 0, 12, 0)
        h.setSpacing(10)

        icon_box = QFrame()
        icon_box.setObjectName("sectionIconBox")
        icon_box.setFixedSize(28, 28)
        ib = QHBoxLayout(icon_box)
        ib.setContentsMargins(0, 0, 0, 0)
        self._icon = QLabel(icon)
        self._icon.setObjectName("sectionIcon")
        self._icon.setAlignment(Qt.AlignCenter)
        ib.addWidget(self._icon)
        h.addWidget(icon_box)

        titles = QVBoxLayout()
        titles.setContentsMargins(0, 0, 0, 0)
        titles.setSpacing(0)
        self._title_lbl = QLabel(title)
        self._title_lbl.setObjectName("sectionTitle")
        self._sub_lbl = QLabel(subtitle)
        self._sub_lbl.setObjectName("sectionSub")
        self._sub_lbl.setVisible(bool(subtitle))
        titles.addWidget(self._title_lbl)
        titles.addWidget(self._sub_lbl)
        h.addLayout(titles, 1)

        self.dot = QLabel()
        self.dot.setObjectName("statusDot")
        self.dot.setFixedSize(8, 8)
        self.dot.setProperty("state", "idle")
        h.addWidget(self.dot, 0, Qt.AlignVCenter)
        self._chev = QLabel("▸")
        self._chev.setObjectName("sectionChev")
        self._chev.setFixedWidth(16)
        self._chev.setAlignment(Qt.AlignCenter)
        h.addWidget(self._chev)

        # the header is one big button: labels must let the mouse through
        for w in (icon_box, self._icon, self._title_lbl, self._sub_lbl, self.dot, self._chev):
            w.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        self.divider = QFrame()
        self.divider.setObjectName("sectionDivider")
        self.divider.setFixedHeight(1)
        self.divider.setVisible(False)
        inner.setVisible(False)
        self.btn.toggled.connect(self._toggled)
        lay.addWidget(self.btn)
        lay.addWidget(self.divider)
        lay.addWidget(inner)
        if open:
            self.btn.setChecked(True)

    def _toggled(self, on):
        self._chev.setText("▾" if on else "▸")
        self.inner.setVisible(on)
        self.divider.setVisible(on)

    def set_status(self, state):
        """Colour of the header dot: idle / ready / running / warning / error (display only)."""
        state = state if state in self.STATES else "idle"
        if self.dot.property("state") == state:
            return
        self.dot.setProperty("state", state)
        self.dot.style().unpolish(self.dot)
        self.dot.style().polish(self.dot)


class TransportBar(QWidget):
    """Playback controls under the video: time | back, play, forward | seek bar | extra button.

    Same widgets and signals as before - only the arrangement adapts to the width it gets, so the controls never
    overlap or get squeezed:
      wide   : [time] [back][play][fwd] [=========seek=========] [extra]      (one row)
      narrow : [back][play][fwd]                       [extra]
               [time] [=============seek==============]                      (two rows)
    """
    SEEK_MIN = 160          # the seek bar keeps at least this width in the one-row layout
    GAP = 6

    def __init__(self, time_lbl, back, play, fwd, seek, extra, parent=None, stop=None):
        super().__init__(parent)
        self.setObjectName("plain")
        self._w = dict(time=time_lbl, back=back, play=play, fwd=fwd, seek=seek, extra=extra)
        if stop is not None:                      # STEP 2: optional Stop button right after Play
            self._w["stop"] = stop
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(self.GAP)
        self._grid.setVerticalSpacing(self.GAP)
        self._grid.setSizeConstraint(QGridLayout.SetNoConstraint)
        for w in self._w.values():
            w.setParent(self)
        seek.setMinimumWidth(60)
        seek.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._wide = None
        self._place(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def _hint_w(self, key):
        w = self._w[key]
        w.ensurePolished()          # sizeHint must include the stylesheet's padding / font, not the unstyled defaults
        return max(w.sizeHint().width(), w.minimumWidth())

    def _wide_needed(self):
        keys = ("time", "back", "play", "fwd", "extra") + (("stop",) if "stop" in self._w else ())
        parts = [self._hint_w(k) for k in keys]
        return sum(parts) + self.SEEK_MIN + self.GAP * len(keys)

    def _narrow_needed(self):
        st = self._hint_w("stop") if "stop" in self._w else 0
        row1 = self._hint_w("back") + self._hint_w("play") + self._hint_w("fwd") + self._hint_w("extra") + st + self.GAP * (4 if st else 3)
        btns = self._hint_w("back") + self._hint_w("play") + self._hint_w("fwd") + st + self.GAP * (3 if st else 2)
        row2 = max(self._hint_w("time"), btns) + self.GAP + max(60, self._hint_w("extra"))
        return max(row1, row2)

    def _place(self, wide):
        if wide == self._wide:
            return
        self._wide = wide
        g, w = self._grid, self._w
        for c in range(8):
            g.setColumnStretch(c, 0)
        for x in w.values():
            g.removeWidget(x)
        if wide:
            order = ("time", "back", "play") + (("stop",) if "stop" in w else ()) + ("fwd", "seek", "extra")
            for col, k in enumerate(order):
                g.addWidget(w[k], 0, col)
            g.setColumnStretch(order.index("seek"), 1)
        else:
            row0 = ("back", "play") + (("stop",) if "stop" in w else ()) + ("fwd",)
            n = len(row0)
            for col, k in enumerate(row0):
                g.addWidget(w[k], 0, col)
            g.addWidget(w["extra"], 0, n + 1)
            g.addWidget(w["time"], 1, 0, 1, n)
            g.addWidget(w["seek"], 1, n, 1, 2)
            g.setColumnStretch(n, 1)
        for x in w.values():
            x.show()
        self.setFixedHeight(self._row_h() if wide else self._row_h() * 2 + self.GAP)   # one row / two rows
        self.updateGeometry()

    def _row_h(self):
        for w in self._w.values():
            w.ensurePolished()
        return max(w.sizeHint().height() for w in self._w.values())

    def minimumSizeHint(self):
        return QSize(self._narrow_needed(), self.height())

    def sizeHint(self):
        return QSize(self._wide_needed(), self.height())

    def resizeEvent(self, e):
        self._place(self.width() >= self._wide_needed())
        super().resizeEvent(e)


def _fmt(t):
    t = max(0, int(t))
    return f"{t // 60:02d}:{t % 60:02d}" if t < 3600 else f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}"


class CueTimeline(QWidget):
    """Ruler + playhead + two lanes: Khmer/original text blocks (T1) and generated-voice blocks (Audio).

    get_cues: callable returning the page's current cue list (read on every paint).
    set_state(audio_ok): list[bool], one per cue = 'voice file exists' (computed by the page).
    """
    cueClicked = Signal(int)
    seekRequested = Signal(float)

    GUT = 46      # left label gutter
    RULER = 22

    def __init__(self, get_cues, parent=None):
        super().__init__(parent)
        self._get = get_cues
        self.duration = 0.0
        self.playhead = 0.0
        self.selected = set()
        self.audio_ok = []
        self.setMinimumHeight(112)   # Figma CueTimeline height
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setToolTip("Click a block to select its row · click the ruler to seek")

    # ---- state from the page ----
    def set_duration(self, d):
        self.duration = float(d or 0.0); self.update()

    def set_playhead(self, t):
        self.playhead = float(t or 0.0); self.update()

    def set_selected(self, rows):
        self.selected = set(rows); self.update()

    def set_state(self, audio_ok):
        self.audio_ok = list(audio_ok); self.update()

    # ---- geometry ----
    def _span(self):
        d = self.duration
        cues = self._get() or []
        if cues:
            d = max(d, max(c.end for c in cues))
        return max(d, 1.0)

    def _x(self, t, span):
        return self.GUT + (self.width() - self.GUT - 8) * (t / span)

    def _t_at(self, x, span):
        w = max(1, self.width() - self.GUT - 8)
        return min(span, max(0.0, (x - self.GUT) / w * span))

    def _lanes(self):
        top = self.RULER + 4
        h = (self.height() - top - 6) / 2.0
        return (QRectF(0, top, self.width(), h - 3), QRectF(0, top + h + 3, self.width(), h - 3))

    # ---- painting ----
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(T["tl_bg"]))
        span = self._span()
        cues = self._get() or []
        # ruler
        step = next((s for s in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600) if span / s <= max(2, (self.width() - self.GUT) / 80)), 3600)
        p.setPen(QPen(QColor(T["border_default"]), 1))
        p.drawLine(self.GUT, self.RULER, self.width() - 6, self.RULER)
        t = 0
        while t <= span:
            x = self._x(t, span)
            p.setPen(QPen(QColor(T["border_default"]), 1))
            p.drawLine(int(x), self.RULER - 5, int(x), self.RULER)
            p.setPen(QColor(T["text_muted"]))
            p.drawText(QRectF(x + 3, 2, 60, self.RULER - 4), Qt.AlignLeft | Qt.AlignVCenter, _fmt(t))
            t += step
        # lanes
        lane_t, lane_a = self._lanes()
        for r, name in ((lane_t, "T1"), (lane_a, "Audio")):
            p.setPen(QColor(T["text_muted"]))
            p.drawText(QRectF(6, r.top(), self.GUT - 8, r.height()), Qt.AlignLeft | Qt.AlignVCenter, name)
            p.fillRect(QRectF(self.GUT, r.top(), self.width() - self.GUT - 6, r.height()), QColor(T["tl_lane"]))
        fm = p.fontMetrics()
        for i, c in enumerate(cues):
            x1, x2 = self._x(c.start, span), self._x(c.end, span)
            w = max(3.0, x2 - x1)
            sel = i in self.selected
            has_tr = bool(c.translation.strip())
            # text lane
            r = QRectF(x1, lane_t.top() + 2, w, lane_t.height() - 4)
            p.setBrush(QColor(T["tl_text"] if has_tr else T["tl_text_empty"]))
            p.setPen(QPen(QColor(T["tl_selected"]), 2) if sel else Qt.NoPen)
            p.drawRoundedRect(r, 5, 5)
            if w > 24:
                p.setPen(QColor("#ffffff"))
                txt = (c.translation if has_tr else c.text).replace("\n", " ")
                p.drawText(r.adjusted(5, 0, -3, 0), Qt.AlignLeft | Qt.AlignVCenter, fm.elidedText(txt, Qt.ElideRight, int(w - 8)))
            # audio lane
            if has_tr:
                ok = self.audio_ok[i] if i < len(self.audio_ok) else False
                ra = QRectF(x1, lane_a.top() + 2, w, lane_a.height() - 4)
                if ok:
                    p.setBrush(QColor(T["tl_audio"])); p.setPen(QPen(QColor(T["tl_selected"]), 2) if sel else Qt.NoPen)
                else:
                    p.setBrush(Qt.NoBrush); p.setPen(QPen(QColor(T["warning"]), 1, Qt.DashLine))
                p.drawRoundedRect(ra, 5, 5)
        # playhead
        x = self._x(min(self.playhead, span), span)
        p.setPen(QPen(QColor(T["tl_playhead"]), 2))
        p.drawLine(QPointF(x, self.RULER - 2), QPointF(x, self.height() - 2))
        p.setBrush(QColor(T["tl_playhead"])); p.setPen(Qt.NoPen)
        p.drawPolygon(QPolygonF([QPointF(x - 5, 1), QPointF(x + 5, 1), QPointF(x, 9)]))
        p.end()

    # ---- interaction ----
    def mousePressEvent(self, e):
        span = self._span()
        pos = e.position()
        t = self._t_at(pos.x(), span)
        if pos.y() > self.RULER:
            for i, c in enumerate(self._get() or []):
                if c.start <= t <= c.end:
                    self.cueClicked.emit(i)
                    break
        self.seekRequested.emit(t)
