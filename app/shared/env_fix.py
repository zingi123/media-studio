"""
Safe environment correction plan. NOTHING here changes packages by itself:
  build_plan(probe)            -> analysis + proposed steps (pure, offline)
  backup_environment(dir)      -> pip freeze + pip check saved BEFORE any change
  dry_run(step)                -> `pip install --dry-run` : proves the combination resolves for THIS Python / Windows / index
  apply(step, confirmed=True)  -> only after a successful dry run in this process AND an explicit confirmation
Optional AI packages are never touched unless they are part of the conflict.
"""
import subprocess
import sys
import time
from pathlib import Path

from . import diagnostics as DG

CPU_INDEX = "https://download.pytorch.org/whl/cpu"
_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DRY_OK = set()


def _base(v):
    return (v or "").split("+")[0]


def build_plan(probe):
    """-> dict(problems=[str], dependents=[...], options=[step], recommended=id|None). Offline analysis of the probe result."""
    if "probe_error" in probe:
        return {"problems": [probe["probe_error"]], "dependents": [], "options": [], "recommended": None}
    pk, st, inst = probe.get("packages", {}), probe.get("steps", {}), probe.get("installed", {})
    tv, tav = pk.get("torch"), pk.get("torchaudio")
    problems, options = [], []
    req_conf = {}
    for root in ("pyannote.audio", "torchaudio", "openai-whisper", "faster-whisper", "speechbrain"):
        c = DG.conflicts((probe.get("requires") or {}).get(root), inst)
        if c:
            req_conf[root] = c
            for x in c:
                problems.append(f"{root} declares '{x['requirement']}' but {x['name']} {x['installed']} is installed")
    ta = st.get("torchaudio", {})
    if tav and not ta.get("ok"):
        problems.append(f"torchaudio {tav} fails its runtime test: {ta.get('error', 'torch does not import')}")
    tm, am = DG._minor(tv), DG._minor(tav)
    if tm and am and tm != am:
        problems.append(f"torch {tv} and torchaudio {tav} are different minor versions (torchaudio is compiled for one torch version)")
    if not problems:
        return {"problems": [], "dependents": [], "options": [], "recommended": None}

    # Option A - keep torch, make torchaudio match it
    if tv and tav and tm != am:
        tgt = {"torch": _base(tv), "torchaudio": _base(tv)}
        options.append(_option("A", f"Keep torch {tv}; install the torchaudio built for it",
                               f"torchaudio=={_base(tv)}", tgt, probe, "Smallest change: only torchaudio is replaced. Pyannote's own pin may still conflict (see problems)."))
    # Option B - satisfy pyannote's declared torch / torchaudio pins
    pins = {}
    for x in req_conf.get("pyannote.audio", []):
        if _canon(x["name"]) in ("torch", "torchaudio") and x["specifier"].startswith("=="):
            pins[_canon(x["name"])] = x["specifier"][2:]
    for x in DG.check_requirements((probe.get("requires") or {}).get("pyannote.audio"), inst):
        if _canon(x["name"]) in ("torch", "torchaudio") and x["specifier"].startswith("==") and _canon(x["name"]) not in pins:
            pins[_canon(x["name"])] = x["specifier"][2:]
    if pins:
        t = pins.get("torch") or pins.get("torchaudio")
        tgt = {"torch": pins.get("torch", t), "torchaudio": pins.get("torchaudio", t)}
        options.append(_option("B", f"Install the versions pyannote.audio {pk.get('pyannote.audio', '')} declares (torch {tgt['torch']}, torchaudio {tgt['torchaudio']})",
                               f"torch=={tgt['torch']} torchaudio=={tgt['torchaudio']}", tgt, probe,
                               "Follows pyannote's metadata exactly. Downgrades torch; other packages that need a newer torch may object (listed)."))
    elif not pins and pk.get("pyannote.audio"):
        pass
    rec = None
    for o in options:                                # recommend the first option that breaks no other installed package
        if not o["breaks"]:
            rec = o["id"]; break
    return {"problems": problems, "dependents": probe.get("dependents", []), "options": options, "recommended": rec}


def _canon(n):
    return DG._canon(n)


def _option(oid, title, pkgs, targets, probe, note):
    tg = {_canon(k): v for k, v in targets.items()}
    broken = DG.dependents_violations(probe.get("dependents", []), tg)
    return {"id": oid, "title": title, "packages": pkgs, "note": note, "breaks": broken,
            "dry_run": [sys.executable, "-m", "pip", "install", "--dry-run", "--no-input", *pkgs.split(), "--index-url", CPU_INDEX],
            "apply": [sys.executable, "-m", "pip", "install", "--no-input", *pkgs.split(), "--index-url", CPU_INDEX]}


def backup_environment(folder):
    """Save `pip freeze` and `pip check` BEFORE changing anything. -> path of the freeze file (restore: pip install -r <file>)."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    fz = folder / f"freeze_{ts}.txt"
    r = subprocess.run([sys.executable, "-m", "pip", "list", "--format=freeze"], capture_output=True, text=True, creationflags=_NOWIN)
    fz.write_text(r.stdout, encoding="utf-8")
    c = subprocess.run([sys.executable, "-m", "pip", "check"], capture_output=True, text=True, creationflags=_NOWIN)
    (folder / f"pip_check_{ts}.txt").write_text(c.stdout + c.stderr, encoding="utf-8")
    return fz


def dry_run(option, timeout=600):
    """Resolve the plan without installing. -> (ok, output tail). Marks the option as validated for apply()."""
    r = subprocess.run(option["dry_run"], capture_output=True, text=True, timeout=timeout, creationflags=_NOWIN)
    ok = r.returncode == 0
    if ok:
        _DRY_OK.add(option["id"] + option["packages"])
    return ok, ((r.stdout or "") + (r.stderr or ""))[-1500:]


def apply(option, confirmed=False, timeout=3600):
    """Install. Refuses unless dry_run() succeeded for exactly this option and the caller confirmed."""
    if not confirmed:
        raise PermissionError("Not confirmed: nothing was changed.")
    if option["id"] + option["packages"] not in _DRY_OK:
        raise PermissionError("Run and pass the dry run first: nothing was changed.")
    r = subprocess.run(option["apply"], capture_output=True, text=True, timeout=timeout, creationflags=_NOWIN)
    return r.returncode == 0, ((r.stdout or "") + (r.stderr or ""))[-2000:]


def format_plan(plan):
    if not plan["problems"]:
        return "No torch / torchaudio / pyannote compatibility problem was detected by metadata checks and runtime tests."
    L = ["PROBLEMS FOUND:"] + [f"  - {p}" for p in plan["problems"]]
    if plan["dependents"]:
        L += ["", "Installed packages that depend on torch-family packages:"]
        L += [f"  - {d['package']} {d['version']}: {d['requirement']}" for d in plan["dependents"][:20]]
    L.append("")
    if not plan["options"]:
        L.append("No automatic option could be derived (no exact version pin was found). Fix manually, then re-run the check.")
    for o in plan["options"]:
        L += [f"OPTION {o['id']}: {o['title']}" + ("   <- recommended" if plan['recommended'] == o['id'] else ""), f"  {o['note']}",
              "  Would break: " + (", ".join(f"{b['package']} ({b['requirement']})" for b in o["breaks"]) or "nothing detected"),
              "  Validate:  " + " ".join(o["dry_run"]), "  Apply:     " + " ".join(o["apply"]), ""]
    L.append("Nothing is installed until you validate (dry run) and confirm. A backup (pip freeze) is saved first.")
    return "\n".join(L)
