"""Environment diagnostic - the same engine as the in-app 'System Status' panel.

  python verify_env.py                 local checks (no network, no API tokens)
  python verify_env.py --online        also test Hugging Face / Gemini / Claude / Ollama with real requests
  python verify_env.py --audio FILE    run Whisper + Pyannote on the first 60 s of a real audio/video file
  python verify_env.py --report FILE   save the full report (never contains keys)
  python verify_env.py --plan          show the torch/torchaudio/pyannote compatibility plan (changes nothing)
  python verify_env.py --fix           back up the environment, validate the plan with a dry run, apply ONLY after you type YES
Exit code 0 = the core app can run (optional parts may be missing)."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--online", action="store_true")
    ap.add_argument("--audio")
    ap.add_argument("--report")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--fix", action="store_true")
    a = ap.parse_args()
    try:
        from app.shared import diagnostics as DG, env_fix as EF
        from app.shared.settings import settings
    except Exception as e:  # noqa
        print(f"The MediaStudio package could not be imported: {e}\nRun this from the MediaStudio folder with the project's Python (.venv311 / .venv).")
        return 2
    st = settings()
    if a.online:
        for pid in ("huggingface", "gemini", "claude", "ollama"):
            try:
                from app.ai.providers import get_provider
                p = get_provider(pid, st)
                if pid != "ollama" and not p.configured()[0]:
                    print(f"[skip] {pid}: no key/token saved"); continue
                print(f"[test] {pid} ...", flush=True)
                DG.run_provider_tests(pid, st)
            except Exception as e:  # noqa
                print(f"[fail] {pid}: {e}")
    if a.audio:
        print("Running Whisper + Pyannote on the first 60 s (CPU can take several minutes; first run downloads models)...", flush=True)
        r = DG.run_audio_test(a.audio, st.get("hf_token"), st.get("diar_model") or None, "cpu", st.get("whisper_model") or "base", seconds=60, settings=st)
        print("audio test:", "OK" if r["ok"] else "FAILED", r.get("whisper", ""), r.get("diarization", ""), r.get("errors", ""))
    print("Probing the environment (PyTorch/Pyannote are imported in a separate process; this can take a minute)...", flush=True)
    comps, probe = DG.collect(st)
    w = max(len(c.name) for c in comps)
    for c in comps:
        print(f"{c.name:<{w}}  {c.status:<20} {c.summary}")
    if a.report:
        Path(a.report).write_text(DG.format_report(comps, probe), encoding="utf-8")
        print(f"\nFull report saved to {a.report}")
    core = {c.name: c for c in comps}
    core_bad = any(core[n].status in (DG.NOT_INSTALLED, DG.COMPAT) for n in ("FFmpeg",)) or "probe_error" in probe
    if a.plan or a.fix:
        plan = EF.build_plan(probe)
        print("\n" + EF.format_plan(plan))
        if a.fix and plan["options"]:
            ids = [o["id"] for o in plan["options"]]
            pick = input(f"\nWhich option to validate? {ids} (Enter = cancel): ").strip().upper()
            opt = next((o for o in plan["options"] if o["id"] == pick), None)
            if opt:
                fz = EF.backup_environment(Path.home() / "MediaStudio" / "env_backup")
                print(f"Backup saved: {fz}   (restore with: python -m pip install -r \"{fz}\")")
                ok, out = EF.dry_run(opt)
                print(out[-800:])
                if not ok:
                    print("The dry run FAILED - nothing was installed. This combination is not available for this Python / platform."); return 1
                if input("Dry run passed. Type YES to install: ").strip() == "YES":
                    ok, out = EF.apply(opt, confirmed=True)
                    print(out[-1000:]); print("Done. Run  python verify_env.py  again.")
                else:
                    print("Cancelled - nothing was changed.")
    print("\nCore app:", "READY (optional parts may be missing - see the table)" if not core_bad else "NOT READY - fix FFmpeg / the Python environment first")
    return 1 if core_bad else 0


if __name__ == "__main__":
    sys.exit(main())
