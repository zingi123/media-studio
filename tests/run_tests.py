"""Dependency-free test runner:  python tests/run_tests.py   (pytest also works: pytest tests)"""
import importlib, inspect, pathlib, sys, traceback
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import conftest  # noqa  sets sys.path + isolated MEDIASTUDIO_HOME
ok = bad = skip = 0
for f in sorted(pathlib.Path(__file__).parent.glob("test_*.py")):
    mod = importlib.import_module(f.stem)
    for name, fn in inspect.getmembers(mod, inspect.isfunction):
        if not name.startswith("test_") or fn.__module__ != mod.__name__:
            continue
        try:
            fn(); ok += 1; print(f"PASS  {f.stem}::{name}")
        except Exception as e:  # noqa
            if type(e).__name__ == "SkipTest":
                skip += 1; print(f"SKIP  {f.stem}::{name}: {e}")
            else:
                bad += 1; print(f"FAIL  {f.stem}::{name}"); traceback.print_exc()
print(f"\n{ok} passed, {bad} failed, {skip} skipped")
sys.exit(1 if bad else 0)
