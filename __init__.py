"""
Quick Image Search for Anki
============================
Adds a "Search Images" button to the editor toolbar (in the Browse
window). Clicking it opens an image search for the text in a
configurable field of the current note.

Configuration:
  In Anki, go to Tools -> Add-ons -> select this add-on -> Config
  Or use the form dialog that appears.

Installation:
  1. Download quick_image_search.ankiaddon (run build.py to generate it)
  2. In Anki, go to Tools -> Add-ons -> Install from file...
  3. Select the downloaded file and restart Anki
"""

import json
import os
import re
import sys
import time
import urllib.parse
import webbrowser
from typing import Optional

from aqt import mw, gui_hooks
from aqt.editor import Editor
from aqt.qt import (
    QAction,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGuiApplication,
    QHBoxLayout,
    QKeySequence,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QPushButton,
    QTimer,
    QVBoxLayout,
    Qt,
)
from aqt.utils import tooltip


# --- Config ---

DEFAULT_SHORTCUT = "Ctrl+Shift+1"

# Qt's portable-text token for the keypad modifier. QKeySequenceEdit can
# tag a plain number-row press with it (it does on macOS), and the
# resulting sequence then never matches that key. Strip it.
KEYPAD_TOKEN = "Num+"


def normalize_shortcut(shortcut):
    # type: (Optional[str]) -> str
    """Drop the keypad modifier and re-render as portable text."""
    if not shortcut:
        return ""
    seq = QKeySequence(shortcut.replace(KEYPAD_TOKEN, ""))
    if seq.count() == 0:
        return ""
    return seq.toString(QKeySequence.SequenceFormat.PortableText)

GOOGLE_DOMAINS = [
    ("Ukrainian - google.com.ua", "google.com.ua"),
    ("English (US) - google.com", "google.com"),
    ("English (UK) - google.co.uk", "google.co.uk"),
    ("Spanish - google.es", "google.es"),
    ("French - google.fr", "google.fr"),
    ("German - google.de", "google.de"),
    ("Italian - google.it", "google.it"),
    ("Portuguese - google.pt", "google.pt"),
    ("Polish - google.pl", "google.pl"),
    ("Japanese - google.co.jp", "google.co.jp"),
    ("Korean - google.co.kr", "google.co.kr"),
    ("Chinese - google.com.cn", "google.com.cn"),
]


def get_config():
    """Load add-on config with defaults."""
    conf = mw.addonManager.getConfig(__name__)
    if conf is None:
        conf = {}
    defaults = {
        "field_name": "Front",
        "google_domain": "google.com",
        "shortcut": DEFAULT_SHORTCUT,
        "snap_layout_enabled": True,
        "layout_mode": "anki_right_50",
        "custom_anki_rect": None,
        "custom_browser_rect": None,
    }
    for key, val in defaults.items():
        if key not in conf:
            conf[key] = val
    conf["shortcut"] = normalize_shortcut(conf["shortcut"])
    return conf


def save_config(conf):
    """Save add-on config."""
    mw.addonManager.writeConfig(__name__, conf)


# --- Config Dialog ---

class ConfigDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Quick Image Search - Settings")
        self.setMinimumWidth(450)

        conf = get_config()

        layout = QVBoxLayout()

        # Description
        desc = QLabel(
            "Configure which note field to search, which Google domain\n"
            "to use, and the keyboard shortcut for the toolbar button."
        )
        layout.addWidget(desc)

        # Form
        form = QFormLayout()

        self.field_input = QLineEdit()
        self.field_input.setText(conf["field_name"])
        self.field_input.setPlaceholderText("e.g. Ukrainian, Spanish, Front")
        form.addRow("Field name:", self.field_input)

        self.domain_combo = QComboBox()
        current_domain = conf["google_domain"]
        selected_index = 0
        for i, (label, domain) in enumerate(GOOGLE_DOMAINS):
            self.domain_combo.addItem(label, domain)
            if domain == current_domain:
                selected_index = i

        # Add "Custom" option
        self.domain_combo.addItem("Custom...", "custom")

        # Check if current domain is not in the predefined list
        known_domains = [d for _, d in GOOGLE_DOMAINS]
        if current_domain not in known_domains:
            selected_index = self.domain_combo.count() - 1

        self.domain_combo.setCurrentIndex(selected_index)

        form.addRow("Google domain:", self.domain_combo)

        self.custom_domain_input = QLineEdit()
        self.custom_domain_input.setPlaceholderText("e.g. google.com.br")
        if current_domain not in known_domains:
            self.custom_domain_input.setText(current_domain)
        self.custom_domain_input.setVisible(
            self.domain_combo.currentData() == "custom"
        )
        form.addRow("Custom domain:", self.custom_domain_input)

        self.domain_combo.currentIndexChanged.connect(self._on_domain_changed)

        self.shortcut_edit = QKeySequenceEdit()
        self.shortcut_edit.setKeySequence(QKeySequence(conf["shortcut"]))
        # Qt 6.5+ only; older builds capture multi-key sequences, which
        # get_values() truncates to the first one anyway.
        if hasattr(self.shortcut_edit, "setMaximumSequenceLength"):
            self.shortcut_edit.setMaximumSequenceLength(1)

        clear_btn = QPushButton("Clear")
        clear_btn.setToolTip("Remove the shortcut")
        clear_btn.clicked.connect(self.shortcut_edit.clear)

        shortcut_row = QHBoxLayout()
        shortcut_row.addWidget(self.shortcut_edit)
        shortcut_row.addWidget(clear_btn)
        form.addRow("Shortcut:", shortcut_row)

        layout.addLayout(form)

        hint = QLabel(
            "Leave the shortcut empty to disable it. Shortcut changes apply\n"
            "to editor windows opened after saving."
        )
        layout.addWidget(hint)

        # Layout section
        layout_sep = QLabel("Disposition des fenetres :")
        layout_sep.setStyleSheet("font-weight: bold; margin-top: 8px;")
        layout.addWidget(layout_sep)

        form_layout = QFormLayout()

        self.snap_cb = QCheckBox("Organiser automatiquement les fenetres cote a cote")
        self.snap_cb.setChecked(conf.get("snap_layout_enabled", True))
        form_layout.addRow("", self.snap_cb)

        self.layout_mode_combo = QComboBox()
        self.layout_mode_combo.addItem("Editeur Anki a droite, navigateur a gauche (50/50)", "anki_right_50")
        self.layout_mode_combo.addItem("Editeur Anki a gauche, navigateur a droite (50/50)", "anki_left_50")
        self.layout_mode_combo.addItem("Disposition personnalisee memorisee", "custom")

        mode_val = conf.get("layout_mode", "anki_right_50")
        for idx in range(self.layout_mode_combo.count()):
            if self.layout_mode_combo.itemData(idx) == mode_val:
                self.layout_mode_combo.setCurrentIndex(idx)
                break
        form_layout.addRow("Disposition :", self.layout_mode_combo)

        self.custom_anki_rect = conf.get("custom_anki_rect")
        self.custom_browser_rect = conf.get("custom_browser_rect")

        self.memorize_btn = QPushButton("Memoriser la disposition actuelle (Anki + Navigateur)")
        self.memorize_btn.setToolTip(
            "Capture les coordonnees exactes de la fenetre Anki et de votre navigateur a l'ecran."
        )
        self.memorize_btn.clicked.connect(self._on_memorize_clicked)
        form_layout.addRow("", self.memorize_btn)

        self.layout_status_lbl = QLabel()
        self.layout_status_lbl.setStyleSheet("color: #666; font-size: 11px;")
        self._update_layout_status()
        form_layout.addRow("", self.layout_status_lbl)

        layout.addLayout(form_layout)

        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.setLayout(layout)

    def _update_layout_status(self):
        if self.custom_anki_rect and self.custom_browser_rect:
            ak = self.custom_anki_rect
            br = self.custom_browser_rect
            self.layout_status_lbl.setText(
                "Position memorisee : Anki (%dx%d a %d,%d) | Navigateur (%dx%d a %d,%d)"
                % (
                    ak.get("w", 0),
                    ak.get("h", 0),
                    ak.get("x", 0),
                    ak.get("y", 0),
                    br.get("w", 0),
                    br.get("h", 0),
                    br.get("x", 0),
                    br.get("y", 0),
                )
            )
        else:
            self.layout_status_lbl.setText("Aucune disposition personnalisee enregistree (mode 50/50 par defaut).")

    def _on_memorize_clicked(self):
        anki_win = self.parent() or mw
        if hasattr(anki_win, "window"):
            anki_win = anki_win.window()
        geo = anki_win.geometry()
        anki_rect = {"x": geo.x(), "y": geo.y(), "w": geo.width(), "h": geo.height()}

        candidate = find_browser_window()
        if not candidate:
            self.layout_status_lbl.setText("Aucun navigateur detecte a l'ecran. Ouvrez votre navigateur et reessayez.")
            return

        b_hwnd, b_cls, b_title, (bx, by, bw, bh) = candidate
        browser_rect = {"x": bx, "y": by, "w": bw, "h": bh}

        self.custom_anki_rect = anki_rect
        self.custom_browser_rect = browser_rect

        for idx in range(self.layout_mode_combo.count()):
            if self.layout_mode_combo.itemData(idx) == "custom":
                self.layout_mode_combo.setCurrentIndex(idx)
                break

        self._update_layout_status()

    def _on_domain_changed(self, index):
        is_custom = self.domain_combo.currentData() == "custom"
        self.custom_domain_input.setVisible(is_custom)

    def get_values(self):
        field_name = self.field_input.text().strip()
        if self.domain_combo.currentData() == "custom":
            domain = self.custom_domain_input.text().strip()
        else:
            domain = self.domain_combo.currentData()

        seq = self.shortcut_edit.keySequence()
        if seq.count() > 1:
            # Keep only the first key of a multi-key sequence.
            seq = QKeySequence(seq[0])
        shortcut = normalize_shortcut(
            seq.toString(QKeySequence.SequenceFormat.PortableText)
        )

        snap_enabled = self.snap_cb.isChecked()
        layout_mode = self.layout_mode_combo.currentData()

        return (
            field_name,
            domain,
            shortcut,
            snap_enabled,
            layout_mode,
            self.custom_anki_rect,
            self.custom_browser_rect,
        )


def on_config():
    """Show the config dialog."""
    dlg = ConfigDialog(mw)
    if dlg.exec():
        (
            field_name,
            domain,
            shortcut,
            snap_enabled,
            layout_mode,
            custom_anki_rect,
            custom_browser_rect,
        ) = dlg.get_values()
        if field_name and domain:
            conf = get_config()
            conf["field_name"] = field_name
            conf["google_domain"] = domain
            conf["shortcut"] = shortcut
            conf["snap_layout_enabled"] = snap_enabled
            conf["layout_mode"] = layout_mode
            conf["custom_anki_rect"] = custom_anki_rect
            conf["custom_browser_rect"] = custom_browser_rect
            save_config(conf)
            tooltip(
                "Parametres enregistres. Disposition : %s"
                % ("Activee" if snap_enabled else "Desactivee")
            )


# --- Shortcut plumbing ---

# Anki's editor is a QtWebEngine view. While the caret is inside a field,
# Chromium claims the key event (it accepts Qt's ShortcutOverride for
# editable content), so the QShortcut that editor.addButton(keys=...)
# registers never fires. The shortcut is therefore also installed as a
# keydown listener inside the editor page, which calls back into the same
# bridge command the toolbar button uses.

EDITOR_CMD = "google_image_search"

# Qt key name -> DOM KeyboardEvent.code, for keys that aren't a plain
# letter, digit or function key.
DOM_CODES = {
    "Space": "Space",
    "Return": "Enter",
    "Enter": "NumpadEnter",
    "Backspace": "Backspace",
    "Tab": "Tab",
    "Esc": "Escape",
    "Ins": "Insert",
    "Del": "Delete",
    "Home": "Home",
    "End": "End",
    "PgUp": "PageUp",
    "PgDown": "PageDown",
    "Left": "ArrowLeft",
    "Right": "ArrowRight",
    "Up": "ArrowUp",
    "Down": "ArrowDown",
    ",": "Comma",
    ".": "Period",
    ";": "Semicolon",
    "'": "Quote",
    "[": "BracketLeft",
    "]": "BracketRight",
    "\\": "Backslash",
    "/": "Slash",
    "-": "Minus",
    "=": "Equal",
    "`": "Backquote",
}


def _enum_value(val):
    """PyQt enums expose .value; older bindings are plain ints."""
    return val.value if hasattr(val, "value") else int(val)


def shortcut_to_dom_spec(shortcut):
    # type: (str) -> Optional[dict]
    """Translate a Qt key sequence into a DOM KeyboardEvent matcher.

    Returns None if the sequence is empty or uses a key we can't map."""
    if not shortcut:
        return None

    seq = QKeySequence(shortcut)
    if seq.count() == 0:
        return None

    combo = seq[0]
    # Qt 5 indexes a sequence as plain ints, Qt 6 as QKeyCombination.
    if hasattr(combo, "key"):
        key = _enum_value(combo.key())
        mods = _enum_value(combo.keyboardModifiers())
    else:
        key = int(combo) & ~int(Qt.KeyboardModifier.KeyboardModifierMask)
        mods = int(combo) & int(Qt.KeyboardModifier.KeyboardModifierMask)

    code = None
    key_a = _enum_value(Qt.Key.Key_A)
    key_0 = _enum_value(Qt.Key.Key_0)
    key_f1 = _enum_value(Qt.Key.Key_F1)
    if key_a <= key <= _enum_value(Qt.Key.Key_Z):
        code = "Key" + chr(key)
    elif key_0 <= key <= _enum_value(Qt.Key.Key_9):
        code = "Digit" + chr(key)
    elif key_f1 <= key <= _enum_value(Qt.Key.Key_F24):
        code = "F%d" % (key - key_f1 + 1)
    else:
        name = QKeySequence(key).toString(
            QKeySequence.SequenceFormat.PortableText
        )
        code = DOM_CODES.get(name)

    if not code:
        return None

    ctrl = bool(mods & _enum_value(Qt.KeyboardModifier.ControlModifier))
    meta = bool(mods & _enum_value(Qt.KeyboardModifier.MetaModifier))

    # On macOS Qt swaps the two: ControlModifier is Command, MetaModifier
    # is the physical Control key. The DOM reports them unswapped.
    if sys.platform == "darwin":
        ctrl, meta = meta, ctrl

    return {
        "code": code,
        "ctrl": ctrl,
        "meta": meta,
        "shift": bool(mods & _enum_value(Qt.KeyboardModifier.ShiftModifier)),
        "alt": bool(mods & _enum_value(Qt.KeyboardModifier.AltModifier)),
    }


# Guards against the Qt shortcut and the DOM listener both firing for a
# single keypress.
_last_activation = [0.0]


def _debounced(func):
    def wrapper(editor):
        now = time.time()
        if now - _last_activation[0] < 0.25:
            return
        _last_activation[0] = now
        return func(editor)

    return wrapper


def install_web_shortcut(editor):
    # type: (Editor) -> None
    """Install the configured shortcut as a keydown listener in the
    editor page, so it also works while a field has focus."""
    if not hasattr(editor, "web") or not editor.web:
        return

    spec = shortcut_to_dom_spec(get_config()["shortcut"])

    editor.web.eval(
        """
(() => {
    const slot = "_quickImageSearchShortcut";
    if (window[slot]) {
        document.removeEventListener("keydown", window[slot], true);
        window[slot] = null;
    }
    const spec = %s;
    if (!spec) {
        return;
    }
    const handler = (event) => {
        if (event.code !== spec.code) return;
        if (event.ctrlKey !== spec.ctrl) return;
        if (event.metaKey !== spec.meta) return;
        if (event.shiftKey !== spec.shift) return;
        if (event.altKey !== spec.alt) return;
        event.preventDefault();
        event.stopPropagation();
        pycmd("%s");
    };
    window[slot] = handler;
    document.addEventListener("keydown", handler, true);
})();
"""
        % (json.dumps(spec), EDITOR_CMD)
    )


# --- Core Functions ---

def strip_html(text):
    # type: (str) -> str
    """Remove HTML tags and decode common entities."""
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ")
    text = text.replace("&amp;", "&")
    text = text.replace("&lt;", "<")
    text = text.replace("&gt;", ">")
    text = text.replace("&quot;", '"')
    return text.strip()


def get_search_text(editor):
    # type: (Editor) -> Optional[str]
    """Get the text from the configured field of the current note,
    falling back to the first field if not found."""
    note = getattr(editor, "note", None)
    if note is None:
        note = getattr(editor, "current_note", None)
    if note is None and callable(getattr(editor, "get_note", None)):
        try:
            note = editor.get_note()
        except Exception:
            pass
    if not note and hasattr(editor, "nid") and editor.nid:
        try:
            col = getattr(mw, "col", None) or getattr(getattr(editor, "mw", None), "col", None)
            if col:
                note = col.get_note(int(editor.nid))
        except Exception:
            note = None

    if not note or not getattr(note, "fields", None):
        return None

    conf = get_config()
    field_name = conf["field_name"]

    note_type = note.note_type() if hasattr(note, "note_type") else note.model()
    field_names = [f["name"] for f in note_type["flds"]]
    if field_name in field_names:
        raw = note[field_name]
    else:
        raw = note.fields[0]

    return strip_html(raw)


def find_browser_window():
    # type: () -> Optional[tuple]
    """Find the top-level visible browser window (Firefox, Chrome, Edge, etc.)."""
    if sys.platform != "win32":
        return None

    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32

        # Ensure enumeration can access the active interactive desktop
        h_desk = user32.OpenDesktopW("Default", 0, False, 0x01FF)
        if h_desk:
            user32.SetThreadDesktop(h_desk)

        browser_classes = ("MozillaWindowClass", "Chrome_WidgetWin_1", "ApplicationFrameWindow")
        candidates = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def enum_cb(hwnd, lparam):
            if not user32.IsWindowVisible(hwnd):
                return True

            cls_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls_buf, 256)
            cls_name = cls_buf.value

            if cls_name in browser_classes:
                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                w = rect.right - rect.left
                h = rect.bottom - rect.top
                if w > 200 and h > 200:
                    length = user32.GetWindowTextLengthW(hwnd)
                    title = ""
                    if length > 0:
                        t_buf = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, t_buf, length + 1)
                        title = t_buf.value
                    candidates.append((hwnd, cls_name, title, (rect.left, rect.top, w, h)))
            return True

        user32.EnumWindows(enum_cb, 0)
        if not candidates:
            return None

        def score(item):
            hwnd, cls, title, (x, y, w, h) = item
            t_lower = title.lower()
            val = 0
            if "google" in t_lower or "recherche" in t_lower or "image" in t_lower:
                val += 100
            if "mozilla" in cls.lower():
                val += 30
            val += min(w * h // 10000, 50)
            return val

        candidates.sort(key=score, reverse=True)
        return candidates[0]
    except Exception:
        return None


def move_window(hwnd, x, y, w, h):
    # type: (int, int, int, int, int) -> None
    """Restore and position a native Win32 window."""
    if sys.platform != "win32" or not hwnd:
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        if user32.IsZoomed(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        SWP_NOZORDER = 0x0004
        SWP_SHOWWINDOW = 0x0040
        SWP_FRAMECHANGED = 0x0020
        user32.SetWindowPos(
            hwnd,
            0,
            int(x),
            int(y),
            int(w),
            int(h),
            SWP_NOZORDER | SWP_SHOWWINDOW | SWP_FRAMECHANGED,
        )
    except Exception:
        pass


def apply_side_by_side_layout(editor, url):
    # type: (Editor, str) -> None
    """Position Anki and the web browser side-by-side according to user preference."""
    conf = get_config()

    parent = getattr(editor, "parentWindow", None)
    top_window = parent() if callable(parent) else parent
    if not top_window and hasattr(editor, "widget"):
        top_window = editor.widget
    if hasattr(top_window, "window") and callable(top_window.window):
        top_window = top_window.window()
    if not top_window:
        top_window = mw

    screen = top_window.screen() if hasattr(top_window, "screen") and callable(top_window.screen) else None
    if not screen and QGuiApplication.instance():
        screen = QGuiApplication.primaryScreen()

    if screen:
        geo = screen.availableGeometry()
        gx, gy, gw, gh = geo.x(), geo.y(), geo.width(), geo.height()
    else:
        gx, gy, gw, gh = 0, 0, 1440, 900

    layout_mode = conf.get("layout_mode", "anki_right_50")
    custom_anki = conf.get("custom_anki_rect")
    custom_browser = conf.get("custom_browser_rect")

    if layout_mode == "custom" and custom_anki and custom_browser:
        anki_rect = (custom_anki["x"], custom_anki["y"], custom_anki["w"], custom_anki["h"])
        browser_rect = (custom_browser["x"], custom_browser["y"], custom_browser["w"], custom_browser["h"])
    elif layout_mode == "anki_left_50":
        split_w = gw // 2
        anki_rect = (gx, gy, split_w, gh)
        browser_rect = (gx + split_w, gy, gw - split_w, gh)
    else:  # "anki_right_50" (default: browser left, Anki right)
        split_w = gw // 2
        browser_rect = (gx, gy, split_w, gh)
        anki_rect = (gx + split_w, gy, gw - split_w, gh)

    # Position Anki window
    try:
        if top_window.isMaximized():
            top_window.showNormal()
            QTimer.singleShot(
                40,
                lambda: top_window.setGeometry(anki_rect[0], anki_rect[1], anki_rect[2], anki_rect[3]),
            )
        else:
            top_window.setGeometry(anki_rect[0], anki_rect[1], anki_rect[2], anki_rect[3])
    except Exception:
        pass

    # Open URL in browser
    webbrowser.open(url)

    # Position browser window with progressive attempts
    def do_position_browser():
        candidate = find_browser_window()
        if candidate:
            move_window(candidate[0], browser_rect[0], browser_rect[1], browser_rect[2], browser_rect[3])

    do_position_browser()
    for delay in (120, 300, 600, 1200):
        QTimer.singleShot(delay, do_position_browser)


def on_memorize_current_layout():
    """Memorize the current position of Anki and browser windows."""
    anki_win = mw.app.activeWindow() or mw
    if hasattr(anki_win, "window"):
        anki_win = anki_win.window()

    geo = anki_win.geometry()
    anki_rect = {"x": geo.x(), "y": geo.y(), "w": geo.width(), "h": geo.height()}

    candidate = find_browser_window()
    if not candidate:
        tooltip("Quick Image Search : aucun navigateur detecte a l'ecran.")
        return

    b_hwnd, b_cls, b_title, (bx, by, bw, bh) = candidate
    browser_rect = {"x": bx, "y": by, "w": bw, "h": bh}

    conf = get_config()
    conf["custom_anki_rect"] = anki_rect
    conf["custom_browser_rect"] = browser_rect
    conf["layout_mode"] = "custom"
    conf["snap_layout_enabled"] = True
    save_config(conf)

    tooltip(
        "Disposition Quick Image Search memorisee :\n"
        f"Anki ({anki_rect['w']}x{anki_rect['h']}) a droite | "
        f"Navigateur ({browser_rect['w']}x{browser_rect['h']}) a gauche"
    )


def on_search_images(editor):
    # type: (Editor) -> None
    """Open a Google Image search for the current note's text."""
    text = get_search_text(editor)
    if not text:
        tooltip("No card text found.")
        return

    conf = get_config()
    domain = conf["google_domain"]

    params = urllib.parse.urlencode({"tbm": "isch", "q": text})
    url = "https://www.%s/search?%s" % (domain, params)

    if conf.get("snap_layout_enabled", True):
        apply_side_by_side_layout(editor, url)
    else:
        webbrowser.open(url)

    tooltip("Searching images for: %s" % text)


def add_editor_button(buttons, editor):
    # type: (list, Editor) -> None
    """Add the Search Images button to the editor toolbar."""
    icon = os.path.join(os.path.dirname(__file__), "search.svg")

    conf = get_config()
    domain = conf["google_domain"]
    # An empty shortcut means "no shortcut"; addButton treats it as unset.
    keys = conf["shortcut"] or None

    tip_text = "Search Google Images (%s)" % domain
    if keys:
        tip_text += " - %s" % QKeySequence(keys).toString(
            QKeySequence.SequenceFormat.NativeText
        )

    func = _debounced(on_search_images)

    if os.path.exists(icon):
        btn = editor.addButton(
            icon=icon,
            cmd=EDITOR_CMD,
            func=func,
            tip=tip_text,
            label="",
            keys=keys,
        )
    else:
        btn = editor.addButton(
            icon=None,
            cmd=EDITOR_CMD,
            func=func,
            tip=tip_text,
            label="Search Images",
            keys=keys,
        )

    buttons.append(btn)


# --- Setup ---

gui_hooks.editor_did_init_buttons.append(add_editor_button)

# The Qt shortcut registered above only fires when the caret is outside a
# field, so mirror it inside the editor page once a note is loaded.
gui_hooks.editor_did_load_note.append(install_web_shortcut)

# Register custom config dialog so it opens instead of raw JSON
mw.addonManager.setConfigAction(__name__, on_config)

# Menu Tools action for memorizing layout
def setup_tools_menu():
    action = QAction("Quick Image Search : Memoriser la disposition des fenetres", mw)
    action.triggered.connect(on_memorize_current_layout)
    mw.form.menuTools.addAction(action)

if hasattr(mw, "form") and hasattr(mw.form, "menuTools"):
    setup_tools_menu()
elif hasattr(gui_hooks, "main_window_did_init"):
    gui_hooks.main_window_did_init.append(setup_tools_menu)


# Compatibility with Anki's NewEditor (Svelte editor in modern Anki)
try:
    from aqt.editor import NewEditor
    if not hasattr(NewEditor, "note"):
        def _get_new_editor_note(self):
            cached = getattr(self, "_note_compat_cache", None)
            cur_nid = getattr(self, "nid", None)
            if cached is not None and (cur_nid is None or getattr(cached, "id", None) == cur_nid):
                return cached
            if cur_nid:
                col = getattr(mw, "col", None) or getattr(getattr(self, "mw", None), "col", None)
                if col:
                    try:
                        n = col.get_note(int(cur_nid))
                        self._note_compat_cache = n
                        return n
                    except Exception:
                        return cached
            return cached

        def _set_new_editor_note(self, val):
            self._note_compat_cache = val
            if val is not None and hasattr(val, "id") and val.id:
                self.nid = val.id

        NewEditor.note = property(_get_new_editor_note, _set_new_editor_note)
except (ImportError, AttributeError):
    pass

