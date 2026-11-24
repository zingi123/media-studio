from pathlib import Path
from ..shared.ffmpeg_util import run_ffmpeg

BASE = {"P": "km-KH-PisethNeural", "S": "km-KH-SreymomNeural"}  # P=Piseth S=Sreymom

# name: (base, pitch%, speed%, formant%, effects)
# pitch = កម្រិតសំឡេង | formant = ទំហំបំពង់កសំឡេង (+ តូច/ក្មេង, - ធំ/ធ្ងន់) | speed = ល្បឿននិយាយ
# Human: ក្រុមនីមួយៗមាន 3 សំឡេងខុសគ្នា (Base P/S, pitch, speed, formant ផ្សេងគ្នា) - ជាប់ជានិច្ច កែបានតែ Nickname
_HUMAN = {
    "Boy": {"Boy 1": ("P", 35, 8, 28, ""), "Boy 2": ("P", 26, -4, 18, ""), "Boy 3": ("P", 42, 14, 34, "")},
    "Girl": {"Girl 1": ("S", 22, 8, 20, ""), "Girl 2": ("S", 12, -2, 10, ""), "Girl 3": ("S", 32, 14, 26, "")},
    "Men": {"Men 1": ("P", 0, 0, 0, ""), "Men 2": ("P", 12, 10, 6, ""), "Men 3": ("P", -6, -6, -8, "")},
    "Women": {"Women 1": ("S", 0, 0, 0, ""), "Women 2": ("S", 10, 8, 8, ""), "Women 3": ("S", -7, -7, -6, "")},
    "Adult Men": {"Adult Men 1": ("P", -14, -8, -16, ""), "Adult Men 2": ("P", -20, -14, -22, ""),
                  "Adult Men 3": ("P", -8, -12, -10, "")},
    "Adult Women": {"Adult Women 1": ("S", -10, -6, -8, ""), "Adult Women 2": ("S", -16, -10, -12, ""),
                    "Adult Women 3": ("S", -6, -16, -4, "")},
    "Old Men": {"Old Men 1": ("P", -10, -18, -6, "Light Distortion"), "Old Men 2": ("P", -18, -25, -14, "Light Distortion"),
                "Old Men 3": ("P", 3, -22, 4, "Light Distortion")},
    "Old Women": {"Old Women 1": ("S", -6, -18, -4, "Light Distortion"), "Old Women 2": ("S", -12, -24, -10, "Light Distortion"),
                  "Old Women 3": ("S", 4, -20, 6, "Light Distortion")},
    "Baby Boy": {"Baby Boy 1": ("P", 55, -5, 38, ""), "Baby Boy 2": ("P", 45, 4, 32, ""), "Baby Boy 3": ("P", 62, -10, 44, "")},
    "Baby Girl": {"Baby Girl 1": ("S", 52, -5, 35, ""), "Baby Girl 2": ("S", 60, 0, 40, ""), "Baby Girl 3": ("S", 45, -10, 30, "")},
}
_H = {v: t for g in _HUMAN.values() for v, t in g.items()}  # បញ្ជីរាបស្មើ ឈ្មោះ → preset
_F = {"God": ("P", -18, -15, -18, "Reverb, Chorus"), "Ghost": ("S", 8, -22, 10, "Whisper, Echo"),
      "Demon / Villain": ("P", -30, -15, -35, "Growl, Distortion, Reverb"), "Dragon": ("P", -38, -25, -40, "Growl, Deep Reverb"),
      "Giant": ("P", -32, -28, -35, "Bass Boost, Deep Reverb"), "Zombie": ("P", -15, -35, -10, "Distortion, Modulation"),
      "Immortal / Master": ("P", -10, -12, -8, "Reverb"), "Spirit / Fairy": ("S", 32, 5, 22, "High EQ, Echo"),
      "Robot": ("P", -5, -8, -10, "Robotic Effect"), "Alien": ("S", 18, -10, 25, "Modulation"),
      "Narrator": ("P", -10, -8, -8, "Light Compression, Bass Boost"), "News Anchor": ("S", 0, 12, 0, "Compression, Light EQ"),
      "Cute Anime": ("S", 32, 12, 22, "Light EQ"), "Grumpy Master": ("P", -18, -20, -12, "Light Distortion")}  # ទេពកោសល្យ
_A = {"Mouse / Bird": ("S", 55, 30, 40, "High EQ"), "Cat": ("S", 22, 8, 15, "Light EQ"),
      "Dog": ("P", 8, 12, -5, "Light Distortion"), "Tiger / Lion": ("P", -30, -18, -32, "Growl, Bass Boost"),
      "Bear": ("P", -34, -22, -32, "Bass Boost, Light Distortion"), "Snake": ("S", -8, -20, -5, "Whisper, Modulation")}  # សត្វ

CATALOG = {"Human & Fantasy": {"Human": _H, "Fantasy": _F}, "Animal": {"": _A}}  # ប្រភេទ → ក្រុមរង → preset
GROUPS = {"Human": _HUMAN}  # ក្រុមរង → ក្រុម (Boy → Boy 1/2/3) : ជំហានបន្ថែមក្នុង dialog
LIM = (-90, 100)  # ជួរ pitch/speed/formant (-100 ធ្វើឱ្យ crash)
# ឈ្មោះចាស់ (ពី AI detect / project ចាស់) → ឈ្មោះថ្មី
LEGACY = {"Boy": "Boy 1", "Girl": "Girl 1", "Man": "Men 1", "Woman": "Women 1", "Adult Man": "Adult Men 1",
          "Adult Woman": "Adult Women 1", "Adult Girl": "Adult Women 1", "Middle Age": "Men 3", "Old Man": "Old Men 1",
          "Old Woman": "Old Women 1", "Baby / Toddler": "Baby Girl 1",
          **{f"{o} {i}": f"{n} {i}" for o, n in (("Man", "Men"), ("Woman", "Women"), ("Adult Man", "Adult Men"),
                                                  ("Adult Woman", "Adult Women"), ("Adult Girl", "Adult Women"),
                                                  ("Old Man", "Old Men"), ("Old Woman", "Old Women")) for i in (1, 2, 3)}}


def resolve(name):  # ឈ្មោះចាស់ → ឈ្មោះថ្មី (ឈ្មោះផ្សេងមិនប្តូរ)
    return LEGACY.get(name, name)


def display_name(name, preset=None):  # ឈ្មោះបង្ហាញ: "Boy 1 (Sophea)" បើមាន Nickname
    nick = ((preset or {}).get("nick") or "").strip()
    return f"{name} ({nick})" if nick else name


SETTING_KEYS = ("base", "pitch", "speed", "formant", "fx")  # តម្លៃដែលប៉ះពាល់សំឡេង (មិនរួម nick)


def same_sound(a, b):  # preset ពីរស្តាប់ដូចគ្នាឬទេ (មិនគិត Nickname)
    return bool(a) and bool(b) and all(a.get(k) == b.get(k) for k in SETTING_KEYS)


def _clean(p):  # preset ពេញលេញ → dict សម្អាតរួច (ជួរត្រឹមត្រូវ) ឬ None បើខូច
    try:
        q = {k: max(LIM[0], min(LIM[1], int(p[k]))) for k in ("pitch", "speed", "formant")}  # កំណត់ជួរ
        if p["base"] not in BASE:  # base ត្រូវតែ P ឬ S
            return None
        q.update(base=p["base"], fx=str(p.get("fx", "")).strip())  # base + effects
        return q
    except (KeyError, TypeError, ValueError):
        return None  # ខូច


def normalize_voices(voices, nicks=None):  # project ចាស់/ដៃកែ → ឈ្មោះថ្មី + រក្សា setting ដែលបានកែ (ខូច → តម្លៃដើម)
    out = {}
    for n, p in (voices or {}).items():
        n2, p = resolve(n), (p if isinstance(p, dict) else {})  # ឈ្មោះថ្មី
        d = default_preset(n2)  # តម្លៃដើមពី catalog
        q = _clean(p) or (dict(d) if d else None)  # តម្លៃដែលបានកែ ឬតម្លៃដើម
        if not q:
            continue  # មិនស្គាល់ + ខូច → រំលង
        q["nick"] = str(p.get("nick") or (nicks or {}).get(n2, "")).strip()[:30]  # Nickname
        out[n2] = q
    return out


def default_preset(name):  # ទាញ preset ដើមតាមឈ្មោះ
    name = resolve(name)  # ទទួលឈ្មោះចាស់ផង
    for subs in CATALOG.values():  # រត់ប្រភេទ
        for items in subs.values():  # រត់ក្រុមរង
            if name in items:  # រកឃើញ
                b, p, s, f, fx = items[name]  # ចែកតម្លៃ
                return {"base": b, "pitch": p, "speed": s, "formant": f, "fx": fx, "nick": ""}  # dict អាចកែបាន
    return None  # មិនមាន


# ffmpeg approximations (ffmpeg មិនមាន reverb ពិត) - ត្រូវស្តាប់ដើម្បីវាយតម្លៃ
FX = {"reverb": "aecho=0.8:0.88:60|120:0.4|0.25", "deep reverb": "aecho=0.8:0.9:120|240:0.5|0.3",
      "echo": "aecho=0.8:0.9:400:0.35", "distortion": "asoftclip=type=tanh:param=3", "light distortion": "asoftclip=type=tanh:param=1.5",
      "robotic effect": "afftfilt=real='hypot(re,im)':imag='0':win_size=512:overlap=0.75",
      "modulation": "tremolo=f=6:d=0.5", "compression": "acompressor=threshold=0.1:ratio=4",
      "light compression": "acompressor=threshold=0.2:ratio=2", "light eq": "equalizer=f=3000:t=q:w=1:g=3",
      "high eq": "highpass=f=400,equalizer=f=4000:t=q:w=1:g=4", "growl": "tremolo=f=30:d=0.4",
      "whisper": "highpass=f=500,volume=0.8",
      "bass boost": "equalizer=f=120:t=q:w=1:g=5", "chorus": "chorus=0.6:0.9:55|65:0.4|0.32:0.25|0.4:2|2.3"}  # ឈ្មោះ effect → filter


def _chain(p):  # បង្កើត filter chain ពី preset
    P, F = 1 + p["pitch"] / 100, 1 + p["formant"] / 100  # ratio pitch និង formant
    f = []  # បញ្ជី filter
    if abs(P - 1) > 1e-6 or abs(F - 1) > 1e-6:  # បើមានប្តូរ
        f.append(f"asetrate=24000*{F:.4f},aresample=24000,rubberband=tempo={1 / F:.4f}:pitch={P / F:.4f}:formant=preserved")  # formant ដាច់ពី pitch
    for e in [x.strip().lower() for x in p["fx"].split(",") if x.strip()]:  # effect ម្តងមួយ
        if e in FX:  # ស្គាល់
            f.append(FX[e])  # បន្ថែម
    return ",".join(f)  # ភ្ជាប់ជាខ្សែ


def process(src, dst, preset, cancel=None):  # អនុវត្ត pitch/formant/effects លើ TTS
    ch = _chain(preset)  # filter chain
    if not ch:  # គ្មានអ្វីត្រូវធ្វើ
        return str(src)  # ប្រើឯកសារដើម
    Path(dst).parent.mkdir(parents=True, exist_ok=True)  # ត្រូវមានថត
    run_ffmpeg(["-y", "-i", src, "-af", ch, "-ar", "24000", "-ac", "1", dst], cancel=cancel)  # រត់ ffmpeg
    return str(dst)  # ផ្លូវលទ្ធផល


def load_saved(st):  # settings → {ឈ្មោះ: preset} (setting ដែលបានកែ + Nickname)
    nicks, sets = st.get("voice_nicks") or {}, st.get("voice_settings") or {}  # Nickname + setting ដែលបានរក្សា
    raw = {n: {**(sets.get(resolve(n)) or {}), "nick": nicks.get(resolve(n), "")} for n in (st.get("voice_selected") or [])}
    return normalize_voices(raw, nicks)


def save_saved(st, selected):  # រក្សា បញ្ជី + Nickname + setting ទាំងអស់ទៅ settings.json
    nicks = dict(st.get("voice_nicks") or {})
    for n, p in selected.items():
        nick = str(p.get("nick", "")).strip()[:30]
        if nick:
            nicks[n] = nick
        else:
            nicks.pop(n, None)
    st.set("voice_nicks", nicks); st.set("voice_selected", list(selected))
    st.set("voice_settings", {n: {k: p[k] for k in SETTING_KEYS} for n, p in selected.items()}); st.save()  # setting ដែលកែបាន


DEMO_TEXT = "សួស្តី ខ្ញុំជាសំឡេងសាកល្បង។ តើអ្នកស្តាប់ឮច្បាស់ទេ?"  # ប្រយោគសាកល្បង


def demo_file(preset, text=DEMO_TEXT, name="demo", progress=None, cancel=None):  # បង្កើតសំឡេងសាកល្បង (cache តាមតម្លៃ)
    import hashlib, re
    from ..shared.paths import work_dir
    from .khmer_tts import synthesize
    text = (text or DEMO_TEXT).strip()
    key = hashlib.md5(repr([preset[k] for k in SETTING_KEYS] + [text]).encode("utf-8")).hexdigest()[:10]
    stem = work_dir("voice_demo") / (re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") + "_" + key)
    wav, raw = stem.with_suffix(".wav"), stem.with_suffix(".mp3")
    if wav.exists() and wav.stat().st_size > 1000:
        return str(wav)  # ធ្លាប់បង្កើតរួច → លេងភ្លាម
    if not (raw.exists() and raw.stat().st_size > 0):
        synthesize(text, raw, BASE[preset["base"]], 1 + preset["speed"] / 100)  # edge-tts (ត្រូវការ internet)
    return process(str(raw), wav, preset, cancel=cancel)  # pitch/formant/effects


def canon(name):  # ឈ្មោះ Voice Type ស្តង់ដារ (មិនប្រកាន់ អក្សរធំ/តូច) ឬ None
    low = {n.lower(): n for s in CATALOG.values() for items in s.values() for n in items}  # តារាងឈ្មោះ
    low.update({k.lower(): v for k, v in LEGACY.items()})  # ឈ្មោះចាស់
    return low.get((name or "").strip().lower())  # រកឃើញឬអត់
