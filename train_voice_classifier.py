"""
Train the optional Fantasy/Animal voice-type classifier.

Folder layout (you must record/collect the clips yourself, >=30 clips/class recommended, 3-30 s each):
    training_voices/
        God/            *.wav
        Ghost/          *.wav
        Cat/            *.wav
        Adult Man/      *.wav     (include Human classes too so the model can say 'not fantasy')
Folder names MUST equal the Voice Type names. Run:   python train_voice_classifier.py training_voices
Result is saved to  ~/MediaStudio/models/voice_type_model.joblib  and is picked up by Generate Voice Type.
"""
import sys
from pathlib import Path
import numpy as np
import librosa, joblib
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import cross_val_score
from app.translator.voice_detect import features, feature_vector, SR
from app.shared.paths import data_dir

root = Path(sys.argv[1] if len(sys.argv) > 1 else "training_voices")
X, y = [], []
for d in sorted(p for p in root.iterdir() if p.is_dir()):
    for f in list(d.glob("*.wav")) + list(d.glob("*.mp3")):
        a, _ = librosa.load(str(f), sr=SR, mono=True)
        X.append(feature_vector(features(a))); y.append(d.name)
    print(d.name, y.count(d.name), "clips")
if len(set(y)) < 2:
    sys.exit("Need at least 2 classes.")
clf = RandomForestClassifier(n_estimators=300, class_weight="balanced", random_state=0)
print("cross-val accuracy:", np.round(cross_val_score(clf, X, y, cv=min(5, min(y.count(c) for c in set(y)))).mean(), 3))
clf.fit(X, y)
out = data_dir() / "models"; out.mkdir(exist_ok=True)
joblib.dump(clf, out / "voice_type_model.joblib")
print("saved", out / "voice_type_model.joblib")
