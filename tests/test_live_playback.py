import unittest

from app.studio.live_playback import channel_signature, mixer_gains, should_resync
from app.studio.model import Mixer


class LivePlaybackLogicTests(unittest.TestCase):
    def test_channel_hash_is_stable_and_changes_with_timeline_edit(self):
        cue = {"start": 1.0, "end": 2.0, "audio": "voice.wav"}
        first = channel_signature("khmer_voice", [cue])
        self.assertEqual(first, channel_signature("khmer_voice", [dict(cue)]))
        cue["start"] = 1.25
        self.assertNotEqual(first, channel_signature("khmer_voice", [cue]))

    def test_channel_hash_is_channel_specific(self):
        clip = {"start": 0, "path": "fx.wav", "volume": 75}
        self.assertNotEqual(channel_signature("sfx", [clip]), channel_signature("bgm", [clip]))

    def test_gain_mapping_matches_mixer_export_rules(self):
        mixer = Mixer()
        mixer.channels["orig_vocals"].volume = 50
        mixer.channels["khmer_voice"].volume = 125
        mixer.master = 80
        gains = mixer_gains(mixer, {"orig_vocals", "khmer_voice"})
        self.assertAlmostEqual(gains["orig_vocals"], 0.4)
        self.assertAlmostEqual(gains["khmer_voice"], 1.0)
        mixer.channels["khmer_voice"].solo = True
        gains = mixer_gains(mixer, {"orig_vocals", "khmer_voice"})
        self.assertEqual(gains["orig_vocals"], 0.0)
        self.assertAlmostEqual(gains["khmer_voice"], 1.0)
        mixer.channels["khmer_voice"].mute = True
        gains = mixer_gains(mixer, {"orig_vocals", "khmer_voice"})
        self.assertEqual(gains["khmer_voice"], 0.0)

    def test_drift_correction_threshold_is_strictly_over_80ms(self):
        self.assertFalse(should_resync(1000, 1080))
        self.assertTrue(should_resync(1000, 1081))
        self.assertTrue(should_resync(1000, 900))


if __name__ == "__main__":
    unittest.main()
