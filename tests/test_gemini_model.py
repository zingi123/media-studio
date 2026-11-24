"""Tests: Gemini default model + 404 fallback + settings migration (no network, no real key)."""
import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ["MEDIASTUDIO_HOME"] = tempfile.mkdtemp()                       # ទីតាំង data សាកល្បង (មិនប៉ះ data ពិត)

from app.ai.providers import GeminiProvider, ProviderError
from app.shared import settings as ST


class Fake(GeminiProvider):
    """Pretends to be the API: only models in `avail` answer, others return a 404 'model' error."""
    def __init__(self, avail, model="gemini-2.5-flash"):
        super().__init__("k", model)
        self.avail, self.calls = avail, []

    def list_models(self):
        return sorted(self.avail)

    def _generate(self, prompt, system="", json_mode=False, model=None, temperature=0.3, timeout=0):
        m = model or self.model
        self.calls.append(m)
        if m not in self.avail:
            raise ProviderError("404 not found - wrong model name", "model")
        return f"ok:{m}"


class ModelTests(unittest.TestCase):
    def test_default_is_new_model(self):
        self.assertEqual(GeminiProvider.default_model, "gemini-3.8-flash")
        self.assertEqual(ST.DEFAULTS["gemini_model"], "gemini-3.8-flash")

    def test_fallback_to_3_8_when_old_model_gone(self):
        p = Fake({"gemini-3.8-flash"})
        self.assertEqual(p.generate("hi"), "ok:gemini-3.8-flash")
        self.assertEqual((p.calls, p.model), (["gemini-2.5-flash", "gemini-3.8-flash"], "gemini-3.8-flash"))
        p.generate("again")                                               # ចងចាំ Model -> មិន 404 ម្តងទៀត
        self.assertEqual(p.calls[-1], "gemini-3.8-flash")

    def test_prefers_3_8_then_3_6(self):
        self.assertEqual(Fake({"gemini-3.6-flash", "gemini-3.8-flash"}).generate("x"), "ok:gemini-3.8-flash")
        self.assertEqual(Fake({"gemini-3.6-flash"}).generate("x"), "ok:gemini-3.6-flash")

    def test_no_silent_swap_when_nothing_available(self):
        with self.assertRaises(ProviderError):
            Fake({"some-other-model"}).generate("x")                      # មិនប្តូរទៅ Model ដែលមិនមានក្នុងបញ្ជី

    def test_other_errors_are_not_retried(self):
        class Quota(Fake):
            def _generate(self, *a, **k):
                self.calls.append(1)
                raise ProviderError("quota", "quota")
        p = Quota({"gemini-3.8-flash"})
        with self.assertRaises(ProviderError):
            p.generate("x")
        self.assertEqual(len(p.calls), 1)


class MigrationTests(unittest.TestCase):
    def _load(self, disk):
        d = Path(tempfile.mkdtemp())
        os.environ["MEDIASTUDIO_HOME"] = str(d)
        (d / "settings.json").write_text(json.dumps(disk), encoding="utf-8")
        return ST.Settings()

    def test_saved_old_model_is_migrated(self):
        s = self._load({"gemini_model": "gemini-2.5-flash", "settings_version": 3, "tts_voice": "keep-me"})
        self.assertEqual(s.get("gemini_model"), "gemini-3.8-flash")
        self.assertEqual((s.get("tts_voice"), s.get("settings_version")), ("keep-me", 4))   # setting ផ្សេងមិនប៉ះ

    def test_custom_model_is_kept(self):
        self.assertEqual(self._load({"gemini_model": "gemini-3.6-flash", "settings_version": 3}).get("gemini_model"), "gemini-3.6-flash")



class RetryTests(unittest.TestCase):
    def test_504_is_retried_then_succeeds(self):                          # SDK path: ServerError 504 ត្រូវ retry
        from app.ai.providers import classify
        self.assertEqual(classify(504, "ServerError", "Deadline expired")[0], "server")
        p, n = GeminiProvider("k", "m"), []
        p.sleep = lambda s: None                                          # មិនរង់ចាំពិត

        def flaky():
            n.append(1)
            if len(n) < 3:
                raise ProviderError("504", "server")
            return "ok"
        self.assertEqual(p._with_retry(flaky), "ok")
        self.assertEqual(len(n), 3)

    def test_auth_error_not_retried(self):
        p, n = GeminiProvider("k", "m"), []
        p.sleep = lambda s: None

        def bad():
            n.append(1)
            raise ProviderError("auth", "auth")
        with self.assertRaises(ProviderError):
            p._with_retry(bad)
        self.assertEqual(len(n), 1)


class OverloadTests(unittest.TestCase):
    def _fake(self, avail, overloaded):
        class F(Fake):
            def _generate(self, prompt, system="", json_mode=False, model=None, temperature=0.3, timeout=0):
                m = model or self.model
                self.calls.append(m)
                if m in overloaded:
                    raise ProviderError("503 high demand", "server")
                return f"ok:{m}"
        return F(avail, model="gemini-3.8-flash")

    def test_503_falls_back_temporarily(self):                            # 503 -> Model ផ្សេងក្នុងបញ្ជី ហើយមិនចងចាំ
        p = self._fake({"gemini-3.8-flash", "gemini-3.6-flash"}, {"gemini-3.8-flash"})
        self.assertEqual(p.generate("x"), "ok:gemini-3.6-flash")
        self.assertEqual(p.model, "gemini-3.8-flash")

    def test_503_everywhere_raises(self):
        p = self._fake({"gemini-3.8-flash", "gemini-3.6-flash"}, {"gemini-3.8-flash", "gemini-3.6-flash"})
        with self.assertRaises(ProviderError):
            p.generate("x")

    def test_503_no_alternative_in_list_raises(self):
        with self.assertRaises(ProviderError):
            self._fake({"gemini-3.8-flash"}, {"gemini-3.8-flash"}).generate("x")


if __name__ == "__main__":
    unittest.main(verbosity=2)
