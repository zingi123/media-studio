"""Tests: web link import logic + SummaryController integration + old features still work. Run: python -m unittest -v"""
import json                                                           # សម្រាប់ fake AI provider
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.downloader.models import Item, Job
from app.summary import web_import as WI
from app.summary.controller import SummaryController

FF = shutil.which("ffmpeg")


def make_video(path, seconds=6):
    """Real test video: 3 coloured blocks + sine audio, so scene detection has cuts to find."""
    src = ["-f", "lavfi", "-i", f"color=c=red:s=160x90:d={seconds/3}:r=10",
           "-f", "lavfi", "-i", f"color=c=blue:s=160x90:d={seconds/3}:r=10",
           "-f", "lavfi", "-i", f"color=c=green:s=160x90:d={seconds/3}:r=10",
           "-f", "lavfi", "-i", f"sine=d={seconds}"]
    subprocess.run([FF, "-y", "-loglevel", "error", *src, "-filter_complex", "[0][1][2]concat=n=3:v=1:a=0[v]",
                    "-map", "[v]", "-map", "3:a", "-shortest", "-pix_fmt", "yuv420p", str(path)], check=True)


def item(url="https://example.com/watch/1", title="Test Movie", **kw):
    return Item(url=url, title=title, site="Web", category="social", **kw)


class FakeProvider:
    def generate(self, prompt, **kw):                                 # ឆ្លើយតាម beats ក្នុង prompt
        rows = json.loads(prompt.split("Beats:\n", 1)[1])
        return json.dumps([{"id": r["id"], "km": "នេះជាអត្ថបទសាកល្បង"} for r in rows], ensure_ascii=False)


class CheckLinkTests(unittest.TestCase):
    def test_invalid_links(self):
        for bad in ("", "   ", "ftp://x.com/a", "https://localhost/a", "not a url"):
            self.assertEqual(WI.check_link(bad, scan=lambda u: [item()]).status, WI.INVALID, bad)

    def test_adds_https(self):
        r = WI.check_link("example.com/v", scan=lambda u: [item()])
        self.assertEqual((r.status, r.url), (WI.OK, "https://example.com/v"))

    def test_single_video_ok_and_not_overclaimed(self):
        r = WI.check_link("https://example.com/v", scan=lambda u: [item()])
        self.assertTrue(r.usable)
        self.assertIn("not guaranteed", r.message)
        self.assertIn("not verified", r.message)                      # untested site -> មិនអះអាង

    def test_episode_list(self):
        r = WI.check_link("https://example.com/series", scan=lambda u: [item(url=f"https://e.com/{i}", episode=i) for i in (1, 2, 3)])
        self.assertEqual((r.status, len(r.items)), (WI.EPISODES, 3))

    def test_images_are_ignored(self):
        r = WI.check_link("https://example.com/p", scan=lambda u: [item(kind="image")])
        self.assertEqual(r.status, WI.ERROR)
        self.assertFalse(r.usable)

    def test_errors_become_friendly_messages(self):
        cases = {"This video is DRM protected": "DRM", "HTTP Error 403: Forbidden": "403",
                 "Sign in to confirm": "logged-in", "Unsupported URL: x": "not supported"}
        for raw, word in cases.items():
            def boom(u, raw=raw):
                raise RuntimeError(raw)
            r = WI.check_link("https://example.com/v", scan=boom)
            self.assertEqual(r.status, WI.ERROR)
            self.assertIn(word, r.message)
            self.assertIn("Import Video", r.message)                  # ផ្តល់ជម្រើស Import Video

    def test_known_sites_are_detected_but_untested(self):
        for url, name in (("https://kisskh.org/Drama/X", "KiSSKH"), ("https://khfullhd.net/x", "KHFullHD")):
            r = WI.check_link(url, scan=lambda u: [item()])
            self.assertEqual((r.site, r.site_status), (name, "untested"))

    def test_direct_link_needs_no_network(self):
        r = WI.check_link("https://cdn.example.com/movie.mp4")        # DirectAdapter: ដោយផ្អែកលើ extension
        self.assertEqual((r.status, r.site_status), (WI.OK, "works"))


class JobAndFolderTests(unittest.TestCase):
    def test_source_folder_layout(self):
        d = WI.source_dir_for("/data/summary", item())
        self.assertEqual(d.name, "source")
        self.assertRegex(d.parent.name, r"^Test Movie_[0-9a-f]{8}$")
        self.assertEqual(d.parent.parent, Path("/data/summary"))

    def test_same_url_same_folder_different_url_different_folder(self):
        a, b = item(), item(url="https://example.com/watch/2")
        self.assertEqual(WI.work_dir_for("/r", a), WI.work_dir_for("/r", item()))
        self.assertNotEqual(WI.work_dir_for("/r", a), WI.work_dir_for("/r", b))

    def test_job_reuses_download_manager_job(self):
        j = WI.make_job(item(), "/r")
        self.assertIsInstance(j, Job)
        self.assertTrue(j.flat)                                       # flat -> ដាក់ត្រង់ក្នុង source/
        self.assertEqual(Path(j.root), WI.source_dir_for("/r", item()))

    def test_finished_path(self):
        j = WI.make_job(item(), "/r")
        j.status, j.path = "Done", "/x/a.mp4"
        self.assertEqual(WI.finished_path(j), "/x/a.mp4")
        j.status = "Duplicate"
        self.assertEqual(WI.finished_path(j), "/x/a.mp4")
        j.status, j.error = "Failed", "boom"
        with self.assertRaisesRegex(ValueError, "boom"):
            WI.finished_path(j)
        j.status, j.error = "Cancelled", ""
        with self.assertRaisesRegex(ValueError, "cancelled"):
            WI.finished_path(j)


@unittest.skipUnless(FF, "ffmpeg is required")
class VideoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.video = self.tmp / "movie.mp4"
        make_video(self.video)

    def test_validate_accepts_real_video_rejects_others(self):
        self.assertGreater(WI.validate_video(self.video)["duration"], 4)
        html = self.tmp / "page.mp4"
        html.write_text("<html>not a video</html>")
        with self.assertRaises(ValueError):
            WI.validate_video(html)
        empty = self.tmp / "empty.mp4"
        empty.write_bytes(b"")
        with self.assertRaises(ValueError):
            WI.validate_video(empty)
        with self.assertRaises(ValueError):
            WI.validate_video(self.tmp / "missing.mp4")

    def test_local_import_legacy_call_unchanged(self):                # Import Video: open_video(path)
        c = SummaryController(self.tmp / "summary")
        p = c.open_video(self.video)
        self.assertEqual(Path(p.source), self.video.resolve())
        self.assertTrue(c.work.parent == self.tmp / "summary" and (c.work / "project.json").exists())

    def test_web_import_into_source_folder(self):                     # Import from Web Link: download -> source/ -> open_video
        c = SummaryController(self.tmp / "summary")
        it = item()
        src = WI.source_dir_for(c.root, it)
        src.mkdir(parents=True)
        dl = src / "Test Movie.mp4"
        shutil.copy(self.video, dl)                                   # តំណាងឱ្យឯកសារដែល Download Manager បានទាញ
        WI.validate_video(dl)
        p = c.open_video(dl, work=WI.work_dir_for(c.root, it))
        self.assertEqual(c.work, WI.work_dir_for(c.root, it))
        self.assertEqual(Path(p.source), dl.resolve())
        self.assertTrue((c.work / "project.json").exists())
        self.assertEqual(dl.parent, c.work / "source")
        c2 = SummaryController(self.tmp / "summary")                  # បើកម្តងទៀត -> project ដដែល
        self.assertEqual(c2.open_video(dl, work=WI.work_dir_for(c.root, it)).duration, p.duration)

    def test_old_features_scene_detection_beats_script(self):         # regression: មុខងារចាស់មិនខូច
        c = SummaryController(self.tmp / "summary")
        c.open_video(self.video)
        scenes = c.analyse(threshold=0.3, min_len=0.5, max_len=30.0)
        self.assertGreaterEqual(len(scenes), 2)
        beats = c.set_beats_text("The hero starts\nThe hero finds a letter\nThe hero leaves")
        self.assertEqual(len(beats), 3)
        segs = c.make_script(FakeProvider(), 30, "Neutral")
        self.assertEqual(len(segs), 3)
        self.assertTrue(all(s.km for s in segs))


if __name__ == "__main__":
    unittest.main(verbosity=2)
