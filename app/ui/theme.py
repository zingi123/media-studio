"""ស្បែក UI ងងឹតបែប MediaStudio — ដាក់តែម្តងលើ app ទាំងមូល (ទំព័រ + dialog ទាំងអស់)

Design tokens below mirror the approved Figma file ("Design System" page, node 0:1 / "Translator UI", node 1:2).
Every colour / radius / size used by the QSS is a token in ``T`` and is referenced as ``@token_name`` in the
template, so a value is changed in exactly one place. Widgets that paint themselves (CueTimeline) read ``T`` too.
"""
import re

# ------------------------------------------------------------------ design tokens (Figma "Design System")
T = {
    # background
    "bg_app": "#0a0d14", "bg_panel": "#11151f", "bg_raised": "#171c29", "bg_input": "#0e121b",
    "bg_hover": "#1e2536", "bg_preview": "#05070c",
    # border
    "border_subtle": "#1f2637", "border_default": "#2a3246", "border_focus": "#3b82f6",
    # text
    "text_primary": "#e8ecf6", "text_secondary": "#9aa4ba", "text_muted": "#6b7690", "text_disabled": "#4a546b",
    # accent
    "accent_primary": "#3b82f6", "accent_hover": "#5a98ff", "accent_secondary": "#8b5cf6", "accent_cyan": "#27b9f2",
    # status
    "success": "#22c55e", "warning": "#fbbf24", "danger": "#e5484d",
    # timeline
    "tl_text": "#7c4dff", "tl_text_empty": "#3a4a78", "tl_audio": "#0f8f86", "tl_playhead": "#ffffff",
    # subtitle table
    "selection": "#26407a",
    # --- values that exist in the original project and were NOT specified in the Figma file (kept as-is) ---
    "accent_pressed": "#2f6fd6",      # primary button :pressed (original theme value)
    "danger_hover": "#f06368",
    "tl_bg": "#0b0e15", "tl_lane": "#10141f", "tl_selected": "#6ee7f9",
    # radius / sizes
    "r_sm": "4px", "r_md": "8px", "r_lg": "12px", "r_pill": "999px",
    "rail_w": "84", "header_h": "52", "control_h": "32", "hero_h": "40", "section_header_h": "52",
}

FONT_UI = '"Inter", "Noto Sans Khmer", "Segoe UI", sans-serif'     # Inter for UI, Noto Sans Khmer for Khmer content
FONT_MONO = '"JetBrains Mono", Consolas, "Courier New", monospace'

_QSS_TEMPLATE = """
* { font-size: 13px; font-family: @font_ui; }
QWidget { background: @bg_app; color: @text_primary; }
QToolTip { background: @bg_raised; color: @text_primary; border: 1px solid @border_focus; }
QListWidget { background: @bg_panel; border: none; padding: 8px; outline: 0; }
QListWidget::item { padding: 11px 12px; border-radius: 10px; margin: 2px 0; color: @text_secondary; }
QListWidget::item:hover { background: @bg_raised; color: @text_primary; }
QListWidget::item:selected { background: @accent_primary; color: white; }
QPushButton { background: @bg_raised; border: 1px solid @border_default; border-radius: @r_md; padding: 6px 14px; color: @text_primary; }
QPushButton:hover { border-color: @border_focus; background: @bg_hover; }
QPushButton:pressed { background: @bg_input; }
QPushButton:checked { background: @accent_primary; border: 1px solid @accent_primary; color: white; }
QPushButton:disabled { color: @text_disabled; background: @bg_panel; border-color: @border_subtle; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit { background: @bg_input; border: 1px solid @border_default; border-radius: @r_md; padding: 5px 8px; selection-background-color: @accent_primary; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus { border-color: @border_focus; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled { color: @text_disabled; background: @bg_panel; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView { background: @bg_input; border: 1px solid @border_default; selection-background-color: @accent_primary; }
QTableWidget { background: @bg_panel; gridline-color: @border_subtle; border: 1px solid @border_subtle; border-radius: @r_lg; alternate-background-color: @bg_raised; selection-background-color: @selection; selection-color: @text_primary; }
QHeaderView::section { background: @bg_raised; color: @text_secondary; border: none; border-right: 1px solid @border_subtle; padding: 6px 8px; font-weight: 600; }
QTabWidget::pane { border: 1px solid @border_default; border-radius: @r_lg; top: -1px; }
QTabBar::tab { background: transparent; color: @text_secondary; padding: 6px 14px; margin-right: 4px; border-radius: @r_md; }
QTabBar::tab:selected { background: @bg_hover; color: @accent_hover; }
QTabBar::tab:hover { color: @text_primary; }
QGroupBox { border: 1px solid @border_default; border-radius: @r_lg; margin-top: 12px; padding: 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; color: @text_secondary; }
QProgressBar { background: @bg_input; border: none; border-radius: 7px; text-align: center; height: 14px; }
QProgressBar::chunk { border-radius: 7px; background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 @accent_primary,stop:1 @accent_secondary); }
QSlider::groove:horizontal { height: 5px; background: @border_default; border-radius: 2px; }
QSlider::sub-page:horizontal { background: @accent_primary; border-radius: 2px; }
QSlider::handle:horizontal { background: @text_primary; width: 14px; margin: -5px 0; border-radius: 7px; }
QCheckBox::indicator { width: 16px; height: 16px; border-radius: @r_sm; border: 1px solid @border_default; background: @bg_input; }
QCheckBox::indicator:checked { background: @accent_primary; border-color: @accent_primary; }
QScrollBar:vertical { background: transparent; width: 10px; }
QScrollBar:horizontal { background: transparent; height: 10px; }
QScrollBar::handle { background: @border_default; border-radius: 5px; min-height: 24px; min-width: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }

/* ===== Translator workspace (scoped by objectName - other pages are not affected) ===== */
QFrame#card, QFrame#topbar { background: @bg_panel; border: 1px solid @border_subtle; border-radius: @r_lg; }
QFrame#preview { background: @bg_preview; border: 1px solid @border_subtle; border-radius: @r_lg; }
QFrame#card QLabel, QFrame#topbar QLabel, QFrame#card QCheckBox, QFrame#preview QLabel { background: transparent; }
QFrame#preview QSlider { background: transparent; }
QWidget#plain { background: transparent; }
QWidget#plain QLabel, QWidget#plain QCheckBox { background: transparent; }
QLabel#cardTitle { color: @text_primary; font-weight: 600; font-size: 14px; background: transparent; }
QLabel#muted { color: @text_muted; font-size: 11px; background: transparent; }
QLabel#emptyTitle { color: @text_primary; font-size: 18px; font-weight: 600; background: transparent; }
QLabel#subLine { background: rgba(0,0,0,150); color: #ffffff; border-radius: @r_md; padding: 3px 10px; font-size: 15px; font-weight: 600; }
QLabel#timecode { color: @accent_hover; background: transparent; font-family: @font_mono; font-size: 12px; }
QPushButton#primary { background: @accent_primary; border: none; color: white; font-weight: 600; padding: 7px 16px; border-radius: @r_md; }
QPushButton#primary:hover { background: @accent_hover; }
QPushButton#primary:pressed { background: @accent_pressed; }
QPushButton#primary:disabled { background: @bg_raised; color: @text_disabled; }
QPushButton#danger { background: @danger; border: none; color: white; font-weight: 700; }
QPushButton#danger:hover { background: @danger_hover; }
QPushButton#chip { border-radius: 15px; padding: 5px 16px; }
QPushButton#voiceCard { padding: 10px 8px; min-height: 30px; }
QToolButton#collapse { background: @bg_input; border: 1px solid @border_subtle; border-radius: @r_md; color: @text_secondary; font-weight: 600; text-align: left; padding: 7px 10px; }
QToolButton#collapse:hover { background: @bg_hover; color: @text_primary; }
QSplitter::handle { background: @bg_app; }
QSplitter::handle:hover { background: @border_default; }
QSplitter::handle:horizontal { width: 6px; }
QSplitter::handle:vertical { height: 6px; }

/* ===== Translator-only application shell ===== */
QWidget#appRoot, QWidget#workspaceBody { background: @bg_app; }
QFrame#appHeader { background: @bg_app; border: none; border-bottom: 1px solid @border_subtle; border-radius: 0; }
QLabel#brand { color: @text_primary; font-size: 18px; font-weight: 700; }
QLabel#activeWorkspace { color: @text_primary; font-size: 13px; font-weight: 600; padding: 9px 14px; background: @bg_raised; border-bottom: 2px solid @accent_cyan; border-radius: @r_md @r_md 0 0; }
QLabel#headerHint { color: @text_muted; font-size: 10px; font-weight: 700; letter-spacing: 1px; }
QPushButton#navTab { color: @text_secondary; font-size: 13px; font-weight: 600; padding: 9px 14px; background: transparent; border: none; border-bottom: 2px solid transparent; border-radius: @r_md @r_md 0 0; }
QPushButton#navTab:hover { color: @text_primary; }
QPushButton#navTab:checked { color: @text_primary; background: @bg_raised; border-bottom: 2px solid @accent_cyan; }
QFrame#headerDivider { color: @border_default; max-width: 1px; }

/* ===== Left rail (RailItem: inactive / hover / active) ===== */
QWidget#workflowRail { background: @bg_panel; border: 1px solid @border_subtle; border-radius: @r_lg; }
QWidget#workflowRail QLabel { background: transparent; }
QPushButton#railBtn { text-align: center; padding: 4px 2px; background: transparent; border: 1px solid transparent; border-radius: 10px; color: @text_muted; font-size: 11px; }
QPushButton#railBtn:hover { background: @bg_hover; border-color: @border_default; color: @text_primary; }
QPushButton#railBtn[active="true"] { background: @bg_raised; border: 1px solid @border_subtle; border-left: 3px solid @accent_cyan; color: @text_primary; }
QPushButton#railLog { padding: 4px 2px; font-size: 11px; }

/* ===== SectionBox (the six groups) ===== */
QFrame#sectionBox { background: @bg_panel; border: 1px solid @border_subtle; border-radius: @r_lg; }
QFrame#sectionBox QFrame#card { background: transparent; border: none; border-radius: 0; }
QToolButton#sectionHeader { background: transparent; border: none; border-radius: @r_lg; padding: 0; text-align: left; }
QToolButton#sectionHeader:hover { background: @bg_raised; }
QFrame#sectionIconBox { background: @bg_raised; border: none; border-radius: @r_md; }
QLabel#sectionIcon { background: transparent; color: @accent_cyan; font-size: 15px; }
QLabel#sectionTitle { background: transparent; color: @text_primary; font-size: 14px; font-weight: 600; }
QLabel#sectionSub { background: transparent; color: @text_muted; font-size: 11px; }
QLabel#sectionChev { background: transparent; color: @text_secondary; font-size: 14px; }
QFrame#sectionDivider { background: @border_subtle; border: none; }
QLabel#statusDot { background: @text_disabled; border-radius: 4px; border: none; }
QLabel#statusDot[state="ready"] { background: @success; }
QLabel#statusDot[state="running"] { background: @accent_primary; }
QLabel#statusDot[state="warning"] { background: @warning; }
QLabel#statusDot[state="error"] { background: @danger; }
"""


def render_qss(template=_QSS_TEMPLATE, tokens=None):
    """Replace every @token with its value. A misspelt token raises KeyError instead of silently breaking the style."""
    t = dict(tokens or T)
    t["font_ui"] = FONT_UI
    t["font_mono"] = FONT_MONO
    return re.sub(r"@([a-z_0-9]+)", lambda m: t[m.group(1)], template)


QSS = render_qss()


def apply_theme():
    from PySide6.QtWidgets import QApplication   # imported here so the tokens / QSS can be read without Qt installed
    QApplication.instance().setStyleSheet(QSS)   # ដាក់លើ app ទាំងមូល
