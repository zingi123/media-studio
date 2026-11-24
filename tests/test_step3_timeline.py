"""STEP 3 timeline editing regression tests (no Qt required)."""
import unittest
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.translator.subtitles import Cue
from app.studio.audio import AudioClip
from app.studio.model import Scene
from app.studio import tracks as TR


class CueEditingTests(unittest.TestCase):
    def test_move_and_trim_keep_valid_timing(self):
        cues = [Cue(2.0, 4.0, "hello", "សួស្តី")]
        TR.cue_move(cues, 0, 5.0, duration=8.0)
        self.assertEqual((cues[0].start, cues[0].end), (5.0, 7.0))
        TR.cue_trim_start(cues, 0, 6.8)
        self.assertGreaterEqual(cues[0].end - cues[0].start, TR.MIN_LEN - 1e-8)
        TR.cue_trim_end(cues, 0, 6.0)
        self.assertGreaterEqual(cues[0].end - cues[0].start, TR.MIN_LEN - 1e-8)

    def test_split_clears_old_generated_voice(self):
        cue = Cue(0.0, 4.0, "hello there", "សួស្តី អ្នកទាំងអស់គ្នា", audio="old.wav", voice_dur=3.0)
        cues = [cue]
        result = TR.cue_split(cues, 0, 2.0)
        self.assertEqual(result, (0, 1))
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0].end, cues[1].start)
        self.assertFalse(cues[0].audio)
        self.assertFalse(cues[1].audio)

    def test_duplicate_delete_and_merge(self):
        cues = [Cue(0, 1, "one", "មួយ"), Cue(2, 3, "two", "ពីរ")]
        made = TR.cue_duplicate(cues, [0])
        self.assertEqual(len(made), 1)
        self.assertEqual((cues[made[0]].start, cues[made[0]].end), (1, 2))
        merged = TR.cue_merge(cues, [0, 1])
        self.assertIsNotNone(merged)
        self.assertEqual(len(cues), 2)
        TR.cue_delete(cues, [1])
        self.assertEqual(len(cues), 1)


class AudioClipEditingTests(unittest.TestCase):
    def make_clip(self):
        return AudioClip(track="ai_voice", path="voice.wav", name="voice.wav",
                         start=1.0, src_in=0.0, dur=4.0, src_len=8.0)

    def test_move_trim_split_duplicate_delete(self):
        clips = [self.make_clip()]
        TR.clip_move(clips[0], 3.0, duration=10.0)
        self.assertEqual(clips[0].start, 3.0)
        TR.clip_trim_start(clips[0], 4.0)
        self.assertEqual(clips[0].src_in, 1.0)
        self.assertEqual(clips[0].dur, 3.0)
        TR.clip_trim_end(clips[0], 6.0)
        self.assertEqual(clips[0].dur, 2.0)
        split = TR.clip_split(clips, 0, 5.0)
        self.assertEqual(split, (0, 1))
        self.assertEqual(clips[1].src_in, clips[0].src_in + clips[0].dur)
        copies = TR.clip_duplicate(clips, [0])
        self.assertEqual(len(copies), 1)
        self.assertEqual(clips[copies[0]].start, clips[0].end)
        TR.clip_delete(clips, [copies[0]])
        self.assertEqual(len(clips), 2)

    def test_merge_requires_contiguous_same_track_and_file(self):
        clips = [
            AudioClip(track="ai_voice", path="a.wav", start=0, src_in=0, dur=1),
            AudioClip(track="ai_voice", path="a.wav", start=1, src_in=1, dur=2),
        ]
        idx = TR.clip_merge(clips, [0, 1])
        self.assertEqual(idx, 0)
        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0].dur, 3)


class HistoryTests(unittest.TestCase):
    def test_undo_redo_restores_cues_and_audio_clips(self):
        cues = [Cue(0, 1, "before", "មុន")]
        clips = [AudioClip(track="ai_voice", path="a.wav", start=0, dur=1)]
        scene = Scene()
        hist = TR.StudioHistory()
        hist.capture(cues, clips, scene)
        cues[0].translation = "ក្រោយ"
        clips[0].start = 5
        ok, _ = hist.undo(cues, clips, scene)
        self.assertTrue(ok)
        self.assertEqual(cues[0].translation, "មុន")
        self.assertEqual(clips[0].start, 0)
        ok, _ = hist.redo(cues, clips, scene)
        self.assertTrue(ok)
        self.assertEqual(cues[0].translation, "ក្រោយ")
        self.assertEqual(clips[0].start, 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
