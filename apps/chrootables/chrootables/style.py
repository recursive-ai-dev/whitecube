import re


STYLE = """
QWidget { background: #121719; color: #e7eeeb; font-family: 'Noto Sans', sans-serif; font-size: 13px; }
QMainWindow, QDialog { background: #121719; }
QFrame#sidebar { background: #0d1214; border-right: 1px solid #263032; }
QFrame#sidebar QLabel, QFrame#sidebar QWidget { background: transparent; }
QLabel#brand { font-size: 22px; font-weight: 700; letter-spacing: -0.5px; }
QLabel#eyebrow { color: #82958e; font-size: 10px; font-weight: 700; letter-spacing: 2px; }
QLabel#title { font-size: 30px; font-weight: 600; letter-spacing: -1px; }
QLabel#subtitle, QLabel#muted { color: #91a39c; }
QLabel#metric { font-size: 28px; font-weight: 600; }
QLabel#section { font-size: 16px; font-weight: 600; }
QLabel#detailTitle { font-size: 23px; font-weight: 600; }
QLabel#badge { color: #8ee2b7; background: #1a342a; border-radius: 5px; padding: 4px 8px; font-size: 10px; font-weight: 600; }
QLabel#banner { color: #d8c6a1; background: transparent; }
QFrame#card, QFrame#detail { background: #192123; border: 1px solid #293436; border-radius: 12px; }
QFrame#card QLabel, QFrame#detail QLabel { background: transparent; }
QFrame#notice { background: #29271e; border: 1px solid #49422e; border-radius: 8px; }
QFrame#notice QWidget { background: transparent; }
QPushButton { background: #232e30; border: 1px solid #354244; border-radius: 7px; padding: 9px 14px; font-weight: 500; }
QPushButton:hover { background: #2c3c3c; border-color: #5c7770; }
QPushButton:focus { border-color: #a8edc9; }
QPushButton:pressed { background: #314844; }
QPushButton:disabled { color: #5e6c67; background: #1a2325; border-color: #263133; }
QPushButton#primary { background: #a8edc9; color: #10281c; border: 1px solid #a8edc9; font-weight: 700; }
QPushButton#primary:hover { background: #c0f8db; }
QPushButton#primary:disabled { background: #355548; color: #819c8e; border-color: #355548; }
QPushButton#danger { color: #f3a79e; background: #352322; border-color: #5b3432; }
QPushButton#nav { text-align: left; padding: 13px 15px; border: none; background: transparent; color: #99aaa3; }
QPushButton#nav:hover { background: #192323; color: #e7eeeb; }
QPushButton#nav:checked { background: #21332e; color: #adf0cd; }
QPushButton#quiet { background: transparent; border: none; color: #a3b6ac; }
QLineEdit, QComboBox, QSpinBox { background: #0f1517; border: 1px solid #354244; border-radius: 7px; padding: 10px; selection-background-color: #426855; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border-color: #a8edc9; }
QComboBox::drop-down { border: none; width: 26px; }
QComboBox QAbstractItemView { background: #1b2628; selection-background-color: #31493d; padding: 6px; }
QListWidget { background: transparent; border: none; outline: none; }
QListWidget::item { background: #192123; border: 1px solid #293436; border-radius: 9px; margin-bottom: 9px; }
QListWidget::item:hover { background: #202c2d; border-color: #4a625a; }
QListWidget::item:selected { background: #20342e; border: 1px solid #7bb497; }
QTableWidget { background: #192123; border: 1px solid #293436; border-radius: 9px; gridline-color: #293436; selection-background-color: #294737; }
QTableWidget::item { padding: 10px; border-bottom: 1px solid #293436; }
QHeaderView::section { background: #151e20; color: #91a39c; border: none; padding: 12px; font-size: 11px; font-weight: 600; }
QPlainTextEdit { background: #0b1113; color: #b9d6c8; border: 1px solid #293436; border-radius: 8px; padding: 12px; font-family: 'Noto Sans Mono', monospace; font-size: 12px; selection-background-color: #365c47; }
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { background: transparent; width: 7px; }
QScrollBar::handle:vertical { background: #3c4c47; border-radius: 3px; min-height: 25px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
QProgressBar { background: #25312d; border: none; border-radius: 3px; max-height: 5px; }
QProgressBar::chunk { background: #a8edc9; border-radius: 3px; }
QToolTip { color: #e7eeeb; background: #263632; border: 1px solid #587063; padding: 7px; }
QMenu { background: #1c2728; border: 1px solid #3a5047; padding: 6px; }
QMenu::item { padding: 9px 24px; border-radius: 4px; }
QMenu::item:selected { background: #304c3e; }
QSplitter::handle { background: transparent; width: 16px; }
QCheckBox { spacing: 8px; background: transparent; }
QCheckBox::indicator { width: 15px; height: 15px; border: 1px solid #60766c; border-radius: 3px; background: #111a17; }
QCheckBox::indicator:checked { background: #a8edc9; border-color: #a8edc9; }
QDialogButtonBox { background: transparent; }
"""


def scaled_style(percent=100):
    """Scale text without scaling layout spacing or control hit targets."""
    factor = max(100, min(150, int(percent))) / 100
    return re.sub(r"font-size: (\d+)px", lambda match:
                  f"font-size: {round(int(match.group(1)) * factor)}px", STYLE)
