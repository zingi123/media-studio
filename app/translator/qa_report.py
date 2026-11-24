"""Quality check run by Auto Dub BEFORE export (also usable on its own).  No Qt - testable.

Finds problems a human would otherwise discover only after listening to the whole film:
  error  - the line will be wrong / silent in the export
           * has original text but no Khmer translation
           * has Khmer text but no voice file (voice failed or was never made)
           * the "Khmer" still contains Chinese / Japanese / Korean letters
  warn   - the line is probably fine but worth a look
           * no Khmer letters at all (a name or number only?)
           * very long line (hard to fit its time slot)
           * speaker not sure / speech and burned-in subtitle disagree (not reviewed yet)
           * every timing problem of voice_synchronizer.timing_report (overlap, cut-off, speed-up)
Nothing is changed: the report only describes.
"""
import re
from dataclasses import dataclass
from pathlib import Path

_FOREIGN = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\u1100-\u11ff\uf900-\ufaff]")   # kana, CJK, Hangul
_KHMER = re.compile(r"[\u1780-\u17ff]")
LONG_LINE = 160            # characters; longer than this rarely fits a normal dialogue slot


@dataclass
class Issue:
    level: str             # "error" | "warn"
    line: int              # 1-based row number, 0 = whole film
    text: str

    def __str__(self):
        where = f"Line {self.line}: " if self.line else ""
        return f"[{self.level.upper()}] {where}{self.text}"


def check(cues, duration=None, durations=None, timing=True, cancel=None):
    """-> [Issue] sorted by line.  `durations` = {row index: voice clip seconds} (tests); timing=False skips the ffprobe pass."""
    out = []
    for i, c in enumerate(cues):
        if cancel and cancel():
            break
        n = i + 1
        tr = (c.translation or "").strip()
        src = (c.text or "").strip()
        if src and not tr:
            out.append(Issue("error", n, "no Khmer translation."))
            continue
        if not tr:
            continue
        if _FOREIGN.search(tr):
            out.append(Issue("error", n, "the Khmer text still contains Chinese / Japanese / Korean letters."))
        elif not _KHMER.search(tr):
            out.append(Issue("warn", n, "no Khmer letters (only a name, number or symbol?)."))
        if getattr(c, "use_voice", True) and not (c.audio and Path(c.audio).exists()):
            out.append(Issue("error", n, "no voice file - this line will be SILENT."))
        if len(tr) > LONG_LINE:
            out.append(Issue("warn", n, f"very long ({len(tr)} characters) - may not fit its time slot."))
        if getattr(c, "speaker_uncertain", False):
            out.append(Issue("warn", n, "speaker is uncertain (check the voice / character)."))
        ms = getattr(c, "match_score", -1.0)
        if 0 <= ms < 75:
            out.append(Issue("warn", n, f"voice performance does not follow the original well ({ms:.0f}/100: {getattr(c, 'match_note', '')})."))
        if getattr(c, "text_conflict", False):
            out.append(Issue("warn", n, "speech and burned-in subtitle disagree and were not reviewed."))
    if timing and not (cancel and cancel()):
        try:
            from .voice_synchronizer import timing_report
            for w in timing_report(cues, duration, durations=durations):
                m = re.match(r"Line (\d+)", w)
                out.append(Issue("warn", int(m.group(1)) if m else 0, re.sub(r"^Line \d+:\s*", "", w)))
        except Exception as e:  # noqa - the timing part must never break the report
            out.append(Issue("warn", 0, f"timing check skipped ({e})."))
    out.sort(key=lambda x: (x.line, x.level != "error"))
    return out


def run(cues, duration=None, progress=None, cancel=None):
    """Worker entry point (fn(*args, progress=, cancel=)).  Returns the issue list."""
    res = check(cues, duration, cancel=cancel)
    if progress:
        progress(100)
    return res


def counts(issues):
    return sum(1 for x in issues if x.level == "error"), sum(1 for x in issues if x.level == "warn")


def to_text(issues, title=""):
    e, w = counts(issues)
    head = (title + "\n" if title else "") + f"{e} error(s), {w} warning(s)\n" + "-" * 40 + "\n"
    return head + ("\n".join(str(x) for x in issues) if issues else "No problems found.") + "\n"
