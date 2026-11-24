"""
Character movement from the VIDEO PICTURE (used by Smart Script, next to the voice analysis).

For every script line (its start-end time) the frames of that time are looked at and a short, simple description
of what the character does is written, e.g.   "face on screen, mouth moving, big movement, hand raised".

What is measured (OpenCV only; hand/pose marks need the optional `mediapipe`):
  * movement size      - how much of the picture changes between sampled frames (scene cuts are ignored)
  * face on screen     - Haar face detector; how many faces; biggest face = "the character"
  * mouth moving       - the mouth area of the biggest face changes -> that face is probably the one talking.
                         Face visible but mouth still -> the voice may come from someone else / off screen.
  * head / body moves, moves closer / farther  - the biggest face changes place or size
  * hand raised, hand near face (optional mediapipe pose)

Honest limits: it does NOT know WHICH person is which speaker when several are on screen - it describes the biggest face.
Haar faces miss turned / small / covered faces. Thresholds are simple and can be tuned below.
"""
import numpy as np
from ..shared.ffmpeg_util import Cancelled

FPS = 3.0            # frames sampled per second (lower = faster)
WIDTH = 320          # frames are shrunk to this width first (fast)
PIX = 25             # gray-level change that counts as "this pixel moved"
CUT = 0.5            # more than this share of the picture changed at once = scene cut (ignored)
STILL, SMALL = 0.01, 0.04      # movement share: below STILL = standing still, below SMALL = small movement
MOUTH = 0.06         # share of mouth pixels that change = mouth moving
_FACE = {}


def _face_detector():
    if "f" not in _FACE:
        try:
            import cv2
            d = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
            _FACE["f"] = None if d.empty() else d          # file missing in some OpenCV builds -> no face marks, movement still works
        except Exception:
            _FACE["f"] = None
    return _FACE["f"]


def _pose():
    if "p" not in _FACE:
        try:
            import mediapipe as mp
            _FACE["p"] = mp.solutions.pose.Pose(static_image_mode=True, model_complexity=0)
        except Exception:
            _FACE["p"] = None
    return _FACE["p"]


def _pose_flags(pose, bgr):
    """-> set of 'hand raised' / 'hand near face' from MediaPipe landmarks (normalised 0..1, y grows downward)."""
    import cv2
    r = pose.process(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
    if not r.pose_landmarks:
        return set()
    L = r.pose_landmarks.landmark
    nose, sh = L[0], (L[11], L[12])
    out = set()
    for wr in (L[15], L[16]):
        if wr.visibility < 0.5:
            continue
        if wr.y < min(s.y for s in sh) - 0.02:
            out.add("hand raised")
        if ((wr.x - nose.x) ** 2 + (wr.y - nose.y) ** 2) ** 0.5 < 0.15:
            out.add("hand near face")
    return out


def sample_frames(video, fps=FPS, progress=None, cancel=None, use_pose=True):
    """One sequential pass over the video -> list of samples
    {t, move, faces, mouth, cx, cy, area, pose}  (move=None on a scene cut)."""
    import cv2
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise RuntimeError("Could not open the video for movement analysis.")
    src = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    step = max(1, int(round(src / fps)))
    det, pose = _face_detector(), (_pose() if use_pose else None)
    out, prev, i = [], None, 0
    try:
        while True:
            if cancel and cancel():
                raise Cancelled()
            if not cap.grab():
                break
            if i % step == 0:
                ok, fr = cap.retrieve()
                if ok:
                    h, w = fr.shape[:2]
                    sm = cv2.resize(fr, (WIDTH, max(1, int(h * WIDTH / w))))
                    g = cv2.GaussianBlur(cv2.cvtColor(sm, cv2.COLOR_BGR2GRAY), (5, 5), 0)
                    diff = cv2.absdiff(g, prev) > PIX if prev is not None else None
                    move = None if diff is None else float(diff.mean())
                    if move is not None and move > CUT:
                        move = None                                       # scene cut: not a character movement
                    try:
                        faces = det.detectMultiScale(g, 1.2, 5, minSize=(24, 24)) if det is not None else None
                    except Exception:
                        faces = None
                    smp = {"t": i / src, "move": move, "faces": None if faces is None else len(faces), "mouth": None, "cx": None, "cy": None,
                           "area": None, "pose": set()}
                    if faces is not None and len(faces):
                        x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])
                        smp.update(cx=(x + fw / 2) / WIDTH, cy=(y + fh / 2) / g.shape[0], area=fw * fh / (WIDTH * g.shape[0]))
                        if diff is not None and move is not None:
                            m = diff[y + int(fh * 0.62): y + fh, x + int(fw * 0.2): x + int(fw * 0.8)]
                            smp["mouth"] = float(m.mean()) if m.size else None
                    if pose is not None and move is not None:
                        try:
                            smp["pose"] = _pose_flags(pose, sm)
                        except Exception:
                            pass
                    out.append(smp)
                    prev = g
                    if move is None and diff is not None:
                        prev = g                                          # restart the comparison after a cut
            i += 1
            if progress and total and i % (step * 20) == 0:
                progress(min(99, int(i * 100 / total)))
    finally:
        cap.release()
    if progress:
        progress(100)
    return out


def describe(samples):
    """samples of ONE line -> short text. Empty list -> ''."""
    if not samples:
        return ""
    ph = []
    n = len(samples)
    has_det = all(s["faces"] is not None for s in samples)       # False = face detector not available: say nothing about faces
    fc = [s["faces"] for s in samples if s["faces"] is not None]
    with_face = [s for s in samples if s["faces"]]
    if not has_det:
        pass
    elif len(with_face) < n * 0.3:
        ph.append("no face visible (off-screen voice or back turned)")
    else:
        k = int(np.median([f for f in fc if f] or [1]))
        ph.append("face on screen" if k <= 1 else f"{k} people on screen")
        mouth = [s["mouth"] for s in with_face if s["mouth"] is not None]
        if mouth:
            ph.append("mouth moving" if np.mean([m > MOUTH for m in mouth]) >= 0.4
                      else "face visible, mouth not moving")
    mv = [s["move"] for s in samples if s["move"] is not None]
    if mv:
        m = float(np.mean(mv))
        ph.append("standing still" if m < STILL else "small movement" if m < SMALL else "big movement")
    if len(with_face) >= 3:
        cx, cy = [s["cx"] for s in with_face], [s["cy"] for s in with_face]
        if (max(cx) - min(cx)) > 0.12 or (max(cy) - min(cy)) > 0.12:
            ph.append("head/body moves")
        a0, a1 = np.mean([s["area"] for s in with_face[:2]]), np.mean([s["area"] for s in with_face[-2:]])
        if a0 > 0 and a1 / a0 > 1.35:
            ph.append("moves closer")
        elif a0 > 0 and a1 / a0 < 0.7:
            ph.append("moves away")
    pf = set().union(*[s["pose"] for s in samples])
    ph += sorted(pf)
    return ", ".join(ph)


def analyze_motion(video, ranges, fps=FPS, use_pose=True, progress=None, cancel=None):
    """ranges = [(start, end), ...] (the script lines) -> [text per range]."""
    return describe_ranges(sample_frames(video, fps, progress, cancel, use_pose), ranges)


def describe_ranges(samples, ranges):
    """samples (from sample_frames, can be made in parallel with the audio work) + line times -> [text per line]."""
    out = []
    for a, b in ranges:
        seg = [s for s in samples if a <= s["t"] < b]
        if not seg and samples:                                           # very short line: take the nearest frame
            seg = [min(samples, key=lambda s: abs(s["t"] - (a + b) / 2))]
        out.append(describe(seg))
    return out
