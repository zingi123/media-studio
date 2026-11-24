"""Cooperative pause/resume/cancel semantics for background workers."""
import threading
import time

import qt_stub
qt_stub.install()

from app.ui.workers import Worker


def test_worker_pause_blocks_at_checkpoint_then_resumes():
    worker = Worker(lambda: None)
    worker.pause()
    finished = threading.Event()
    result = []

    def checkpoint():
        result.append(worker._checkpoint())
        finished.set()

    thread = threading.Thread(target=checkpoint)
    thread.start()
    time.sleep(0.03)
    assert not finished.is_set()
    assert worker.is_paused
    worker.resume()
    thread.join(1)
    assert finished.is_set()
    assert result == [False]


def test_worker_cancel_releases_paused_checkpoint():
    worker = Worker(lambda: None)
    worker.pause()
    result = []
    thread = threading.Thread(target=lambda: result.append(worker._checkpoint()))
    thread.start()
    time.sleep(0.03)
    worker.cancel()
    thread.join(1)
    assert not thread.is_alive()
    assert result == [True]
