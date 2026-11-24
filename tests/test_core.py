"""Run: python -m unittest discover -s tests -v
(needs ffmpeg for the media tests)
"""

import os
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("MEDIASTUDIO_HOME", tempfile.mkdtemp())

from app.downloader.url_detector import detect, DRAMA, SOCIAL  # noqa
from app.downloader.models import Item  # noqa
from app.downloader.organizer import target_dir, base_name  # noqa
from app.shared.ffmpeg_util import find_ffmpeg, probe  # noqa
from app.translator import subtitles as S  # noqa
from app.converter.video_splitter import plan_segments, split_video  # noqa
from app.converter.frame_extractor import extract_frames  # noqa
from app.converter.video_converter import convert_video  # noqa
from app.converter.image_converter import convert_image  # noqa


# ============================================================
# URL DETECTOR TESTS
# ============================================================

# ============================================================
# URL DETECTOR TESTS
# ============================================================

class Detect(unittest.TestCase):

    def test_social(self):
        examples = [
            ("https://www.youtube.com/watch?v=x", "YouTube"),
            ("https://youtu.be/x", "YouTube"),
            ("https://vm.tiktok.com/abc", "TikTok"),
            ("https://www.instagram.com/reel/x", "Instagram"),
            ("https://fb.watch/x", "Facebook"),
            ("https://pin.it/x", "Pinterest"),
            ("https://v.douyin.com/x", "Douyin"),
            ("https://x.com/user/status/1", "X / Twitter"),
            ("https://old.reddit.com/r/a/comments/1", "Reddit"),
            ("https://www.bilibili.tv/en/video/1", "BiliBili"),
        ]

        for url, expected_name in examples:
            with self.subTest(url=url):
                result = detect(url)
                self.assertIsNotNone(result, url)
                self.assertEqual(result.name, expected_name, url)

    def test_all_social_catalogue_domains(self):
        """Every domain registered in SOCIAL must be detected."""

        self.assertGreater(len(SOCIAL), 0, "SOCIAL catalogue is empty")

        for site in SOCIAL:
            for domain in site.domains:
                with self.subTest(site=site.name, domain=domain):
                    result = detect(f"https://{domain}/test")

                    self.assertIsNotNone(
                        result,
                        f"Could not detect {domain}"
                    )

                    self.assertEqual(
                        result.name,
                        site.name,
                        f"Wrong platform detected for {domain}"
                    )

    def test_social_www_domains(self):
        """Test www subdomains for registered social platforms."""

        for site in SOCIAL:
            for domain in site.domains:
                with self.subTest(site=site.name, domain=domain):
                    result = detect(f"https://www.{domain}/test")

                    self.assertIsNotNone(
                        result,
                        f"Could not detect www.{domain}"
                    )

                    self.assertEqual(result.name, site.name)

    def test_drama_sites(self):
        examples = [
            ("https://khdiamond.net/series/1", "KHDiamond"),
            ("https://kisskh.org/Drama/Abc?id=1", "KiSSKH"),
            ("https://www.khfullhd.net/movie/x", "KHFullHD"),
            ("https://khfullhd.co/movie/x", "KHFullHD"),
            ("https://hongguoduanju.com/play/1", "Hongguo"),
        ]

        for url, expected_name in examples:
            with self.subTest(url=url):
                result = detect(url)
                self.assertIsNotNone(result, url)
                self.assertEqual(result.name, expected_name, url)

    def test_only_four_drama_sites(self):
        self.assertEqual(
            sorted(site.name for site in DRAMA),
            ["Hongguo", "KHDiamond", "KHFullHD", "KiSSKH"]
        )

    def test_all_drama_domains(self):
        """Every domain registered in DRAMA must be detected."""

        self.assertGreater(len(DRAMA), 0, "DRAMA catalogue is empty")

        for site in DRAMA:
            for domain in site.domains:
                with self.subTest(site=site.name, domain=domain):
                    result = detect(f"https://www.{domain}/series/1")

                    self.assertIsNotNone(
                        result,
                        f"Could not detect {domain}"
                    )

                    self.assertEqual(result.name, site.name)

    def test_removed_drama_sites_not_supported(self):
        self.assertIsNone(detect("https://www.dramaboxdb.com/x"))
        self.assertIsNone(detect("https://www.reelshort.com/x"))

    def test_unknown_and_direct(self):
        self.assertIsNone(detect("https://example.com/page"))
        self.assertIsNone(detect("not a url"))
        self.assertIsNone(detect("https://notyoutube.com/x"))

        for url in ("https://example.com/a.mp4",
                    "https://example.com/pic.jpg",
                    "https://example.com/song.mp3"):
            with self.subTest(url=url):
                result = detect(url)

                self.assertIsNotNone(result, url)
                self.assertEqual(result.category, "direct")
# ============================================================
# ORGANIZER TESTS
# ============================================================

class Organizer(unittest.TestCase):

    def test_paths(self):
        drama = Item(
            "u",
            "My Show",
            "KHFullHD",
            "drama",
            series="Cool: Drama?",
            episode=3
        )

        self.assertEqual(
            target_dir("/r", drama),
            Path("/r/KHFullHD/Cool_ Drama_")
        )

        self.assertEqual(base_name(drama), "Episode_03")

        other = Item(
            "u",
            "t",
            "KissKH",
            "drama",
            series="",
            episode=0
        )

        self.assertEqual(
            target_dir("/r", other),
            Path("/r/KissKH/Unknown_Series")
        )


# ============================================================
# SUBTITLE TESTS
# ============================================================

class Subs(unittest.TestCase):

    def test_roundtrip(self):
        cues = [
            S.Cue(1, 4.5, "Hello", "សួស្តី"),
            S.Cue(5, 8.25, "How are you?", "តើអ្នកសុខសប្បាយទេ?")
        ]

        srt = S.to_srt(cues)
        back = S.parse_srt(srt)

        self.assertEqual(
            [(c.start, c.end, c.text) for c in back],
            [
                (1, 4.5, "សួស្តី"),
                (5, 8.25, "តើអ្នកសុខសប្បាយទេ?")
            ]
        )

        vtt = S.parse_vtt(S.to_vtt(cues, "text"))

        self.assertEqual(vtt[1].text, "How are you?")
        self.assertEqual(vtt[1].end, 8.25)


# ============================================================
# VIDEO SEGMENT PLANNING TESTS
# ============================================================

class Plan(unittest.TestCase):

    def test_fixed(self):
        segs = plan_segments(600, "fixed", 20)

        self.assertEqual(len(segs), 30)
        self.assertTrue(all(length == 20 for _, length in segs))

    def test_random_within_range_and_covers(self):
        segs = plan_segments(
            600,
            "random",
            20,
            35,
            random.Random(1)
        )

        self.assertAlmostEqual(
            sum(length for _, length in segs),
            600,
            places=1
        )

        self.assertTrue(
            all(length <= 35.001 for _, length in segs)
        )

        self.assertTrue(
            all(length >= 20 for _, length in segs[:-1])
        )


# ============================================================
# MEDIA TESTS
# Requires FFmpeg
# ============================================================

@unittest.skipUnless(find_ffmpeg(), "ffmpeg missing")
class Media(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.video = cls.tmp / "in.mp4"

        subprocess.run(
            [
                find_ffmpeg(),
                "-y",
                "-f", "lavfi",
                "-i", "testsrc=s=320x240:r=25:d=12",
                "-f", "lavfi",
                "-i", "sine=f=440:d=12",
                "-c:v", "libx264",
                "-c:a", "aac",
                "-shortest",
                str(cls.video)
            ],
            capture_output=True,
            check=True
        )

    def test_probe(self):
        info = probe(self.video)

        self.assertEqual(
            (info["width"], info["height"]),
            (320, 240)
        )

        self.assertAlmostEqual(
            info["duration"],
            12,
            delta=0.5
        )

        self.assertAlmostEqual(
            info["fps"],
            25,
            delta=0.1
        )

    def test_convert(self):
        progress = []

        out = convert_video(
            self.video,
            self.tmp / "o",
            "mp4",
            height=120,
            fps=10,
            progress=progress.append
        )

        info = probe(out)

        self.assertEqual(info["height"], 120)
        self.assertAlmostEqual(info["fps"], 10, delta=0.5)
        self.assertEqual(progress[-1], 100)

        # Must not overwrite the existing output.
        out2 = convert_video(
            self.video,
            self.tmp / "o",
            "mp4",
            height=120
        )

        self.assertNotEqual(out, out2)

        # Hardware mode should fall back to CPU if unavailable.
        out3 = convert_video(
            self.video,
            self.tmp / "o",
            "mp4",
            hw=True
        )

        self.assertTrue(Path(out3).exists())

    def test_split(self):
        outs = split_video(
            self.video,
            self.tmp,
            "fixed",
            5
        )

        self.assertEqual(len(outs), 3)
        self.assertEqual(Path(outs[0]).parent.name, "short")

        self.assertAlmostEqual(
            probe(outs[0])["duration"],
            5,
            delta=0.5
        )

    def test_frames(self):
        outs = extract_frames(
            self.video,
            self.tmp,
            count=4
        )

        self.assertEqual(len(outs), 4)

        self.assertTrue(
            all(Path(out).stat().st_size > 0 for out in outs)
        )

    def test_image(self):
        frame = extract_frames(
            self.video,
            self.tmp,
            count=1
        )[0]

        out = convert_image(
            Path(frame),
            self.tmp / "img",
            "webp",
            100
        )

        self.assertTrue(out.endswith(".webp"))
        self.assertTrue(Path(out).exists())

    def test_dub_pipeline(self):
        from app.translator.voice_synchronizer import build_voice_track
        from app.translator.exporter import export_video, export_audio

        a1 = self.tmp / "a1.mp3"
        a2 = self.tmp / "a2.mp3"

        # a2 is 6 seconds but its subtitle slot is 2 seconds.
        for file_path, duration in ((a1, 2), (a2, 6)):
            subprocess.run(
                [
                    find_ffmpeg(),
                    "-y",
                    "-f", "lavfi",
                    "-i", f"sine=f=600:d={duration}",
                    str(file_path)
                ],
                capture_output=True,
                check=True
            )

        cues = [
            S.Cue(1, 3.5, "a", "ក", str(a1)),
            S.Cue(5, 7, "b", "ខ", str(a2))
        ]

        wav = self.tmp / "voice.wav"

        over = build_voice_track(cues, 8, wav)

        # Only 1 second of free gap after cue 2 -> overrun.
        self.assertEqual(over, [1])

        self.assertAlmostEqual(
            probe(wav)["duration"],
            9,
            delta=0.6
        )

        mp3 = export_audio(
            wav,
            self.tmp / "v.mp3"
        )

        self.assertTrue(Path(mp3).exists())

        export_options = [
            {
                "voice_wav": wav,
                "original_volume": 0.0,
                "subs": "soft"
            },
            {
                "voice_wav": wav,
                "original_volume": 0.2,
                "subs": "soft"
            },
            {
                "voice_wav": None,
                "subs": "soft"
            },
            {
                "voice_wav": wav,
                "subs": "burn"
            }
        ]

        for options in export_options:
            out = export_video(
                self.video,
                self.tmp / "final.mp4",
                cues,
                **options
            )

            self.assertTrue(
                Path(out).exists()
                and Path(out).stat().st_size > 1000,
                options
            )


# ============================================================
# RUN TESTS
# ============================================================

if __name__ == "__main__":
    unittest.main()