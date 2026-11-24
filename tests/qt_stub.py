"""A tiny fake PySide6 used ONLY by the smoke tests (there is no Qt in the test sandbox).
Value widgets (spin boxes, sliders, check boxes, combo boxes, line edits) really store values and fire their signals, so the
Style-tab <-> on-screen-object synchronisation logic can be exercised.  It cannot draw anything and says nothing about real Qt behaviour."""
import sys
import types


class _Meta(type):
    def __getattr__(cls, n):
        if n.startswith("__"):
            raise AttributeError(n)
        return Stub()


class Stub(metaclass=_Meta):
    def __init__(self, *a, **k):
        pass

    def __getattr__(self, n):
        if n.startswith("__"):
            raise AttributeError(n)
        return Stub()

    def __call__(self, *a, **k):
        return Stub()

    def _t(self, *a):
        return Stub()
    __or__ = __ror__ = __and__ = __rand__ = __invert__ = __add__ = __radd__ = __sub__ = __mul__ = __truediv__ = _t

    def __bool__(self):
        return True

    def count(self):
        return 0

    def currentRow(self):
        return -1

    def selectedIndexes(self):
        return []

    def width(self):
        return 900

    def height(self):
        return 300

    def rowCount(self):
        return 0

    def __iter__(self):
        return iter(())

    def __int__(self):
        return 0

    def __float__(self):
        return 0.0

    def __index__(self):
        return 0

    def __lt__(self, o):
        return True
    __le__ = __gt__ = __ge__ = __lt__

    def __hash__(self):
        return id(self)


class Signal:
    def __init__(self, *a):
        self._name = None

    def __set_name__(self, owner, name):
        self._name = name

    def __get__(self, obj, typ=None):
        if obj is None:
            return self
        d = obj.__dict__.setdefault("_sigs", {})
        return d.setdefault(self._name or id(self), _Bound())


class _Bound:
    def __init__(self):
        self.slots = []

    def connect(self, f, *a):
        self.slots.append(f)

    def disconnect(self, *a):
        self.slots.clear()

    def emit(self, *a):
        for f in list(self.slots):
            f(*a)

    def __call__(self, *a):
        self.emit(*a)


def _sigs(*names):
    return {n: Signal() for n in names}


class _Value(Stub):
    SIGS = ()

    def __init__(self, *a, **k):
        self._val = self._default()
        self._enabled = True
        self._checked = False
        self._rng = (0, 100)

    def _default(self):
        return 0

    def setRange(self, lo, hi):
        self._rng = (lo, hi)

    def minimum(self):
        return self._rng[0]

    def maximum(self):
        return self._rng[1]

    def setValue(self, v):
        v = max(self._rng[0], min(self._rng[1], v)) if isinstance(v, (int, float)) else v
        if v != self._val:
            self._val = v
            self.valueChanged.emit(v)

    def value(self):
        return self._val

    def setEnabled(self, v):
        self._enabled = v

    def setChecked(self, v):
        v = bool(v)
        if v != self._checked:
            self._checked = v
            self.toggled.emit(v)

    def isChecked(self):
        return self._checked


def _mk(name, extra=None, sigs=("valueChanged", "toggled", "clicked", "currentIndexChanged", "currentTextChanged", "textChanged", "activated")):
    ns = {s: Signal() for s in sigs}
    ns.update(extra or {})
    return type(name, (_Value,), ns)


def _combo_ns():
    def init(self, *a, **k):
        _Value.__init__(self)
        self._items = []
        self._idx = -1

    def addItem(self, text, data=None):
        self._items.append((text, data))
        if self._idx < 0:
            self._idx = 0

    def addItems(self, items):
        for t in items:
            self.addItem(t)

    def currentIndex(self):
        return self._idx

    def setCurrentIndex(self, i):
        if i != self._idx and -1 <= i < len(self._items):
            self._idx = i
            self.currentIndexChanged.emit(i)
            if i >= 0:
                self.currentTextChanged.emit(self._items[i][0])

    def currentText(self):
        return self._items[self._idx][0] if 0 <= self._idx < len(self._items) else ""

    def currentData(self):
        return self._items[self._idx][1] if 0 <= self._idx < len(self._items) else None

    def setCurrentText(self, t):
        for i, (tx, _) in enumerate(self._items):
            if tx == t:
                self.setCurrentIndex(i)
                return

    def findText(self, t):
        return next((i for i, (tx, _) in enumerate(self._items) if tx == t), -1)

    def findData(self, d):
        return next((i for i, (_, dd) in enumerate(self._items) if dd == d), -1)

    def setItemText(self, i, t):
        self._items[i] = (t, self._items[i][1])

    def count(self):
        return len(self._items)
    return {k: v for k, v in locals().items() if not k.startswith("__")} | {"__init__": init}


def _text_ns():
    def init(self, *a, **k):
        _Value.__init__(self)
        self._text = a[0] if a and isinstance(a[0], str) else ""

    def text(self):
        return self._text

    def setText(self, t):
        self._text = t
        self.textChanged.emit(t)

    def toPlainText(self):
        return self._text

    def setPlainText(self, t):
        self._text = t
        self.textChanged.emit()          # QPlainTextEdit.textChanged has no argument

    def appendPlainText(self, t):
        self._text += t + "\n"
    return {"__init__": init, "text": text, "setText": setText, "toPlainText": toPlainText, "setPlainText": setPlainText,
            "appendPlainText": appendPlainText}


_REAL = {}
for n in ("QSpinBox", "QDoubleSpinBox", "QSlider", "QCheckBox", "QPushButton", "QToolButton", "QProgressBar"):
    _REAL[n] = _mk(n)
_REAL["QComboBox"] = _mk("QComboBox", _combo_ns())
for n in ("QLineEdit", "QLabel", "QPlainTextEdit", "QTextEdit"):
    _REAL[n] = _mk(n, _text_ns())


class _QTimer(Stub):
    timeout = Signal()

    def __init__(self, *a, **k):
        self.fired = False

    @staticmethod
    def singleShot(ms, fn):
        fn()

    def start(self, *a):
        self.fired = False


_REAL["QTimer"] = _QTimer
_CACHE = {}


class _Mod(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        if name == "Signal":
            return Signal
        if name == "Slot":
            return lambda *a, **k: (lambda f: f)
        if name in _REAL:
            return _REAL[name]
        if name not in _CACHE:
            _CACHE[name] = type(name, (Stub,), {})
        return _CACHE[name]


def install():
    for m in ("PySide6", "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets"):
        sys.modules[m] = _Mod(m)
