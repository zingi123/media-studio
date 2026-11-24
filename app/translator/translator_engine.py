"""Chinese (or other) -> spoken Khmer. Engines go through ai.providers (one interface); NLLB stays as the offline engine.
Timestamps are never touched. Engine index is kept compatible with the old UI: 0 = Gemini, 1 = NLLB (offline)."""
from ..shared.ffmpeg_util import Cancelled
from .script_builder import plain_text   # Smart Script: strips (emotion) / [pause] tags before translating
from ..ai import providers as P
from ..ai import khmer_prompt as KP

ENGINES = ["Gemini (API key, best quality)", "NLLB local (offline, lower quality)",
           "Claude (API key)", "Ollama local (needs a downloaded model)"]
ENGINE_PROVIDER = {0: "gemini", 2: "claude", 3: "ollama"}
NLLB_CODES = {"zh": "zho_Hans", "en": "eng_Latn", "ko": "kor_Hang", "ja": "jpn_Jpan", "th": "tha_Thai",
              "vi": "vie_Latn", "auto": "zho_Hans"}


def make_provider(engine=0, api_key="", model=""):
    """LLM provider for an engine index (0 Gemini, 2 Claude, 3 Ollama); None for the offline NLLB engine."""
    if engine not in ENGINE_PROVIDER:
        return None
    provider = P.get_provider(ENGINE_PROVIDER[engine])
    if engine == 0:
        provider.key = (api_key or provider.key).strip()
        provider.model = (model or provider.model).strip()
    return provider


def _llm_batch(provider, batch, ctx, glossary, characters, model=None, src_lang="auto"):
    """One provider call -> {id: khmer}. Retries once with a stricter instruction when the JSON is unusable."""
    prompt = KP.build_prompt(batch, ctx, glossary, characters)
    last = None
    for attempt in range(3):
        txt = provider.generate(prompt, system=KP.system_for(src_lang), json_mode=True, model=model, temperature=0.3)
        try:
            return KP.parse_json_array(txt)
        except (ValueError, KeyError, TypeError) as e:
            last = e
            prompt += "\nYour previous answer was not valid JSON. Return ONLY the JSON array."
    raise RuntimeError(f"{provider.label} returned an unusable response ({last}).")


def _characters_text(cues):
    ch = {}
    for c in cues:
        if c.speaker and c.speaker not in ch:
            ch[c.speaker] = c.voice_desc or c.gender or ""
    return "; ".join(f"{k}" + (f" ({v})" if v else "") for k, v in ch.items())


def translate_cues(cues, engine=0, api_key="", model="gemini-3.8-flash", src_lang="auto", glossary="",
                   batch_size=50, only_empty=True, progress=None, cancel=None, provider=None):
    """Fill cue.translation in place. engine: 0 Gemini, 1 NLLB, 2 Claude, 3 Ollama (or pass `provider`)."""
    todo = [i for i, c in enumerate(cues) if c.text.strip() and not (only_empty and c.translation.strip())]
    if not todo:
        return cues
    if provider is None and engine in ENGINE_PROVIDER:
        provider = P.get_provider(ENGINE_PROVIDER[engine])
        if engine == 0:                                   # UI passes the Gemini key/model explicitly - keep honouring them
            provider.key = (api_key or provider.key).strip()
            provider.model = (model or provider.model).strip()
    if provider is not None:
        ok, why = provider.configured()
        if not ok:
            raise RuntimeError(why + (" (free tier at aistudio.google.com)" if provider.id == "gemini" else "") +
                               " Or choose another engine.")
        chars = _characters_text(cues)
        for b in range(0, len(todo), batch_size):
            if cancel and cancel():
                raise Cancelled()
            idxs = todo[b:b + batch_size]
            batch = [{"id": i, "text": plain_text(cues[i].text), "sec": round(cues[i].end - cues[i].start, 1),
                      **({"speaker": cues[i].speaker} if cues[i].speaker else {}),
                      **({"emotion": cues[i].emotion} if cues[i].emotion and cues[i].emotion != "normal" else {})} for i in idxs]
            ctx = [plain_text(cues[i].text) for i in range(max(0, idxs[0] - 3), idxs[0])]
            try:
                got = _llm_batch(provider, batch, ctx, glossary, chars, src_lang=src_lang)
            except P.ProviderError as e:
                raise RuntimeError(f"{provider.label}: {e}") from e
            for i in idxs:
                if i in got:
                    cues[i].translation = got[i]
            if progress:
                progress(min(100, int((b + len(idxs)) * 100 / len(todo))))
        return cues
    try:
        from transformers import pipeline
    except ImportError as e:
        raise RuntimeError("transformers/torch not installed. Run: pip install -r requirements-ai.txt") from e
    tr = pipeline("translation", model="facebook/nllb-200-distilled-600M",
                  src_lang=NLLB_CODES.get(src_lang, "zho_Hans"), tgt_lang="khm_Khmr", max_length=200)
    for b in range(0, len(todo), 8):
        if cancel and cancel():
            raise Cancelled()
        idxs = todo[b:b + 8]
        out = tr([plain_text(cues[i].text) for i in idxs])
        for i, o in zip(idxs, out):
            cues[i].translation = o["translation_text"]
        if progress:
            progress(min(100, int((b + len(idxs)) * 100 / len(todo))))
    return cues


def timing_warnings(cues):
    """[(row index, warning)] for Khmer lines that are probably too long for their slot."""
    out = []
    for i, c in enumerate(cues):
        w = KP.fit_warning(c.translation, c.end - c.start)
        if w:
            out.append((i, w))
    return out
