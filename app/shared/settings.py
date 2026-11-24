import json
from .paths import data_dir
from . import secrets as _sec

DEFAULTS = {
    "download_root": "",
    "ffmpeg_path": "",
    "cookies_file": "",
    "parallel_downloads": 2,
    "hw_accel": False,
    "gemini_key": "",
    "gemini_model": "gemini-3.8-flash",
    "whisper_model": "small",
    "tts_voice": "km-KH-SreymomNeural",
    "khmer_font_path": "",
    "hf_token": "",
    "claude_key": "", "claude_model": "claude-sonnet-5-5", "ollama_url": "http://localhost:11434", "ollama_model": "", "provider_transport": "auto",
    "ollama_exe": "",                                                                                       # optional path to ollama.exe (auto-detected when empty)
    "translate_provider": "gemini", "story_provider": "gemini",                                             # provider per task: gemini | claude | ollama
    "asr_backend": "auto", "cpu_threads": 0, "processing_workers": 1,                                       # CPU-first: 1 worker = one heavy model at a time
    "ocr_engine": "auto", "ocr_fix_auto_apply": False,                                                      # OCR vs Whisper conflicts are REVIEWED unless this is on
    "diar_mode": "pyannote", "diar_model": "pyannote/speaker-diarization-community-1",                   # pyannote | fallback (energy VAD, explicit) | off (single speaker)
    "settings_version": 4,
    "voice_min_conf": 45,
    "fx_blur_pct": 60, "fx_top": 70, "fx_h": 11, "fx_left": 5, "fx_w": 90,   # Blur: % និងតំបន់ subtitle
    "fx_sub_size": 54, "fx_sub_margin": 220,                                 # Khmer subtitle: size, edge distance
    "fx_blur_on": False, "fx_sub_outline": True, "fx_sub_pos": "Bottom",
    "fx_sub_color": "#FFFFFF", "fx_sub_font_file": "", "fx_version": 2,
    "voice_offset": 0.0, "match_rate": False, "smart_ocr_fix": False, "output_dir": "", "logo_autobg": True,
    "scan_method": "auto", "scan_lang": "zh",                                      # scan: source, language
    "merge_mode": "Auto", "merge_chapters": True,                                                      # Converter > Merge videos
    "trk_mirror": False, "trk_wm_on": False, "trk_wm_path": "", "trk_wm_size": 20, "trk_wm_opacity": 50, "trk_wm_pos": "Bottom Right",
    "voxcpm_device": "auto",                                                                             # CLONE VOICE engine: auto | cpu | cuda (GPU) | hf (Hugging Face online)
    "hf_space": "openbmb/VoxCPM-Demo", "hf_api_name": "", "hf_online_ack": False,                       # Hugging Face online: Space id, endpoint ("" = auto-detect), upload warning accepted
    "voxcpm_model": "",                                                                                  # CLONE VOICE: "" = openbmb/VoxCPM2 (auto-download) or a local model folder
    "emotion_min_pct": 70, "event_min_pct": 50, "smart_motion": True, "smart_story": True, "split_silence_s": 3.0,                                   # Smart Script: write an emotion only at/above this %; scream/cry %; scan character movement
    "emotion_tts_prosody": True,                                                                          # Emotion Raw -> speed/pitch/volume of default + preset voices (edge-tts has no emotion styles)
    "voice_nicks": {}, "voice_selected": [], "voice_settings": {},                                         # Voice Type: Nickname + បញ្ជីដែលបានជ្រើស (ជាប់ជានិច្ច)
    "translator_active_section": "media", "translator_timeline_visible": True,
    "translator_inspector_visible": True, "translator_left_width": 340,
    "translator_inspector_width": 280, "translator_bottom_height": 300,
}


class Settings:
    def __init__(self):
        self.path = data_dir() / "settings.json"
        self.data = dict(DEFAULTS)
        self._secrets = {}
        disk = {}
        if self.path.exists():
            try:
                disk = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                disk = {}
        self.data.update(disk)
        migrated = False
        for k in _sec.SECRET_KEYS:                       # keys found in the old plaintext file -> secure store
            old = str(disk.get(k, "") or "").strip()
            if old:
                _sec.set_secret(k, old)
                migrated = True
            self.data[k] = ""
        if self.data.get("fx_version", 1) < 2:  # ចាស់ → edge distance ថ្មី 220px (អាចកែបាន)
            self.data["fx_sub_margin"] = DEFAULTS["fx_sub_margin"]; self.data["fx_version"] = 2
        if disk and disk.get("settings_version", 1) < 3:   # v3: default row-split silence is 3 s (was 1.5 s)
            if float(disk.get("split_silence_s", 1.5)) == 1.5:
                self.data["split_silence_s"] = 3.0
            self.data["settings_version"] = 3
            migrated = True
        if disk and disk.get("settings_version", 1) < 4:   # v4: Model Gemini ចាស់ដែលបានរក្សាទុក -> Model ថ្មី (2.5-flash លែងមានសម្រាប់អ្នកប្រើថ្មី)
            if disk.get("gemini_model") == "gemini-2.5-flash":
                self.data["gemini_model"] = DEFAULTS["gemini_model"]
            self.data["settings_version"] = 4
            migrated = True
        if not self.data["download_root"]:
            self.data["download_root"] = str(data_dir() / "Downloads")
        if migrated:
            self.save()

    def get(self, k):
        if k in _sec.SECRET_KEYS:
            return _sec.get_secret(k)
        return self.data.get(k, DEFAULTS.get(k))

    def set(self, k, v):
        if k in _sec.SECRET_KEYS:
            _sec.set_secret(k, str(v or ""))
            return
        self.data[k] = v

    def save(self):
        out = {k: v for k, v in self.data.items() if k not in _sec.SECRET_KEYS}
        self.path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")


_instance = None


def settings() -> Settings:
    global _instance
    if _instance is None:
        _instance = Settings()
    return _instance