"""Audio Mixer panel: Demucs button, 6 channels (enable / volume / mute / solo / reset / listen), master, listen to the whole mix.
Holds no data: it edits the studio.model.Mixer it is given and emits `changed`."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QSlider, QFrame, QGridLayout, QCheckBox)

from ..studio.model import MIXER_TRACKS, MIXER_LABELS


class MixerPanel(QWidget):
    changed = Signal()
    demucsRequested = Signal()
    listenRequested = Signal(str)       # channel key, or "" for the whole mix
    stopRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.mixer = None
        self._rows = {}
        self._busy = False
        lay = QVBoxLayout(self); lay.setContentsMargins(8, 6, 8, 6); lay.setSpacing(6)
        top = QHBoxLayout()
        self.demucs_btn = QPushButton("Run Demucs (separate voice / music)")
        self.demucs_btn.setObjectName("primary")
        self.demucs_btn.setToolTip("Splits the ORIGINAL audio into vocals and background. Runs only when you press this - never during Export.")
        self.demucs_btn.clicked.connect(lambda: self.demucsRequested.emit())
        self.demucs_lbl = QLabel("Demucs: not run"); self.demucs_lbl.setObjectName("muted")
        top.addWidget(self.demucs_btn); top.addWidget(self.demucs_lbl, 1)
        self.listen_all = QPushButton("▶ Play mix"); self.listen_all.setToolTip("Plays the video together with the mix (same as the main Play button)")
        self.listen_all.clicked.connect(lambda: self.listenRequested.emit(""))
        stop = QPushButton("■"); stop.setFixedWidth(34); stop.clicked.connect(lambda: self.stopRequested.emit())
        reset = QPushButton("Reset all"); reset.clicked.connect(self._reset_all)
        top.addWidget(self.listen_all); top.addWidget(stop); top.addWidget(reset)
        lay.addLayout(top)
        grid = QGridLayout(); grid.setHorizontalSpacing(8); grid.setVerticalSpacing(4)
        for r, key in enumerate(MIXER_TRACKS + ("master",)):
            name = QLabel(MIXER_LABELS.get(key, "Master")); name.setMinimumWidth(170)
            if key != "master":
                en = QCheckBox(); en.setChecked(True); en.setToolTip("Enable / disable this track (disabled = not played, not exported)")
                grid.addWidget(en, r, 7)
                en.toggled.connect(lambda on, k=key: self._set(k, enabled=on))
            sl = QSlider(Qt.Horizontal); sl.setRange(0, 200); sl.setValue(100)
            val = QLabel("100%"); val.setMinimumWidth(44)
            grid.addWidget(name, r, 0); grid.addWidget(sl, r, 1); grid.addWidget(val, r, 2)
            row = {"name": name, "slider": sl, "val": val}
            if key != "master":
                mute = QPushButton("M"); mute.setCheckable(True); mute.setFixedWidth(30); mute.setToolTip("Mute")
                solo = QPushButton("S"); solo.setCheckable(True); solo.setFixedWidth(30); solo.setToolTip("Solo: only soloed channels are heard")
                rst = QPushButton("↺"); rst.setFixedWidth(30); rst.setToolTip("Reset this channel")
                lis = QPushButton("▶"); lis.setFixedWidth(30); lis.setToolTip("Play with only this channel audible (the video plays too)")
                for c, w in enumerate((mute, solo, rst, lis)):
                    grid.addWidget(w, r, 3 + c)
                mute.toggled.connect(lambda on, k=key: self._set(k, mute=on))
                solo.toggled.connect(lambda on, k=key: self._set(k, solo=on))
                rst.clicked.connect(lambda _=False, k=key: self._reset(k))
                lis.clicked.connect(lambda _=False, k=key: self.listenRequested.emit(k))
                row.update(mute=mute, solo=solo, reset=rst, listen=lis, enabled=en)
            sl.valueChanged.connect(lambda v, k=key: self._vol(k, v))
            self._rows[key] = row
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)
        self.audio_status = QLabel("Audio ready"); self.audio_status.setObjectName("muted")
        lay.addWidget(self.audio_status)
        self.note = QLabel("Settings are saved with the project and used by Export."); self.note.setObjectName("muted")
        lay.addWidget(self.note); lay.addStretch(1)

    def set_audio_status(self, text):
        self.audio_status.setText(str(text))

    # ---- model <-> widgets ----
    def set_mixer(self, mixer):
        self.mixer = mixer
        self._busy = True
        for key, row in self._rows.items():
            if key == "master":
                row["slider"].setValue(mixer.master); row["val"].setText(f"{mixer.master}%")
                continue
            c = mixer.channels[key]
            row["slider"].setValue(c.volume); row["val"].setText(f"{c.volume}%")
            row["mute"].setChecked(c.mute); row["solo"].setChecked(c.solo); row["enabled"].setChecked(c.enabled)
        self._busy = False

    def set_available(self, have):
        """have = {channel: bool}; channels without audio are dimmed (Demucs channels appear after Demucs ran)."""
        for key, row in self._rows.items():
            if key == "master":
                continue
            ok = bool(have.get(key))
            for k_, w in row.items():
                w.setEnabled(ok or k_ == "enabled")
            row["name"].setText(MIXER_LABELS[key] + ("" if ok else "  (no audio yet)"))

    def set_demucs(self, text, running=False):
        self.demucs_lbl.setText(text)
        self.demucs_btn.setEnabled(not running)

    def _vol(self, key, v):
        self._rows[key]["val"].setText(f"{v}%")
        if self._busy or self.mixer is None:
            return
        if key == "master":
            self.mixer.master = v
        else:
            self.mixer.channels[key].volume = v
        self.changed.emit()

    def _set(self, key, mute=None, solo=None, enabled=None):
        if self._busy or self.mixer is None:
            return
        c = self.mixer.channels[key]
        if mute is not None:
            c.mute = bool(mute)
        if solo is not None:
            c.solo = bool(solo)
        if enabled is not None:
            c.enabled = bool(enabled)
        self.changed.emit()

    def _reset(self, key):
        if self.mixer is None:
            return
        self.mixer.reset_channel(key)
        self.set_mixer(self.mixer); self.changed.emit()

    def _reset_all(self):
        if self.mixer is None:
            return
        self.mixer.reset()
        self.set_mixer(self.mixer); self.changed.emit()
