"""BatchMixin: dub MANY videos one after another with the same settings - ask once, then no questions.

For every video: load it -> the normal Auto Dub chain (Scan -> Translate -> Review -> [Demucs] -> Performance -> Voice -> Check -> Timing fit ->
Quality -> Export) -> next.  A problem in one video is logged and the queue goes on with the next one (finished steps of that video are kept:
run the batch again and it resumes).  STOP ALL ends the whole batch.  Nothing new is computed here: it only drives AutoDubMixin.
"""
from pathlib import Path

from PySide6.QtWidgets import QCheckBox, QFileDialog, QMessageBox

from ..shared.model_manager import MM

VIDEO_FILTER = "Video (*.mp4 *.mkv *.mov *.avi *.webm *.flv *.m4v)"


class BatchMixin:
    _batch = None

    def batch_clicked(self):
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "Batch", "Another step is still running. Press STOP ALL first.")
            return
        files, _ = QFileDialog.getOpenFileNames(self, "Choose the videos to dub (in this order)", "", VIDEO_FILTER)
        if not files:
            return
        files = sorted(files)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question); box.setWindowTitle("Batch Auto Dub")
        box.setText(f"Dub {len(files)} video(s) one after another?")
        box.setInformativeText("Same language, accuracy, voice and export settings for all. No questions are asked while it runs; a problem in "
                               "one video is written to the log and the next video starts. Each video gets its own quality report.\n"
                               "STOP ALL ends the batch; run it again to resume.")
        cb = QCheckBox("Also run Demucs (music removed before Whisper listens, original sound kept as background)")
        cb.setChecked(bool(self.ctx.settings.get("autodub_demucs")))
        box.setCheckBox(cb)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel); box.setDefaultButton(QMessageBox.Yes)
        if box.exec() != QMessageBox.Yes:
            return
        self.ctx.settings.set("autodub_demucs", bool(cb.isChecked())); self.ctx.settings.save()
        self._batch = {"queue": list(files), "total": len(files), "ok": [], "failed": [], "demucs": bool(cb.isChecked()), "current": ""}
        self.tog["Process"].setChecked(True)
        self.log.appendPlainText(f"═══ Batch started: {len(files)} video(s) ═══")
        self._batch_step()

    # ------------------------------------------------------------------ driver
    def _batch_wait(self):
        try:
            self.worker.wait()                      # a new _run says 'Busy' until the finished thread is gone
        except Exception:  # noqa
            pass

    def _batch_step(self):
        b = self._batch
        if b is None:
            return
        self._batch_wait()
        if not b["queue"]:
            return self._batch_finish()
        path = b["queue"].pop(0)
        b["current"] = path
        n = b["total"] - len(b["queue"])
        self.log.appendPlainText(f"═══ Batch {n}/{b['total']}: {Path(path).name} ═══")
        try:
            MM.unload_all()                         # free the previous video's models
            self.cues, self.story = [], None
            self.load_video(path)
            self.refresh()
        except Exception as e:  # noqa
            return self._batch_result(False, f"cannot open the video: {e}")
        self._auto = {"demucs": b["demucs"], "batch": True}
        self._ad_step("scan")

    def _batch_result(self, ok, why=""):
        b = self._batch
        if b is None:
            return
        name = Path(b["current"]).name
        (b["ok"] if ok else b["failed"]).append(name if ok else f"{name} - {why}")
        self.log.appendPlainText(("✓ " if ok else "✗ ") + name + ("" if ok else f": {why}"))
        self._batch_wait()
        self._batch_step()

    def _batch_video_done(self):
        self._batch_result(True)

    def _batch_video_failed(self, why=""):
        self._batch_result(False, why or "stopped")

    def _batch_finish(self):
        b, self._batch = self._batch, None
        self._auto = None
        msg = f"Batch finished: {len(b['ok'])} done, {len(b['failed'])} failed of {b['total']}."
        self.log.appendPlainText("═══ " + msg + " ═══")
        for f in b["failed"]:
            self.log.appendPlainText("   ✗ " + f)
        QMessageBox.information(self, "Batch Auto Dub", msg + ("\n\nFailed:\n" + "\n".join(b["failed"][:12]) if b["failed"] else ""))
