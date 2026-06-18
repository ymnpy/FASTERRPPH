import sys
import os
import numpy as np
import warnings
warnings.filterwarnings('ignore')

os.environ['QT_API']             = 'pyqt5'
os.environ['QT_OPENGL']         = 'software'
os.environ['PYVISTA_USE_PANEL'] = '0'

import vtk
vtk.vtkObject.GlobalWarningDisplayOff()

from pyNastran.bdf.bdf import BDF
import pyvista as pv
from pyvistaqt import QtInteractor

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QGroupBox, QFileDialog,
    QSplitter, QComboBox, QProgressBar, QFrame, QCheckBox,
    QSizePolicy, QSlider, QTextEdit
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer

# =============================================================================
# PALETTE  — aerospace dark blue
# =============================================================================
BG         = "#0e1117"
CARD       = "#161b27"
BORDER     = "#1e2736"
ACCENT     = "#3b82f6"
ACCENT_HOV = "#2563eb"
FG         = "#e2e8f0"
FG_DIM     = "#64748b"
FG_MONO    = "#94a3b8"
SUCCESS    = "#22c55e"
WARN       = "#f59e0b"

# =============================================================================
# CONSTANTS
# =============================================================================
COLOR_SHELL      = "#2a3f5f"
COLOR_SHELL_EDGE = "#1e2736"
COLOR_CBUSH      = "#3b82f6"
OPACITY_SHELL    = 0.35

# =============================================================================
# GEOMETRY BUILDERS
# =============================================================================
def _rotation_to_align(v_from: np.ndarray, v_to: np.ndarray) -> np.ndarray:
    """3x3 rotation matrix that rotates unit vector v_from onto unit vector v_to."""
    a, b = v_from, v_to
    cross = np.cross(a, b)
    dot   = np.clip(np.dot(a, b), -1.0, 1.0)
    s     = np.linalg.norm(cross)

    if s < 1e-9:
        if dot > 0:
            return np.eye(3)
        # 180 degree flip - pick any axis perpendicular to a
        ortho = np.array([1.0, 0.0, 0.0])
        if abs(a[0]) > 0.9:
            ortho = np.array([0.0, 1.0, 0.0])
        axis = np.cross(a, ortho)
        axis /= np.linalg.norm(axis)
        K = np.array([[0, -axis[2], axis[1]],
                      [axis[2], 0, -axis[0]],
                      [-axis[1], axis[0], 0]])
        return np.eye(3) + 2 * K @ K

    K = np.array([[0, -cross[2], cross[1]],
                  [cross[2], 0, -cross[0]],
                  [-cross[1], cross[0], 0]])
    return np.eye(3) + K + K @ K * ((1 - dot) / (s ** 2))


def _make_fastener_mesh(p1_arr: np.ndarray, p2_arr: np.ndarray, radius: float,
                        scalar_vals: np.ndarray = None,
                        scalar_name: str = "val") -> pv.PolyData:
    """Build N cylinders, each spanning its own p1->p2 axis (real fastener orientation)."""
    n = len(p1_arr)
    if n == 0:
        return pv.PolyData()

    base_dir = np.array([0.0, 0.0, 1.0])
    template = pv.Cylinder(radius=radius, height=1.0,
                           direction=(0, 0, 1), resolution=16).triangulate()
    tv = template.points          # centered at origin, height 1 along Z
    tf = template.faces.reshape(-1, 4)[:, 1:]
    V, F = len(tv), len(tf)

    all_pts   = np.empty((n * V, 3), dtype=np.float64)
    all_faces = np.empty((n * F, 3), dtype=np.int64)
    if scalar_vals is not None:
        all_sc = np.empty(n * V, dtype=np.float64)

    for i in range(n):
        p1, p2 = p1_arr[i], p2_arr[i]
        axis   = p2 - p1
        length = np.linalg.norm(axis)
        if length < 1e-9:
            length = radius * 2.5
            axis   = base_dir
        direction = axis / length
        center    = (p1 + p2) / 2.0

        R = _rotation_to_align(base_dir, direction)
        scaled    = tv.copy()
        scaled[:, 2] *= length
        rotated   = scaled @ R.T
        world_pts = rotated + center

        all_pts  [i*V:(i+1)*V] = world_pts
        all_faces[i*F:(i+1)*F] = tf + i * V
        if scalar_vals is not None:
            all_sc[i*V:(i+1)*V] = scalar_vals[i]

    face_col  = np.full((n * F, 1), 3, dtype=np.int64)
    faces_pvt = np.hstack([face_col, all_faces]).ravel()
    mesh = pv.PolyData(all_pts, faces_pvt)
    if scalar_vals is not None:
        mesh.point_data[scalar_name] = all_sc
    return mesh


def _cbush_radius(bounds, scale: float = 1.0) -> float:
    """World-unit cylinder radius derived from model bounds."""
    try:
        dims    = [abs(bounds[1]-bounds[0]),
                   abs(bounds[3]-bounds[2]),
                   abs(bounds[5]-bounds[4])]
        nonzero = [d for d in dims if d > 1e-6]
        if not nonzero:
            return 10.0 * scale
        return float(np.clip(min(nonzero) * 0.04 * scale, 0.5, 1000.0))
    except Exception:
        return 10.0 * scale


# =============================================================================
# BDF LOADER THREAD
# =============================================================================
class BDFLoader(QThread):
    done = pyqtSignal(object, str)

    def __init__(self, path):
        super().__init__()
        self.path = path

    def run(self):
        try:
            bdf = BDF()
            bdf.read_bdf(self.path, xref=False)
            self.done.emit(bdf, "")
        except Exception as e:
            self.done.emit(None, str(e))


# =============================================================================
# CALC THREAD  (wire your logic here)
# =============================================================================
class CalcThread(QThread):
    done = pyqtSignal(str, str)

    def __init__(self, xlsm, run_metal, run_composite,
                 df_fastpph, df_joint, bdf_path):
        super().__init__()
        self.xlsm          = xlsm
        self.run_metal     = run_metal
        self.run_composite = run_composite
        self.df_fastpph    = df_fastpph
        self.df_joint      = df_joint
        self.bdf_path      = bdf_path

    def run(self):
        try:
            import xlwings as xw

            out_dir  = os.path.dirname(self.bdf_path) or "."
            out_path = os.path.join(out_dir, "fastener_results.xlsx")

            app = xw.App(visible=False)
            wb  = app.books.open(self.xlsm)
            ws  = wb.sheets["YourSheet"]

            # ... write inputs, calculate, read outputs ...

            # Write results to a fresh workbook
            import pandas as pd
            df_results = pd.DataFrame({...})
            df_results.to_excel(out_path, sheet_name="Results", index=False)

            wb.close()
            app.quit()

            self.done.emit(out_path, "")
        except Exception as e:
            self.done.emit("", str(e))

# =============================================================================
# MAIN WINDOW
# =============================================================================
class FastenerViewer(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Fastener Viewer")
        self.setGeometry(60, 60, 1600, 900)
        self.setMinimumSize(800, 500)

        # ── data ──────────────────────────────────────────────────────────────
        self.bdf            = None
        self.df_fastpph     = None
        self.df_joint       = None
        self.df_output      = None
        self._xlsm_path     = None

        # ── render state ──────────────────────────────────────────────────────
        self._bdf_loader    = None
        self._calc_thread   = None
        self._shell_actor   = None
        self._cbush_actor   = None
        self._rod_actors    = []
        self._pts_cache     = None
        self._nmap_cache    = None
        self._cbush_centers   = {}
        self._cbush_endpoints = {}
        self._scalar_bar    = None
        self._radius_scale  = 1.0
        self._base_radius   = None   # set once after first BDF render
        self._cached_bounds = None   # bounds snapshot taken right after shells render
        self._label_actors  = []
        self._labels_on     = False

        self._init_ui()

        self._progress_timer = QTimer()
        self._progress_timer.timeout.connect(self._swing_progress)
        self._progress_val   = 0
        self._progress_dir   = 1

    # =========================================================================
    # UI
    # =========================================================================
    def _init_ui(self):
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{ background-color:{BG}; color:{FG}; }}
            QSplitter::handle    {{ background-color:{BORDER}; }}
            QScrollBar:vertical  {{ background:{CARD}; width:8px; border-radius:4px; }}
            QScrollBar::handle:vertical {{ background:{BORDER}; border-radius:4px; }}
            QToolTip {{ background:{CARD}; color:{FG}; border:1px solid {BORDER}; }}
        """)
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(2)
        splitter.addWidget(self._build_left())
        splitter.addWidget(self._build_right())
        splitter.setSizes([360, 1240])
        root.addWidget(splitter)

    # -------------------------------------------------------------------------
    # LEFT PANEL
    # -------------------------------------------------------------------------
    def _build_left(self):
        panel = QWidget()
        panel.setMaximumWidth(380)
        panel.setStyleSheet(f"background-color:{CARD};")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        title = QLabel("⬡  Fastener Viewer")
        title.setStyleSheet(
            f"font-size:15px;font-weight:bold;color:{ACCENT};"
            f"padding:6px 0 4px 0;letter-spacing:1px;")
        lay.addWidget(title)

        # ── Input files ───────────────────────────────────────────────────────
        fg = QGroupBox("Input Files")
        fg.setStyleSheet(self._group_style())
        fl = QVBoxLayout(); fl.setSpacing(6)

        for attr, label, filt in [
            ("bdf_edit",  "BDF File:",          "BDF Files (*.bdf);;All (*.*)"),
            ("joint_edit","JOINT CSV:",          "CSV Files (*.csv);;All (*.*)"),
            ("fph_edit",  "fastpph CSV:",        "CSV Files (*.csv);;All (*.*)"),
            ("xlsm_edit", "Calculator (.xlsm):", "Excel Macro (*.xlsm);;All (*.*)"),
            ("out_edit",  "Output XLSX:",        "Excel Files (*.xlsx);;All (*.*)"),
        ]:
            lbl = QLabel(label)
            lbl.setStyleSheet(f"color:{FG_DIM};font-size:10px;")
            fl.addWidget(lbl)
            row  = QHBoxLayout()
            edit = QLineEdit()
            edit.setReadOnly(True)
            edit.setPlaceholderText("Not loaded")
            edit.setStyleSheet(self._edit_style())
            btn  = QPushButton("Browse")
            btn.setFixedWidth(68)
            btn.setStyleSheet(self._btn_style())
            btn.clicked.connect(lambda _, a=attr, f=filt: self._browse_file(a, f))
            row.addWidget(edit)
            row.addWidget(btn)
            fl.addLayout(row)
            setattr(self, attr, edit)

        fg.setLayout(fl)
        lay.addWidget(fg)

        # ── Calculation ───────────────────────────────────────────────────────
        cg = QGroupBox("Calculation")
        cg.setStyleSheet(self._group_style())
        cl = QVBoxLayout(); cl.setSpacing(6)

        # Checkboxes side by side
        chk_row = QHBoxLayout()
        chk_ss = f"font-size:11px;color:{FG};"
        self.chk_metal     = QCheckBox("Metallic")
        self.chk_composite = QCheckBox("Composite")
        self.chk_metal.setChecked(True)
        self.chk_composite.setChecked(True)
        self.chk_metal.setStyleSheet(chk_ss)
        self.chk_composite.setStyleSheet(chk_ss)
        chk_row.addWidget(self.chk_metal)
        chk_row.addWidget(self.chk_composite)
        chk_row.addStretch()
        cl.addLayout(chk_row)

        # Progress bar always visible, inside the group
        cl.addSpacing(15)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFixedHeight(20)
        self.progress.setTextVisible(False)
        self.progress.setStyleSheet(f"""
            QProgressBar       {{ border:none; background:{BORDER}; border-radius:2px; }}
            QProgressBar::chunk {{ background:{ACCENT}; border-radius:2px; }}
        """)
        cl.addWidget(self.progress)

        # Gap then Calculate
        cl.addSpacing(15)
        self.calc_btn = QPushButton("Calculate")
        self.calc_btn.setStyleSheet(f"""
            QPushButton {{
                background-color:{ACCENT}; color:white; border:none;
                border-radius:4px; padding:7px; font-weight:bold; font-size:11px;
            }}
            QPushButton:hover    {{ background-color:{ACCENT_HOV}; }}
            QPushButton:disabled {{ background-color:{BORDER}; color:{FG_DIM}; }}
        """)
        self.calc_btn.clicked.connect(self._on_calculate)
        cl.addWidget(self.calc_btn)

        cg.setLayout(cl)
        lay.addWidget(cg)

        # Status label below the group
        self.status_lbl = QLabel("Load a BDF file to begin.")
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setStyleSheet(f"color:{FG_DIM};font-size:10px;padding:2px 0;")
        lay.addWidget(self.status_lbl)

        # ── LOG ──────────────────────────────────────────────────────────────
        lay.addStretch()
        log_hdr_row = QHBoxLayout()
        log_hdr = QLabel("Log")
        log_hdr.setStyleSheet(f"font-weight:bold;font-size:10px;margin-top:4px;color:{FG};")
        self.log_toggle_btn = QPushButton("▼")
        self.log_toggle_btn.setFixedSize(20, 20)
        self.log_toggle_btn.setToolTip("Collapse log")
        self.log_toggle_btn.setStyleSheet(f"""
            QPushButton {{
                border:1px solid {BORDER}; border-radius:3px;
                background:{BG}; color:{FG}; font-size:10px;
            }}
            QPushButton:hover {{ background:{ACCENT}; color:white; border-color:{ACCENT}; }}
        """)
        self.log_toggle_btn.clicked.connect(self._toggle_log)
        log_hdr_row.addWidget(log_hdr)
        log_hdr_row.addStretch()
        log_hdr_row.addWidget(self.log_toggle_btn)
        lay.addLayout(log_hdr_row)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(600)
        self.log_text.setStyleSheet(f"""
            QTextEdit {{
                background:{BG}; color:{FG_MONO};
                border:1px solid {BORDER}; border-radius:4px;
                font-family:monospace; font-size:10px;
                padding:4px;
            }}
        """)
        lay.addWidget(self.log_text, stretch=1)

        return panel

    # -------------------------------------------------------------------------
    # RIGHT PANEL
    # -------------------------------------------------------------------------
    def _build_right(self):
        panel = QWidget()
        panel.setStyleSheet(f"background:{BG};")
        lay   = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        try:
            self.plotter = QtInteractor(panel)
            self.plotter.set_background(BG)
            self.plotter.add_axes(color=FG_DIM, viewport=(0.0, 0.0, 0.10, 0.13))
            self.plotter.track_click_position(callback=self._on_click, side='right')
            lay.addWidget(self.plotter.interactor)
        except Exception as e:
            self.plotter = None
            lay.addWidget(QLabel(f"Viewport error: {e}"))

        lay.addWidget(self._build_bottom_bar())
        return panel

    def _build_bottom_bar(self):
        bar = QWidget()
        bar.setFixedHeight(52)
        bar.setStyleSheet(f"""
            QWidget {{ background-color:{CARD}; border-top:1px solid {BORDER}; }}
            QLabel  {{ font-size:10px; color:{FG_DIM}; background:transparent; }}
            QComboBox {{
                border:1px solid {BORDER}; border-radius:3px;
                padding:3px 7px; font-size:10px;
                background:{BG}; color:{FG};
            }}
            QComboBox::drop-down {{ border:none; }}
            QComboBox QAbstractItemView {{
                background:{CARD}; color:{FG}; border:1px solid {BORDER};
                selection-background-color:{ACCENT};
            }}
            QPushButton {{
                border:1px solid {BORDER}; border-radius:3px;
                padding:4px 10px; font-size:10px;
                background:{BG}; color:{FG}; font-weight:600;
            }}
            QPushButton:hover    {{ background:{ACCENT};  color:white; border-color:{ACCENT}; }}
            QPushButton:checked  {{ background:{ACCENT};  color:white; border-color:{ACCENT}; }}
            QPushButton:!checked {{ background:{BG};      color:{FG};  border-color:{BORDER}; }}
            QSlider::groove:horizontal {{
                height:4px; background:{BORDER}; border-radius:2px;
            }}
            QSlider::handle:horizontal {{
                background:{ACCENT}; border:none;
                width:12px; height:12px; margin:-4px 0; border-radius:6px;
            }}
            QSlider::sub-page:horizontal {{
                background:{ACCENT}; border-radius:2px;
            }}
        """)
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(12, 6, 12, 6)
        bl.setSpacing(8)

        # ── Color by ──────────────────────────────────────────────────────────
        bl.addWidget(self._bar_label("Color by:"))
        self.col_combo = QComboBox()
        self.col_combo.setFixedWidth(170)
        self.col_combo.addItem("Default")
        self.col_combo.setEnabled(False)
        self.col_combo.currentIndexChanged.connect(self._on_col_changed)
        bl.addWidget(self.col_combo)

        apply_btn = QPushButton("Apply")
        apply_btn.setFixedWidth(56)
        apply_btn.clicked.connect(self._recolor_cbush)
        bl.addWidget(apply_btn)

        bl.addWidget(self._vsep())

        # ── Labels toggle ─────────────────────────────────────────────────────
        self.label_btn = QPushButton("Labels: OFF")
        self.label_btn.setFixedWidth(90)
        self.label_btn.setCheckable(True)
        self.label_btn.setChecked(False)
        self.label_btn.clicked.connect(self._on_toggle_labels)
        bl.addWidget(self.label_btn)

        bl.addWidget(self._vsep())

        # ── Opacity slider ────────────────────────────────────────────────────
        bl.addWidget(self._bar_label("Opacity:"))
        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(5, 100)
        self.opacity_slider.setValue(100)
        self.opacity_slider.setFixedWidth(80)
        self.opacity_slider.valueChanged.connect(self._on_opacity_changed)
        bl.addWidget(self.opacity_slider)
        self.opacity_val_lbl = self._bar_label("100%")
        self.opacity_val_lbl.setFixedWidth(32)
        bl.addWidget(self.opacity_val_lbl)

        bl.addWidget(self._vsep())

        # ── Radius slider ─────────────────────────────────────────────────────
        bl.addWidget(self._bar_label("Radius:"))
        self.radius_slider = QSlider(Qt.Horizontal)
        self.radius_slider.setRange(25, 400)
        self.radius_slider.setValue(100)
        self.radius_slider.setFixedWidth(80)
        self.radius_slider.valueChanged.connect(self._on_radius_changed)
        bl.addWidget(self.radius_slider)
        self.radius_val_lbl = self._bar_label("1.0×")
        self.radius_val_lbl.setFixedWidth(32)
        bl.addWidget(self.radius_val_lbl)

        bl.addWidget(self._vsep())

        # ── Pick info — pinned to bottom-right ────────────────────────────────
        bl.addStretch()
        self.pick_lbl = QLabel("")
        self.pick_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.pick_lbl.setStyleSheet(
            f"color:{FG_MONO};font-size:10px;font-family:monospace;background:transparent;")
        bl.addWidget(self.pick_lbl)

        return bar

    # ── helpers ───────────────────────────────────────────────────────────────
    def _bar_label(self, text):
        lbl = QLabel(text)
        lbl.setStyleSheet("background:transparent;")
        return lbl

    def _vsep(self):
        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setStyleSheet(f"background:{BORDER}; max-width:1px;")
        return sep

    # =========================================================================
    # STYLES
    # =========================================================================
    def _group_style(self):
        return f"""
        QGroupBox {{
            background-color:{CARD};
            border:1px solid {BORDER};
            border-top:2px solid {ACCENT};
            border-radius:5px;
            margin-top:10px; padding-top:12px;
            font-weight:bold; font-size:10px; color:{FG};
        }}
        QGroupBox::title {{
            subcontrol-origin:margin; subcontrol-position:top left;
            left:10px; padding:0 4px; color:{ACCENT};
        }}
        QLabel    {{ font-size:11px; color:{FG}; background:transparent; }}
        QCheckBox {{ font-size:11px; color:{FG}; background:transparent; }}
        QCheckBox::indicator {{
            width:13px; height:13px; border:1px solid {BORDER};
            border-radius:3px; background:{BG};
        }}
        QCheckBox::indicator:checked {{ background:{ACCENT}; border-color:{ACCENT}; }}
        """

    def _edit_style(self):
        return (f"border:1px solid {BORDER}; border-radius:4px;"
                f"padding:4px 7px; font-size:11px; background:{BG}; color:{FG};")

    def _btn_style(self):
        return (f"border:1px solid {BORDER}; border-radius:4px;"
                f"padding:5px 8px; font-size:10px;"
                f"background:{BG}; color:{FG}; font-weight:600;")

    # =========================================================================
    # PROGRESS
    # =========================================================================
    def _toggle_log(self):
        visible = self.log_text.isVisible()
        self.log_text.setVisible(not visible)
        self.log_toggle_btn.setText("▼" if visible else "▶")
        self.log_toggle_btn.setToolTip("Expand log" if visible else "Collapse log")

    def _log(self, msg):
        import datetime
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_text.append(f"[{ts}] {msg}")

    def _start_busy(self, msg="Working..."):
        self.status_lbl.setText(msg)
        self._log(msg)
        self._progress_val = 0
        self._progress_dir = 1
        self._progress_timer.start(30)
        QApplication.processEvents()

    def _stop_busy(self, msg=""):
        self._progress_timer.stop()
        self.progress.setValue(0)
        if msg:
            self.status_lbl.setText(msg)
            self._log(msg)
        QApplication.processEvents()

    def _swing_progress(self):
        self._progress_val += self._progress_dir * 3
        if   self._progress_val >= 100: self._progress_val = 100; self._progress_dir = -1
        elif self._progress_val <=   0: self._progress_val =   0; self._progress_dir =  1
        self.progress.setValue(self._progress_val)

    # =========================================================================
    # FILE BROWSING
    # =========================================================================
    def _browse_file(self, attr, filt):
        fn, _ = QFileDialog.getOpenFileName(self, "Select File", "", filt)
        if not fn:
            return
        edit = getattr(self, attr)
        edit.setText(os.path.basename(fn))
        edit.setToolTip(fn)

        if   attr == "bdf_edit":   self._load_bdf(fn)
        elif attr == "fph_edit":   self._load_fastpph(fn)
        elif attr == "out_edit":   self._load_output(fn)
        elif attr == "joint_edit": self._load_joint(fn)
        elif attr == "xlsm_edit":  self._xlsm_path = fn

    # =========================================================================
    # BDF
    # =========================================================================
    def _load_bdf(self, fn):
        self._start_busy("Loading BDF...")
        self._bdf_loader = BDFLoader(fn)
        self._bdf_loader.done.connect(self._on_bdf_loaded)
        self._bdf_loader.start()

    def _on_bdf_loaded(self, bdf, err):
        if err:
            self._stop_busy(f"Error: {err}")
            return
        self.bdf = bdf
        self._pts_cache  = None
        self._nmap_cache = None
        n_sh = sum(1 for e in bdf.elements.values() if e.type in ("CQUAD4", "CTRIA3"))
        n_cb = sum(1 for e in bdf.elements.values() if e.type == "CBUSH")
        self._render_bdf()
        self._stop_busy(f"BDF loaded — {n_sh} shells, {n_cb} CBUSH")

    def _build_pts(self):
        if self._pts_cache is None:
            nodes = []
            nmap  = {}
            for nid, node in self.bdf.nodes.items():
                nmap[nid] = len(nodes)
                nodes.append(node.get_position())
            self._pts_cache  = np.array(nodes, dtype=float)
            self._nmap_cache = nmap
        return self._pts_cache, self._nmap_cache

    # =========================================================================
    # RENDER BDF
    # =========================================================================
    def _render_bdf(self):
        if not self.plotter or not self.bdf:
            return

        self.plotter.clear()
        self._shell_actor   = None
        self._cbush_actor   = None
        self._rod_actors    = []
        self._scalar_bar    = None
        self._label_actors  = []
        self._cached_bounds = None
        self._base_radius   = None

        pts, nmap = self._build_pts()

        # ── Shells ────────────────────────────────────────────────────────────
        sh_cells = []
        for elem in self.bdf.elements.values():
            try:
                if elem.type == "CQUAD4":
                    nids = elem.node_ids[:4]
                    if all(n in nmap for n in nids):
                        sh_cells.append([4] + [nmap[n] for n in nids])
                elif elem.type == "CTRIA3":
                    nids = elem.node_ids[:3]
                    if all(n in nmap for n in nids):
                        sh_cells.append([3] + [nmap[n] for n in nids])
            except Exception:
                continue

        if sh_cells:
            mesh = pv.PolyData(pts, np.hstack(sh_cells))
            self._shell_actor = self.plotter.add_mesh(
                mesh, color=COLOR_SHELL, show_edges=True,
                edge_color=COLOR_SHELL_EDGE, opacity=OPACITY_SHELL,
                pickable=False, show_scalar_bar=False)

        # ── 1-D elements ──────────────────────────────────────────────────────
        ONE_D = ("CROD", "CBAR", "CBEAM", "CBUSH1D", "CONROD")
        for elem in self.bdf.elements.values():
            if elem.type not in ONE_D:
                continue
            try:
                nids = elem.node_ids[:2]
                if not all(n in self.bdf.nodes for n in nids):
                    continue
                p1 = np.array(self.bdf.nodes[nids[0]].get_position(), dtype=float)
                p2 = np.array(self.bdf.nodes[nids[1]].get_position(), dtype=float)
                actor = self.plotter.add_mesh(
                    pv.Line(p1, p2), color=FG_MONO, line_width=1,
                    pickable=False, show_scalar_bar=False)
                self._rod_actors.append(actor)
            except Exception:
                continue

        # ── CBUSH endpoints + midpoints ─────────────────────────────────────────
        self._cbush_centers   = {}   # eid -> midpoint (used for picking/labels)
        self._cbush_endpoints = {}   # eid -> (p1, p2)  (used for true axis orientation)
        for eid, elem in self.bdf.elements.items():
            if elem.type != "CBUSH":
                continue
            try:
                nids = elem.node_ids[:2]
                if not all(n in self.bdf.nodes for n in nids):
                    continue
                p1 = np.array(self.bdf.nodes[nids[0]].get_position(), dtype=float)
                p2 = np.array(self.bdf.nodes[nids[1]].get_position(), dtype=float)
                self._cbush_centers[eid]   = (p1 + p2) / 2.0
                self._cbush_endpoints[eid] = (p1, p2)
            except Exception:
                continue

        self.plotter.reset_camera()
        self.plotter.render()

        # Cache bounds BEFORE adding CBUSH actors (avoids inflated bounds later)
        try:
            self._cached_bounds = tuple(self.plotter.bounds)
        except Exception:
            self._cached_bounds = None

        self._render_cbush_default()
        self.plotter.render()

    # =========================================================================
    # CBUSH DEFAULT RENDER
    # =========================================================================
    def _render_cbush_default(self):
        if not self._cbush_endpoints:
            return
        eids = list(self._cbush_endpoints.keys())
        p1s  = np.array([self._cbush_endpoints[e][0] for e in eids], dtype=float)
        p2s  = np.array([self._cbush_endpoints[e][1] for e in eids], dtype=float)
        # Store base radius (scale=1) so slider multiplications stay consistent
        self._base_radius = _cbush_radius(self._cached_bounds, 1.0)
        r  = self._base_radius * self._radius_scale
        op = self.opacity_slider.value() / 100.0
        mesh = _make_fastener_mesh(p1s, p2s, r)
        self._cbush_actor = self.plotter.add_mesh(
            mesh, color=COLOR_CBUSH, smooth_shading=True,
            opacity=op, pickable=True, show_scalar_bar=False)

    # =========================================================================
    # OPACITY SLIDER  — controls shell transparency so fasteners show through
    # =========================================================================
    def _on_opacity_changed(self, val):
        self.opacity_val_lbl.setText(f"{val}%")
        if self._shell_actor:
            try:
                self._shell_actor.GetProperty().SetOpacity(val / 100.0)
            except Exception:
                pass
        if self.plotter:
            self.plotter.render()

    # =========================================================================
    # RADIUS SLIDER  — full mesh rebuild at new scale
    # =========================================================================
    def _on_radius_changed(self, val):
        self._radius_scale = val / 100.0
        self.radius_val_lbl.setText(f"{self._radius_scale:.2f}×")
        self._recolor_cbush()

    # =========================================================================
    # FASTPPH CSV
    # =========================================================================
    def _load_fastpph(self, fn):
        import pandas as pd
        self._start_busy("Loading fastpph...")
        try:
            df = pd.read_csv(fn, skiprows=2, sep=None, engine="python")
            df.columns = df.columns.str.strip()
            df.rename(columns={"elem 1 id": "Element ID"}, inplace=True)
            df["Element ID"] = pd.to_numeric(
                df["Element ID"], errors="coerce").astype("Int64")
            self.df_fastpph = df
            self._update_col_combo()
            self._stop_busy(f"fastpph loaded — {len(df)} rows")
        except Exception as e:
            self._stop_busy(f"fastpph error: {e}")

    # =========================================================================
    # JOINT CSV
    # =========================================================================
    def _load_joint(self, fn):
        import pandas as pd
        self._start_busy("Loading JOINT CSV...")
        try:
            df = pd.read_csv(fn)
            df.columns = df.columns.str.strip()
            self.df_joint = df
            self._stop_busy(f"JOINT CSV loaded — {len(df)} rows")
        except Exception as e:
            self._stop_busy(f"JOINT CSV error: {e}")

    # =========================================================================
    # OUTPUT XLSX
    # =========================================================================
    def _load_output(self, fn):
        import pandas as pd
        self._start_busy("Loading output xlsx...")
        try:
            df = pd.read_excel(fn, sheet_name="Results")
            df.columns = df.columns.str.strip()
            df["Element ID"] = pd.to_numeric(
                df["Element ID"], errors="coerce").astype("Int64")
            
            # Only keep what you want
            keep = ["Element ID", "RF", "Allowable", "Applied", "MS_tension", "MS_shear"]
            self.df_output = df[[c for c in keep if c in df.columns]]
            
            self._update_col_combo()
            self._stop_busy(f"Output loaded — {len(self.df_output)} rows")
            if self.bdf:
                self._recolor_cbush()
        except Exception as e:
            self._stop_busy(f"Output error: {e}")

    # =========================================================================
    # COLUMN COMBO
    # =========================================================================
    def _update_col_combo(self):
        prev = self.col_combo.currentText()
        self.col_combo.blockSignals(True)
        self.col_combo.clear()
        self.col_combo.addItem("Default")

        numeric_cols  = ["Fx", "Fy", "Fz"]
        discrete_cols = ["Diameter", "Fastener Name"]

        if self.df_fastpph is not None:
            for c in numeric_cols:
                if c in self.df_fastpph.columns:
                    self.col_combo.addItem(f"[fastpph] {c}")
            for c in discrete_cols:
                if c in self.df_fastpph.columns:
                    self.col_combo.addItem(f"[fastpph] {c}")

        if self.df_output is not None:
            for c in self.df_output.columns:
                if c != "Element ID":
                    self.col_combo.addItem(f"[output] {c}")

        self.col_combo.setEnabled(True)
        idx = self.col_combo.findText(prev)
        self.col_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.col_combo.blockSignals(False)

    def _on_col_changed(self):
        pass   # user presses Apply to commit

    # =========================================================================
    # LABEL TOGGLE
    # =========================================================================
    def _on_toggle_labels(self, checked):
        self._labels_on = checked
        self.label_btn.setText("Labels: ON" if checked else "Labels: OFF")
        self._refresh_labels()

    def _refresh_labels(self):
        if not self.plotter:
            return
        for a in self._label_actors:
            try:
                self.plotter.remove_actor(a)
            except Exception:
                pass
        self._label_actors = []

        if not self._labels_on or not self._cbush_centers:
            self.plotter.render()
            return

        col_text   = self.col_combo.currentText()
        eids       = list(self._cbush_centers.keys())
        eid_to_val = {}

        if col_text != "Default":
            if col_text.startswith("[fastpph]"):
                col = col_text.replace("[fastpph] ", "").strip()
                df  = self.df_fastpph
            else:
                col = col_text.replace("[output] ", "").strip()
                df  = self.df_output

            if df is not None and "Element ID" in df.columns and col in df.columns:
                for _, row in df.iterrows():
                    try:
                        eid_to_val[int(row["Element ID"])] = row[col]
                    except Exception:
                        pass

        r = (self._base_radius or _cbush_radius(self._cached_bounds, 1.0)) * self._radius_scale

        for eid in eids:
            pos       = self._cbush_centers[eid]
            label_pos = pos + np.array([0, 0, r * 2])

            if col_text != "Default" and eid in eid_to_val:
                v    = eid_to_val[eid]
                text = f"{v:.3g}" if isinstance(v, float) else str(v)
            else:
                text = str(eid)

            try:
                actor = self.plotter.add_point_labels(
                    [label_pos], [text],
                    font_size=9, text_color=FG,
                    point_color=FG, point_size=0,
                    shape=None, render_points_as_spheres=False,
                    always_visible=True, shadow=False, pickable=False)
                self._label_actors.append(actor)
            except Exception:
                pass

        self.plotter.render()

    # =========================================================================
    # RECOLOR CBUSH  — central rebuild used by Apply, radius slider, output load
    # CBUSH cylinders always render fully opaque; the Opacity slider controls
    # the shell mesh instead, so fasteners stay clearly visible through it.
    # =========================================================================
    def _recolor_cbush(self):
        if not self.plotter or not self._cbush_endpoints:
            return

        if self._cbush_actor is not None:
            self.plotter.remove_actor(self._cbush_actor)
            self._cbush_actor = None
        if self._scalar_bar is not None:
            try:
                self.plotter.remove_scalar_bar()
            except Exception:
                pass
            self._scalar_bar = None

        col_text = self.col_combo.currentText()
        eids = list(self._cbush_endpoints.keys())
        p1s  = np.array([self._cbush_endpoints[e][0] for e in eids], dtype=float)
        p2s  = np.array([self._cbush_endpoints[e][1] for e in eids], dtype=float)
        r    = (self._base_radius or _cbush_radius(self._cached_bounds, 1.0)) * self._radius_scale

        # ── Default solid color ───────────────────────────────────────────────
        if col_text == "Default":
            mesh = _make_fastener_mesh(p1s, p2s, r)
            self._cbush_actor = self.plotter.add_mesh(
                mesh, color=COLOR_CBUSH, smooth_shading=True,
                opacity=1.0, pickable=True, show_scalar_bar=False)
            self.plotter.render()
            self._refresh_labels()
            return

        # ── Resolve column + dataframe ─────────────────────────────────────
        if col_text.startswith("[fastpph]"):
            col = col_text.replace("[fastpph] ", "").strip()
            df  = self.df_fastpph
        else:
            col = col_text.replace("[output] ", "").strip()
            df  = self.df_output

        if df is None or "Element ID" not in df.columns or col not in df.columns:
            self._stop_busy(f"Column '{col}' not found")
            return

        eid_to_val     = dict(zip(df["Element ID"].astype(int), df[col]))
        force_discrete = (col == "Diameter")
        vals           = [eid_to_val.get(e) for e in eids]
        is_num         = (not force_discrete) and all(
            isinstance(v, (int, float)) and not (isinstance(v, float) and np.isnan(v))
            for v in vals if v is not None)

        # ── Numeric (continuous colormap) ──────────────────────────────────
        if is_num:
            scalar_arr = np.array([
                float(eid_to_val[e]) if e in eid_to_val else np.nan
                for e in eids], dtype=float)
            valid = scalar_arr[~np.isnan(scalar_arr)]
            clim  = [float(valid.min()), float(valid.max())] if len(valid) else [0.0, 1.0]
            mesh  = _make_fastener_mesh(p1s, p2s, r, scalar_arr, col)
            self._cbush_actor = self.plotter.add_mesh(
                mesh, scalars=col, cmap="coolwarm",
                smooth_shading=True, opacity=1.0,
                clim=clim, pickable=True, show_scalar_bar=True,
                scalar_bar_args=dict(
                    title=col, vertical=True,
                    position_x=0.88, position_y=0.10,
                    width=0.04, height=0.70, n_labels=5,
                    color=FG, title_font_size=11, label_font_size=10))
            self._scalar_bar = col

        # ── Discrete / categorical ────────────────────────────────────────
        else:
            unique  = sorted({str(eid_to_val[e]) for e in eids if e in eid_to_val})
            palette = [ACCENT, "#22c55e", "#f59e0b", "#ef4444",
                       "#a855f7", "#06b6d4", "#f97316", "#84cc16",
                       "#ec4899", "#14b8a6", "#fb923c", "#a3e635"]
            v2c  = {v: palette[i % len(palette)] for i, v in enumerate(unique)}
            mesh = _make_fastener_mesh(p1s, p2s, r)
            V    = mesh.n_points // len(eids) if eids else 1
            per_pt_rgb = []
            for e in eids:
                hex_c = v2c.get(str(eid_to_val.get(e, "")), COLOR_CBUSH)
                per_pt_rgb.extend([np.array(pv.Color(hex_c).float_rgb)] * V)
            mesh.point_data["rgb"] = np.array(per_pt_rgb, dtype=float)
            self._cbush_actor = self.plotter.add_mesh(
                mesh, scalars="rgb", rgb=True,
                smooth_shading=True, opacity=1.0,
                pickable=True, show_scalar_bar=False)

        self.plotter.render()
        self._refresh_labels()
        self.status_lbl.setText(f"Colored by: {col}")

    # =========================================================================
    # CALCULATE
    # =========================================================================
    def _on_calculate(self):
        if not self.chk_metal.isChecked() and not self.chk_composite.isChecked():
            self.status_lbl.setText("Select at least one: Metallic or Composite")
            return
        xlsm = self._xlsm_path or self.xlsm_edit.toolTip()
        if not xlsm or not os.path.exists(xlsm):
            self.status_lbl.setText("Please browse a .xlsm calculator file first")
            return
        self.calc_btn.setEnabled(False)
        self._start_busy("Calculating... please wait")
        self._calc_thread = CalcThread(
            xlsm=xlsm,
            run_metal=self.chk_metal.isChecked(),
            run_composite=self.chk_composite.isChecked(),
            df_fastpph=self.df_fastpph,
            df_joint=self.df_joint,
            bdf_path=self.bdf_edit.toolTip())
        self._calc_thread.done.connect(self._on_calc_done)
        self._calc_thread.start()

    def _on_calc_done(self, result_path, err):
        self.calc_btn.setEnabled(True)
        if err:
            self._stop_busy(f"Calculation error: {err}")
            return
        self._stop_busy(f"Done — {result_path}")
        if result_path and os.path.exists(result_path):
            self._load_output(result_path)

    # =========================================================================
    # RIGHT-CLICK PICK
    # =========================================================================
    def _on_click(self, pos):
        if not pos or not self._cbush_centers:
            return
        pos   = np.array(pos, dtype=float)
        eids  = list(self._cbush_centers.keys())
        cpts  = np.array([self._cbush_centers[e] for e in eids], dtype=float)
        dists = np.linalg.norm(cpts - pos, axis=1)
        idx   = int(np.argmin(dists))

        try:
            b    = self.plotter.bounds
            span = max(abs(b[1]-b[0]), abs(b[3]-b[2]), abs(b[5]-b[4]))
        except Exception:
            span = 1000
        if dists[idx] > span * 0.03:
            return

        eid      = eids[idx]
        col_text = self.col_combo.currentText()

        if col_text != "Default":
            if col_text.startswith("[fastpph]"):
                col_name = col_text.replace("[fastpph] ", "").strip()
                df = self.df_fastpph
            else:
                col_name = col_text.replace("[output] ", "").strip()
                df = self.df_output

            if df is not None and "Element ID" in df.columns and col_name in df.columns:
                row = df[df["Element ID"].astype(int) == eid]
                if not row.empty:
                    v       = row.iloc[0][col_name]
                    val_str = f"{v:.4g}" if isinstance(v, float) else str(v)
                    text    = f"EID {eid}  {col_name}: {val_str}"
                    self.pick_lbl.setText(text)
                    self._log(text)
                    return

        text = f"EID {eid}"
        self.pick_lbl.setText(text)
        self._log(text)

    # =========================================================================
    # CLOSE
    # =========================================================================
    def closeEvent(self, event):
        if self.plotter:
            self.plotter.close()
        super().closeEvent(event)


# =============================================================================
# ENTRY POINT
# =============================================================================
def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = FastenerViewer()
    win.showMaximized()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
