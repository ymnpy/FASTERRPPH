"""
Fastener Viewer
===============
PyQt5 + PyVista 3-D viewer for Nastran BDF models with integrated
fastener joint calculation via xlwings.

Thread model
------------
  Main thread  : Qt event loop + all widget mutations.
  BDFLoader    : reads BDF file, emits done() signal → picked up by main thread.
  CalcThread   : all xlwings / pandas work, emits done() signal → main thread.
  QTimer       : progress bar animation on main thread only.

All signals crossing thread boundaries use Qt's queued connection
(automatic for cross-thread signals), so no direct widget access ever
happens from a worker thread.
"""

import os
import sys
from collections import Counter
from datetime import datetime

import numpy as np
import warnings
warnings.filterwarnings("ignore")

os.environ["QT_API"]             = "pyqt5"
os.environ["QT_OPENGL"]         = "software"
os.environ["PYVISTA_USE_PANEL"] = "0"

import vtk
vtk.vtkObject.GlobalWarningDisplayOff()

import pandas as pd
import pyvista as pv
from pyNastran.bdf.bdf import BDF
from pyvistaqt import QtInteractor

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QGroupBox, QFileDialog,
    QSplitter, QComboBox, QProgressBar, QFrame, QCheckBox,
    QSizePolicy, QSlider, QTextEdit,
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer

# =============================================================================
# PALETTE  — aerospace dark blue / light
# =============================================================================
PALETTE_DARK = dict(
    BG="#0e1117", CARD="#161b27", BORDER="#1e2736",
    ACCENT="#3b82f6", ACCENT_HOV="#2563eb",
    FG="#e2e8f0", FG_DIM="#64748b", FG_MONO="#94a3b8",
    SUCCESS="#22c55e", WARN="#f59e0b",
    COLOR_SHELL="#2a3f5f", COLOR_SHELL_EDGE="#1e2736",
)
PALETTE_LIGHT = dict(
    BG="#f8fafc", CARD="#ffffff", BORDER="#e2e8f0",
    ACCENT="#3b82f6", ACCENT_HOV="#2563eb",
    FG="#1e293b", FG_DIM="#64748b", FG_MONO="#475569",
    SUCCESS="#16a34a", WARN="#d97706",
    COLOR_SHELL="#94a3b8", COLOR_SHELL_EDGE="#cbd5e1",
)

# Runtime-mutable globals (mutated only by _apply_theme on the main thread)
BG = PALETTE_DARK["BG"]; CARD = PALETTE_DARK["CARD"]; BORDER = PALETTE_DARK["BORDER"]
ACCENT = PALETTE_DARK["ACCENT"]; ACCENT_HOV = PALETTE_DARK["ACCENT_HOV"]
FG = PALETTE_DARK["FG"]; FG_DIM = PALETTE_DARK["FG_DIM"]; FG_MONO = PALETTE_DARK["FG_MONO"]
SUCCESS = PALETTE_DARK["SUCCESS"]; WARN = PALETTE_DARK["WARN"]
COLOR_SHELL = PALETTE_DARK["COLOR_SHELL"]; COLOR_SHELL_EDGE = PALETTE_DARK["COLOR_SHELL_EDGE"]
COLOR_CBUSH   = "#3b82f6"
OPACITY_SHELL = 0.35

# =============================================================================
# SHEET / COLUMN CONFIGURATION
# =============================================================================
METAL_SHEET     = "Metal_Joint_Calculation"
COMPOSITE_SHEET = "Composite_Joint_Calculation"

INFO_COLS = [
    "Component Name", "Element ID", "elem 1 Node id",
    "elem 2 id", "elem 2 Node id", "box dimension",
    "file Name", "LoadCase Name",
]
DATA_COLS_METAL = [
    "Fx", "Fy", "Fz",
    "Nx bypass", "Ny bypass", "Nxy bypass",
    "n1", "n2", "n3", "n4",
]
DATA_COLS_COMPOSITE = [
    "Fx", "Fy", "Fz",
    "Nx bypass", "Ny bypass", "Nxy bypass",
    "n1", "n2", "n3", "n4",
]
LOAD_COLS = [
    "Fx", "Fy", "Fz",
    "Nx bypass", "Ny bypass", "Nxy bypass",
    "Mx total",  "My total",  "Mxy total",
]
GROUP_KEYS = [
    "Component Name", "Element ID", "elem 1 Node id",
    "file Name", "LoadCase Name",
]
# Columns that must survive groupby for INFO_COLS downstream
AGG_FIRST_EXTRA = [
    "elem 2 id", "elem 2 Node id", "box dimension",
    "Fastener Diameter", "Pitch",
]

# =============================================================================
# COLUMN DISPLAY CONFIGURATION
# =============================================================================
COLUMN_CONFIG = [
    # (display_name,      source_df_attr,  discrete)
    ("Fastener Diameter", "df_joint",      True),
    ("Fastener Name",     "df_joint",      True),
    ("Fx",                "df_fastpph",    False),
    ("Fy",                "df_fastpph",    False),
    ("Fz",                "df_fastpph",    False),
    ("RF",                "df_output",     False),
]

# =============================================================================
# PURE HELPERS
# =============================================================================
def _col_letter(col_index: int) -> str:
    """1-based column index → Excel letter(s). No xlwings utils dependency."""
    result = ""
    while col_index > 0:
        col_index, rem = divmod(col_index - 1, 26)
        result = chr(65 + rem) + result
    return result


def parse_comp_name(comp_name: str):
    """
    'PSHELL_123' → ('PSHELL', 123).
    Returns (None, None) on any parse failure — callers must guard.
    """
    try:
        parts = str(comp_name).strip().split("_")
        if len(parts) < 2:
            raise ValueError
        return parts[0].upper(), int(parts[-1])
    except (ValueError, IndexError):
        return None, None


def get_property_info(comp_name: str, bdf: BDF):
    """
    Extract (mat_id, mat_name, n1, n2, n3, n4) from BDF property.
    All fields are None on any failure — callers receive a warning print.
    """
    prop_type, pid = parse_comp_name(comp_name)
    mat_id = mat = n1 = n2 = n3 = n4 = None

    if prop_type is None:
        print(f"WARNING: Cannot parse Component Name '{comp_name}' — skipping.")
        return mat_id, mat, n1, n2, n3, n4

    prop = bdf.properties.get(pid)
    if prop is None:
        print(f"WARNING: Property {pid} not found in BDF — skipping.")
        return mat_id, mat, n1, n2, n3, n4

    try:
        if prop_type == "PSHELL":
            t      = prop.t
            mat_id = prop.mid
            mat    = "Al-2024"
            total  = round(t / 0.1)
            base   = total // 4
            rem    = total % 4
            n1     = base + rem
            n2 = n3 = n4 = base

        elif prop_type == "PCOMP":
            mat_id = prop.mids[0]
            mat    = "TT84"
            counts = Counter(prop.thetas)
            n1 = counts.get(0,   0)
            n2 = counts.get(45,  0)
            n3 = counts.get(-45, 0)
            n4 = counts.get(90,  0)

    except Exception as exc:
        print(f"WARNING: Error reading property {pid}: {exc}")

    return mat_id, mat, n1, n2, n3, n4


# =============================================================================
# GEOMETRY BUILDERS
# =============================================================================
def _rotation_to_align(v_from: np.ndarray, v_to: np.ndarray) -> np.ndarray:
    """3×3 rotation matrix that rotates unit vector v_from onto v_to."""
    a, b  = v_from, v_to
    cross = np.cross(a, b)
    dot   = np.clip(np.dot(a, b), -1.0, 1.0)
    s     = np.linalg.norm(cross)

    if s < 1e-9:
        if dot > 0:
            return np.eye(3)
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


# Cylinder resolution — fixed so per-cylinder vertex count V is predictable
_CYL_RESOLUTION = 16


def _make_fastener_mesh(p1_arr: np.ndarray, p2_arr: np.ndarray,
                        radius: float,
                        scalar_vals: np.ndarray = None,
                        scalar_name: str = "val") -> pv.PolyData:
    """
    Build N cylinders, each spanning its own p1→p2 axis.
    Returns a single merged PolyData (fast single draw-call).
    Per-cylinder vertex count V is stable = _CYL_RESOLUTION * 2 + 2
    from PyVista's Cylinder triangulate output — used by the discrete
    colour branch to assign per-point RGB without guessing n_points//N.
    """
    n = len(p1_arr)
    if n == 0:
        return pv.PolyData()

    base_dir = np.array([0.0, 0.0, 1.0])
    template = pv.Cylinder(
        radius=radius, height=1.0,
        direction=(0, 0, 1), resolution=_CYL_RESOLUTION,
    ).triangulate()

    tv = template.points
    tf = template.faces.reshape(-1, 4)[:, 1:]
    V  = len(tv)   # vertices per cylinder — stable for fixed resolution
    F  = len(tf)

    all_pts   = np.empty((n * V, 3), dtype=np.float64)
    all_faces = np.empty((n * F, 3), dtype=np.int64)
    if scalar_vals is not None:
        all_sc = np.empty(n * V, dtype=np.float64)

    for i in range(n):
        p1, p2 = p1_arr[i], p2_arr[i]
        axis   = p2 - p1
        length = np.linalg.norm(axis)
        if length < 1e-9:
            length    = radius * 2.5
            direction = base_dir
        else:
            direction = axis / length

        center  = (p1 + p2) / 2.0
        R       = _rotation_to_align(base_dir, direction)
        scaled  = tv.copy()
        scaled[:, 2] *= length
        world_pts = scaled @ R.T + center

        all_pts  [i * V:(i + 1) * V] = world_pts
        all_faces[i * F:(i + 1) * F] = tf + i * V
        if scalar_vals is not None:
            all_sc[i * V:(i + 1) * V] = scalar_vals[i]

    face_col  = np.full((n * F, 1), 3, dtype=np.int64)
    faces_pvt = np.hstack([face_col, all_faces]).ravel()
    mesh      = pv.PolyData(all_pts, faces_pvt)
    if scalar_vals is not None:
        mesh.point_data[scalar_name] = all_sc
    return mesh, V   # return V so callers don't have to recompute it


def _cbush_radius(bounds, scale: float = 1.0) -> float:
    """World-unit cylinder radius derived from model bounds."""
    try:
        dims    = [abs(bounds[1] - bounds[0]),
                   abs(bounds[3] - bounds[2]),
                   abs(bounds[5] - bounds[4])]
        nonzero = [d for d in dims if d > 1e-6]
        if not nonzero:
            return 10.0 * scale
        return float(np.clip(min(nonzero) * 0.04 * scale, 0.5, 1000.0))
    except Exception:
        return 10.0 * scale


# =============================================================================
# EXCEL HELPERS  (called from CalcThread only)
# =============================================================================
def _read_sheet_results(ws, n_rows: int,
                        header_row: int = 8,
                        data_start_row: int = 9) -> pd.DataFrame:
    """
    Dynamically detect last used column, read header + data block.
    Normalises the xlwings single-row edge-case (flat list → list-of-lists).
    """
    last_col_idx = ws.range(f"B{header_row}").end("right").column
    last_col_ltr = _col_letter(last_col_idx)
    last_row     = data_start_row + n_rows - 1

    raw_headers = ws.range(f"B{header_row}:{last_col_ltr}{header_row}").value
    col_names   = [
        str(c).strip() if c is not None else f"col_{i}"
        for i, c in enumerate(raw_headers)
    ]

    raw_data = ws.range(
        f"B{data_start_row}:{last_col_ltr}{last_row}").value
    if n_rows == 1:
        raw_data = [raw_data]

    return pd.DataFrame(raw_data, columns=col_names)


def calculate_metal(df_metal: pd.DataFrame, wb) -> pd.DataFrame:
    ws = wb.sheets[METAL_SHEET]
    ws.range("B9").expand("table").clear_contents()
    paste_data = df_metal[DATA_COLS_METAL].values.tolist()
    ws.range("B9").value = paste_data
    wb.app.calculate()
    df_results = _read_sheet_results(ws, n_rows=len(paste_data))
    return pd.concat(
        [df_metal[INFO_COLS].reset_index(drop=True), df_results], axis=1)


def calculate_composite(df_composite: pd.DataFrame, wb) -> pd.DataFrame:
    ws = wb.sheets[COMPOSITE_SHEET]
    ws.range("B9").expand("table").clear_contents()
    paste_data = df_composite[DATA_COLS_COMPOSITE].values.tolist()
    ws.range("B9").value = paste_data
    wb.app.calculate()
    df_results = _read_sheet_results(ws, n_rows=len(paste_data))
    return pd.concat(
        [df_composite[INFO_COLS].reset_index(drop=True), df_results], axis=1)


# =============================================================================
# BDF LOADER THREAD
# =============================================================================
class BDFLoader(QThread):
    """Loads BDF on a worker thread; emits done(bdf, error_string)."""
    done = pyqtSignal(object, str)

    def __init__(self, path: str):
        super().__init__()
        self.path = path

    def run(self):
        try:
            bdf = BDF()
            bdf.read_bdf(self.path, xref=True)   # xref=True for property lookup
            self.done.emit(bdf, "")
        except Exception as exc:
            self.done.emit(None, str(exc))


# =============================================================================
# CALC THREAD
# =============================================================================
class CalcThread(QThread):
    """
    Runs the full data pipeline + xlwings calculation on a worker thread.
    Emits done(result_xlsx_path, error_string).
    Zero Qt widget access inside run().
    """
    done    = pyqtSignal(str, str)
    status  = pyqtSignal(str)   # progress text → connected to _log on main thread

    def __init__(self, xlsm: str, fastpph_path: str, joint_path: str,
                 bdf: BDF, run_metal: bool, run_composite: bool,
                 output_dir: str):
        super().__init__()
        self.xlsm          = xlsm
        self.fastpph_path  = fastpph_path
        self.joint_path    = joint_path
        self.bdf           = bdf
        self.run_metal     = run_metal
        self.run_composite = run_composite
        self.output_dir    = output_dir

    def run(self):
        import xlwings as xw

        try:
            process_start = datetime.now()

            # ── fastpph CSV ───────────────────────────────────────────────
            self.status.emit("Reading fastpph CSV…")
            df_fast = pd.read_csv(
                self.fastpph_path, skiprows=2, sep=None, engine="python")
            df_fast.columns = df_fast.columns.str.strip()

            keep_cols = [
                "Component Name", "elem 1 id", "elem 1 Node id",
                "elem 2 id",      "elem 2 Node id", "box dimension",
                "file Name",      "LoadCase Name",
                "Fx", "Fy", "Fz",
                "Nx bypass", "Ny bypass", "Nxy bypass",
                "Mx total",  "My total",  "Mxy total",
                "DLS Ratio",
            ]
            missing = [c for c in keep_cols if c not in df_fast.columns]
            if missing:
                raise ValueError(f"fastpph CSV missing columns: {missing}")

            df_fast = df_fast[keep_cols].copy()
            df_fast.rename(columns={"elem 1 id": "Element ID"}, inplace=True)

            # ── JOINT CSV ─────────────────────────────────────────────────
            self.status.emit("Reading JOINT CSV…")
            df_joint = pd.read_csv(self.joint_path)
            df_joint.columns = df_joint.columns.str.strip()

            # ── Merge ─────────────────────────────────────────────────────
            self.status.emit("Merging datasets…")
            df = pd.merge(df_fast, df_joint, on="Element ID", how="left")
            if "Fastener Diameter" not in df.columns:
                raise ValueError(
                    "JOINT CSV must contain a 'Fastener Diameter' column.")
            df["Pitch"] = (df["box dimension"].astype(float) /
                           df["Fastener Diameter"].astype(float))

            # ── Combine duplicate CBUSH rows ──────────────────────────────
            self.status.emit("Aggregating duplicate CBUSH rows…")
            agg_spec = {c: (c, "sum") for c in LOAD_COLS}
            for col in AGG_FIRST_EXTRA:
                if col in df.columns:
                    agg_spec[col] = (col, "first")
            if "DLS Ratio" in df.columns:
                agg_spec["DLS Ratio"] = ("DLS Ratio", "max")

            df = df.groupby(GROUP_KEYS, as_index=False).agg(**agg_spec)

            # ── BDF properties ────────────────────────────────────────────
            self.status.emit("Extracting BDF properties…")
            prop_results = df["Component Name"].apply(
                lambda x: get_property_info(x, self.bdf))
            df[["MAT ID", "MAT", "n1", "n2", "n3", "n4"]] = pd.DataFrame(
                prop_results.tolist(), index=df.index)

            bad = df["MAT ID"].isna().sum()
            if bad:
                self.status.emit(
                    f"WARNING: {bad} rows had unresolvable BDF properties.")

            # ── Split metal / composite ───────────────────────────────────
            df["prop_type"] = df["Component Name"].apply(
                lambda x: parse_comp_name(x)[0])
            df_metal     = df[df["prop_type"] == "PSHELL"].reset_index(drop=True)
            df_composite = df[df["prop_type"] == "PCOMP" ].reset_index(drop=True)

            run_metal     = self.run_metal     and not df_metal.empty
            run_composite = self.run_composite and not df_composite.empty

            if not run_metal and not run_composite:
                raise ValueError(
                    "No PSHELL or PCOMP rows found — nothing to calculate.")

            # ── xlwings ───────────────────────────────────────────────────
            self.status.emit("Opening XLSM workbook…")
            xw_app = xw.App(visible=False, add_book=False)
            xw_app.display_alerts  = False
            xw_app.screen_updating = False
            xw_app.calculation     = "manual"

            df_metal_out     = None
            df_composite_out = None

            try:
                wb = xw_app.books.open(self.xlsm, update_links=False)
                try:
                    if run_metal:
                        self.status.emit(
                            f"Calculating {len(df_metal)} metallic joints…")
                        df_metal_out = calculate_metal(df_metal, wb)

                    if run_composite:
                        self.status.emit(
                            f"Calculating {len(df_composite)} composite joints…")
                        df_composite_out = calculate_composite(df_composite, wb)

                finally:
                    wb.close(save_changes=False)   # never overwrite the .xlsm

            finally:
                xw_app.screen_updating = True
                xw_app.quit()

            # ── Write results ─────────────────────────────────────────────
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            os.makedirs(self.output_dir, exist_ok=True)

            files_used = {
                "fastpph CSV" : os.path.abspath(self.fastpph_path),
                "JOINT CSV"   : os.path.abspath(self.joint_path),
                "XLSM File"   : os.path.abspath(self.xlsm),
            }
            elapsed = (datetime.now() - process_start).total_seconds()
            df_info = pd.DataFrame({
                "Item": list(files_used.keys()) + [
                    "Process Start", "Elapsed (s)"],
                "Value": list(files_used.values()) + [
                    process_start.strftime("%Y-%m-%d %H:%M:%S"),
                    f"{elapsed:.2f}",
                ],
            })

            last_out = ""
            if df_metal_out is not None:
                self.status.emit("Writing metallic results…")
                last_out = os.path.join(
                    self.output_dir, f"Metal_Results_{ts}.xlsx")
                with pd.ExcelWriter(last_out, engine="openpyxl") as writer:
                    df_metal_out.to_excel(writer, sheet_name="Results", index=False)
                    df_info.to_excel(writer,      sheet_name="Info",    index=False)

            if df_composite_out is not None:
                self.status.emit("Writing composite results…")
                last_out = os.path.join(
                    self.output_dir, f"Composite_Results_{ts}.xlsx")
                with pd.ExcelWriter(last_out, engine="openpyxl") as writer:
                    df_composite_out.to_excel(
                        writer, sheet_name="Results", index=False)
                    df_info.to_excel(writer, sheet_name="Info", index=False)

            self.done.emit(last_out, "")

        except Exception as exc:
            self.done.emit("", str(exc))


# =============================================================================
# MAIN WINDOW
# =============================================================================
class FastenerViewer(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Fastener Viewer")
        self.setGeometry(60, 60, 1600, 900)
        self.setMinimumSize(800, 500)

        # ── data ──────────────────────────────────────────────────────────
        self.bdf            = None
        self.df_fastpph     = None
        self.df_joint       = None
        self.df_output      = None
        self._xlsm_path     = None
        self._fastpph_path  = None
        self._joint_path    = None
        self._discrete_actors = []

        # ── render state ──────────────────────────────────────────────────
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
        self._base_radius   = None
        self._cached_bounds = None
        self._label_actors  = []
        self._labels_on     = False
        self._verts_per_cyl = None   # V from _make_fastener_mesh — stable per build

        self._theme = "dark"
        self._init_ui()

        self._progress_timer = QTimer()
        self._progress_timer.timeout.connect(self._swing_progress)
        self._progress_val  = 0
        self._progress_dir  = 1

    # =========================================================================
    # THEME
    # =========================================================================
    def _toggle_theme(self):
        self._apply_theme("light" if self._theme == "dark" else "dark")

    def _apply_theme(self, theme: str = "dark"):
        global BG, CARD, BORDER, ACCENT, ACCENT_HOV
        global FG, FG_DIM, FG_MONO, SUCCESS, WARN
        global COLOR_SHELL, COLOR_SHELL_EDGE

        p = PALETTE_DARK if theme == "dark" else PALETTE_LIGHT
        BG, CARD, BORDER   = p["BG"], p["CARD"], p["BORDER"]
        ACCENT, ACCENT_HOV = p["ACCENT"], p["ACCENT_HOV"]
        FG, FG_DIM, FG_MONO = p["FG"], p["FG_DIM"], p["FG_MONO"]
        SUCCESS, WARN      = p["SUCCESS"], p["WARN"]
        COLOR_SHELL        = p["COLOR_SHELL"]
        COLOR_SHELL_EDGE   = p["COLOR_SHELL_EDGE"]
        self._theme        = theme
        self._restyle_all()

    def _restyle_all(self):
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{ background-color:{BG}; color:{FG}; }}
            QSplitter::handle    {{ background-color:{BORDER}; }}
            QScrollBar:vertical  {{ background:{CARD}; width:8px; border-radius:4px; }}
            QScrollBar::handle:vertical {{ background:{BORDER}; border-radius:4px; }}
            QToolTip {{ background:{CARD}; color:{FG}; border:1px solid {BORDER}; }}
        """)
        self.left_panel.setStyleSheet(f"background-color:{CARD};")
        self.title_lbl.setStyleSheet(
            f"font-size:15px;font-weight:bold;color:{ACCENT};"
            f"padding:6px 0 4px 0;letter-spacing:1px;")

        for gb in self.findChildren(QGroupBox):
            gb.setStyleSheet(self._group_style())
        for ed in self.findChildren(QLineEdit):
            ed.setStyleSheet(self._edit_style())
        for btn in self._browse_btns:
            btn.setStyleSheet(self._btn_style())
        for lbl in self._dim_labels:
            lbl.setStyleSheet(f"color:{FG_DIM};font-size:10px;")

        self.calc_btn.setStyleSheet(self._calc_btn_style())
        self.progress.setStyleSheet(f"""
            QProgressBar       {{ border:none; background:{BORDER}; border-radius:2px; }}
            QProgressBar::chunk {{ background:{ACCENT}; border-radius:2px; }}
        """)
        self.status_lbl.setStyleSheet(
            f"color:{FG_DIM};font-size:10px;padding:2px 0;")
        self.log_hdr_lbl.setStyleSheet(
            f"font-weight:bold;font-size:10px;margin-top:4px;color:{FG};")
        self.log_toggle_btn.setStyleSheet(self._small_btn_style())
        self.log_text.setStyleSheet(f"""
            QTextEdit {{
                background:{BG}; color:{FG_MONO};
                border:1px solid {BORDER}; border-radius:4px;
                font-family:monospace; font-size:10px; padding:4px;
            }}
        """)

        self.bottom_bar.setStyleSheet(self._bottom_bar_style())
        self.pick_lbl.setStyleSheet(
            f"color:{FG_MONO};font-size:10px;"
            f"font-family:monospace;background:transparent;")
        for sep in self._vseps:
            sep.setStyleSheet(f"background:{BORDER}; max-width:1px;")

        if self.plotter:
            self.plotter.set_background(BG)
            if self._shell_actor:
                try:
                    self._shell_actor.GetProperty().SetColor(
                        pv.Color(COLOR_SHELL).float_rgb)
                    self._shell_actor.GetProperty().SetEdgeColor(
                        pv.Color(COLOR_SHELL_EDGE).float_rgb)
                except Exception:
                    pass
            self.plotter.render()

        self.theme_btn.setText("☀" if self._theme == "dark" else "🌙")

    # =========================================================================
    # UI
    # =========================================================================
    def _init_ui(self):
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{ background-color:{BG}; color:{FG}; }}
            QSplitter::handle    {{ background-color:{BORDER}; }}
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
        self._browse_btns = []
        self._dim_labels  = []

        panel = QWidget()
        self.left_panel = panel
        panel.setMaximumWidth(380)
        panel.setStyleSheet(f"background-color:{CARD};")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        title = QLabel("⬡  Fastener Viewer")
        self.title_lbl = title
        title.setStyleSheet(
            f"font-size:15px;font-weight:bold;color:{ACCENT};"
            f"padding:6px 0 4px 0;letter-spacing:1px;")
        lay.addWidget(title)

        # ── Input files ───────────────────────────────────────────────────
        fg = QGroupBox("Input Files")
        fg.setStyleSheet(self._group_style())
        fl = QVBoxLayout(); fl.setSpacing(6)

        for attr, label, filt in [
            ("bdf_edit",   "BDF File:",          "BDF Files (*.bdf);;All (*.*)"),
            ("fph_edit",   "fastpph CSV:",        "CSV Files (*.csv);;All (*.*)"),
            ("joint_edit", "JOINT CSV:",          "CSV Files (*.csv);;All (*.*)"),
            ("xlsm_edit",  "Calculator (.xlsm):", "Excel Macro (*.xlsm);;All (*.*)"),
            ("out_edit",   "Output XLSX:",        "Excel Files (*.xlsx);;All (*.*)"),
        ]:
            lbl = QLabel(label)
            lbl.setStyleSheet(f"color:{FG_DIM};font-size:10px;")
            self._dim_labels.append(lbl)
            fl.addWidget(lbl)
            row  = QHBoxLayout()
            edit = QLineEdit()
            edit.setReadOnly(True)
            edit.setPlaceholderText("Not loaded")
            edit.setStyleSheet(self._edit_style())
            btn  = QPushButton("Browse")
            btn.setFixedWidth(68)
            btn.setStyleSheet(self._btn_style())
            self._browse_btns.append(btn)
            btn.clicked.connect(lambda _, a=attr, f=filt: self._browse_file(a, f))
            row.addWidget(edit)
            row.addWidget(btn)
            fl.addLayout(row)
            setattr(self, attr, edit)

        fg.setLayout(fl)
        lay.addWidget(fg)

        # ── Calculation ───────────────────────────────────────────────────
        cg = QGroupBox("Calculation")
        cg.setStyleSheet(self._group_style())
        cl = QVBoxLayout(); cl.setSpacing(6)

        chk_row = QHBoxLayout()
        chk_ss  = f"font-size:11px;color:{FG};"
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

        cl.addSpacing(15)
        self.calc_btn = QPushButton("Calculate")
        self.calc_btn.setStyleSheet(self._calc_btn_style())
        self.calc_btn.clicked.connect(self._on_calculate)
        cl.addWidget(self.calc_btn)

        cg.setLayout(cl)
        lay.addWidget(cg)

        self.status_lbl = QLabel("Load a BDF file to begin.")
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setStyleSheet(
            f"color:{FG_DIM};font-size:10px;padding:2px 0;")
        lay.addWidget(self.status_lbl)

        # ── Log ───────────────────────────────────────────────────────────
        lay.addStretch()
        log_hdr_row = QHBoxLayout()
        self.log_hdr_lbl = QLabel("Log")
        self.log_hdr_lbl.setStyleSheet(
            f"font-weight:bold;font-size:10px;margin-top:4px;color:{FG};")
        self.log_toggle_btn = QPushButton("▼")
        self.log_toggle_btn.setFixedSize(20, 20)
        self.log_toggle_btn.setToolTip("Collapse log")
        self.log_toggle_btn.setStyleSheet(self._small_btn_style())
        self.log_toggle_btn.clicked.connect(self._toggle_log)
        log_hdr_row.addWidget(self.log_hdr_lbl)
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
                font-family:monospace; font-size:10px; padding:4px;
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
            self.plotter.add_axes(
                color=FG_DIM, viewport=(0.0, 0.0, 0.10, 0.13))
            self.plotter.track_click_position(
                callback=self._on_click, side="right")
            lay.addWidget(self.plotter.interactor)
        except Exception as exc:
            self.plotter = None
            lay.addWidget(QLabel(f"Viewport error: {exc}"))

        lay.addWidget(self._build_bottom_bar())
        return panel

    def _bottom_bar_style(self):
        return f"""
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
            QPushButton:hover   {{ background:{ACCENT}; color:white;
                                   border-color:{ACCENT}; }}
            QPushButton:checked {{ background:{ACCENT}; color:white;
                                   border-color:{ACCENT}; }}
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
        """

    def _build_bottom_bar(self):
        self._vseps = []
        bar = QWidget()
        self.bottom_bar = bar
        bar.setFixedHeight(52)
        bar.setStyleSheet(self._bottom_bar_style())
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(12, 6, 12, 6)
        bl.setSpacing(8)

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

        self.label_btn = QPushButton("Labels: OFF")
        self.label_btn.setFixedWidth(90)
        self.label_btn.setCheckable(True)
        self.label_btn.setChecked(False)
        self.label_btn.clicked.connect(self._on_toggle_labels)
        bl.addWidget(self.label_btn)
        bl.addWidget(self._vsep())

        self.theme_btn = QPushButton("☀" if self._theme == "dark" else "🌙")
        self.theme_btn.setFixedWidth(32)
        self.theme_btn.setToolTip("Toggle light/dark theme")
        self.theme_btn.clicked.connect(self._toggle_theme)
        bl.addWidget(self.theme_btn)
        bl.addWidget(self._vsep())

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

        bl.addStretch()
        self.pick_lbl = QLabel("")
        self.pick_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.pick_lbl.setStyleSheet(
            f"color:{FG_MONO};font-size:10px;"
            f"font-family:monospace;background:transparent;")
        bl.addWidget(self.pick_lbl)

        return bar

    # ── widget helpers ────────────────────────────────────────────────────────
    def _bar_label(self, text):
        lbl = QLabel(text)
        lbl.setStyleSheet("background:transparent;")
        return lbl

    def _vsep(self):
        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setStyleSheet(f"background:{BORDER}; max-width:1px;")
        self._vseps.append(sep)
        return sep

    # =========================================================================
    # STYLE HELPERS
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

    def _small_btn_style(self):
        return (f"border:1px solid {BORDER}; border-radius:3px;"
                f"background:{BG}; color:{FG}; font-size:10px;")

    def _calc_btn_style(self, disabled: bool = False):
        if disabled:
            return (f"QPushButton {{ background-color:{BORDER}; color:{FG_DIM};"
                    f"border:none; border-radius:4px; padding:7px;"
                    f"font-weight:bold; font-size:11px; }}")
        return f"""
            QPushButton {{
                background-color:{ACCENT}; color:white; border:none;
                border-radius:4px; padding:7px;
                font-weight:bold; font-size:11px;
            }}
            QPushButton:hover    {{ background-color:{ACCENT_HOV}; }}
            QPushButton:disabled {{ background-color:{BORDER}; color:{FG_DIM}; }}
        """

    # =========================================================================
    # LOG / PROGRESS  (main thread only — called only from slots)
    # =========================================================================
    def _toggle_log(self):
        visible = self.log_text.isVisible()
        self.log_text.setVisible(not visible)
        self.log_toggle_btn.setText("▼" if visible else "▶")

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.append(f"[{ts}] {msg}")

    def _start_busy(self, msg: str = "Working…"):
        self.status_lbl.setText(msg)
        self._log(msg)
        self._progress_val = 0
        self._progress_dir = 1
        self._progress_timer.start(30)
        QApplication.processEvents()

    def _stop_busy(self, msg: str = ""):
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
    def _browse_file(self, attr: str, filt: str):
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
    def _load_bdf(self, fn: str):
        self._start_busy("Loading BDF…")
        self._bdf_loader = BDFLoader(fn)
        self._bdf_loader.done.connect(self._on_bdf_loaded)
        self._bdf_loader.start()

    def _on_bdf_loaded(self, bdf, err: str):
        if err:
            self._stop_busy(f"Error: {err}")
            return
        self.bdf         = bdf
        self._pts_cache  = None
        self._nmap_cache = None
        n_sh = sum(1 for e in bdf.elements.values()
                   if e.type in ("CQUAD4", "CTRIA3"))
        n_cb = sum(1 for e in bdf.elements.values()
                   if e.type == "CBUSH")
        self._render_bdf()
        self._stop_busy(f"BDF loaded — {n_sh} shells, {n_cb} CBUSH")

    def _build_pts(self):
        if self._pts_cache is None:
            nodes, nmap = [], {}
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
        self._verts_per_cyl = None
        self._discrete_actors = []

        pts, nmap = self._build_pts()

        # ── Shells ────────────────────────────────────────────────────────
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
                edge_color=COLOR_SHELL_EDGE,
                opacity=self.opacity_slider.value() / 100.0,
                pickable=False, show_scalar_bar=False)

        # ── 1-D elements ──────────────────────────────────────────────────
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
                    pv.Line(p1, p2), color=FG_MONO,
                    line_width=1, pickable=False, show_scalar_bar=False)
                self._rod_actors.append(actor)
            except Exception:
                continue

        # ── CBUSH endpoints + midpoints ───────────────────────────────────
        self._cbush_centers   = {}
        self._cbush_endpoints = {}
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

        # Cache bounds BEFORE CBUSH actors (avoids inflated bounds)
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
        self._base_radius = _cbush_radius(self._cached_bounds, 1.0)
        r    = self._base_radius * self._radius_scale
        op   = self.opacity_slider.value() / 100.0

        mesh, V = _make_fastener_mesh(p1s, p2s, r)
        self._verts_per_cyl = V
        self._cbush_actor   = self.plotter.add_mesh(
            mesh, color=COLOR_CBUSH, smooth_shading=True,
            opacity=op, pickable=True, show_scalar_bar=False)

    # =========================================================================
    # OPACITY SLIDER  — controls shell transparency
    # =========================================================================
    def _on_opacity_changed(self, val: int):
        self.opacity_val_lbl.setText(f"{val}%")
        if self._shell_actor:
            try:
                self._shell_actor.GetProperty().SetOpacity(val / 100.0)
            except Exception:
                pass
        if self.plotter:
            self.plotter.render()

    # =========================================================================
    # RADIUS SLIDER
    # =========================================================================
    def _on_radius_changed(self, val: int):
        self._radius_scale = val / 100.0
        self.radius_val_lbl.setText(f"{self._radius_scale:.2f}×")
        self._recolor_cbush()

    # =========================================================================
    # CSV LOADERS
    # =========================================================================
    def _load_fastpph(self, fn: str):
        self._start_busy("Loading fastpph…")
        try:
            df = pd.read_csv(fn, skiprows=2, sep=None, engine="python")
            df.columns = df.columns.str.strip()
            df.rename(columns={"elem 1 id": "Element ID"}, inplace=True)
            df["Element ID"] = pd.to_numeric(
                df["Element ID"], errors="coerce").astype("Int64")
            self.df_fastpph    = df
            self._fastpph_path = fn
            self._update_col_combo()
            self._stop_busy(f"fastpph loaded — {len(df)} rows")
        except Exception as exc:
            self._stop_busy(f"fastpph error: {exc}")

    def _load_joint(self, fn: str):
        self._start_busy("Loading JOINT CSV…")
        try:
            df = pd.read_csv(fn)
            df.columns = df.columns.str.strip()
            # Normalise Element ID so lookups match
            df["Element ID"] = pd.to_numeric(
                df["Element ID"], errors="coerce").astype("Int64")
            self.df_joint    = df
            self._joint_path = fn
            self._update_col_combo()          # ← was missing
            self._stop_busy(f"JOINT CSV loaded — {len(df)} rows")
        except Exception as exc:
            self._stop_busy(f"JOINT CSV error: {exc}")

    def _load_output(self, fn: str):
        self._start_busy("Loading output xlsx…")
        try:
            df = pd.read_excel(fn, sheet_name="Results")
            df.columns = df.columns.str.strip()
            df["Element ID"] = pd.to_numeric(
                df["Element ID"], errors="coerce").astype("Int64")
            keep = ["Element ID", "RF", "Allowable",
                    "Applied", "MS_tension", "MS_shear"]
            self.df_output = df[[c for c in keep if c in df.columns]]
            self._update_col_combo()
            self._stop_busy(f"Output loaded — {len(self.df_output)} rows")
            if self.bdf:
                self._recolor_cbush()
        except Exception as exc:
            self._stop_busy(f"Output error: {exc}")

    # =========================================================================
    # COLUMN COMBO
    # =========================================================================
    def _update_col_combo(self):
        prev = self.col_combo.currentText()
        self.col_combo.blockSignals(True)
        self.col_combo.clear()
        self.col_combo.addItem("Default")

        for col, df_attr, _ in COLUMN_CONFIG:
            df = getattr(self, df_attr, None)
            if df is not None and col in df.columns:
                self.col_combo.addItem(col)

        self.col_combo.setEnabled(True)
        idx = self.col_combo.findText(prev)
        self.col_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.col_combo.blockSignals(False)

    def _on_col_changed(self):
        pass   # user presses Apply to commit

    # =========================================================================
    # LABEL TOGGLE
    # =========================================================================
    def _on_toggle_labels(self, checked: bool):
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
            cfg = next((c for c in COLUMN_CONFIG if c[0] == col_text), None)
            if cfg is not None:
                df = getattr(self, cfg[1], None)
                if df is not None and "Element ID" in df.columns and col_text in df.columns:
                    for _, row in df.iterrows():
                        try:
                            eid_to_val[int(row["Element ID"])] = row[col_text]
                        except Exception:
                            pass

        r = (self._base_radius or _cbush_radius(self._cached_bounds, 1.0)) \
            * self._radius_scale

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
                    font_size=9, text_color=FG, point_color=FG,
                    point_size=0, shape=None,
                    render_points_as_spheres=False,
                    always_visible=True, shadow=False, pickable=False)
                self._label_actors.append(actor)
            except Exception:
                pass

        self.plotter.render()

    # =========================================================================
    # RECOLOR CBUSH
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

        # Remove any previous discrete actors
        if not hasattr(self, '_discrete_actors'):
            self._discrete_actors = []
        for a in self._discrete_actors:
            try:
                self.plotter.remove_actor(a)
            except Exception:
                pass
        self._discrete_actors = []

        col_text = self.col_combo.currentText()
        eids = list(self._cbush_endpoints.keys())
        p1s  = np.array([self._cbush_endpoints[e][0] for e in eids], dtype=float)
        p2s  = np.array([self._cbush_endpoints[e][1] for e in eids], dtype=float)
        r    = (self._base_radius or _cbush_radius(self._cached_bounds, 1.0)) \
            * self._radius_scale

        # ── Default solid colour ──────────────────────────────────────────
        if col_text == "Default":
            mesh, V = _make_fastener_mesh(p1s, p2s, r)
            self._verts_per_cyl = V
            self._cbush_actor   = self.plotter.add_mesh(
                mesh, color=COLOR_CBUSH, smooth_shading=True,
                opacity=1.0, pickable=True, show_scalar_bar=False)
            self.plotter.render()
            self._refresh_labels()
            return

        # ── Resolve column + dataframe + discrete flag ────────────────────
        cfg = next((c for c in COLUMN_CONFIG if c[0] == col_text), None)
        if cfg is None:
            self._stop_busy(f"Column '{col_text}' not in configuration.")
            return

        col, df_attr, is_discrete = cfg
        df = getattr(self, df_attr, None)
        if df is None or col not in df.columns:
            self._stop_busy(f"Column '{col}' not available.")
            return

        # ── Build eid → value map ─────────────────────────────────────────
        eid_to_val = {}
        for _, row in df.iterrows():
            try:
                eid_to_val[int(row["Element ID"])] = row[col]
            except Exception:
                pass

        # DEBUG — remove once working
        self._log(f"eid_to_val keys sample: {list(eid_to_val.keys())[:5]}")
        self._log(f"eids sample: {eids[:5]}")
        self._log(f"eids type: {type(eids[0]) if eids else 'empty'}")
        self._log(f"eid_to_val key type: {type(list(eid_to_val.keys())[0]) if eid_to_val else 'empty'}")
        matched = [e for e in eids if e in eid_to_val]
        self._log(f"matched: {len(matched)}/{len(eids)}")

        vals   = [eid_to_val.get(e) for e in eids]
        is_num = not is_discrete and all(
            isinstance(v, (int, float)) and not (isinstance(v, float) and np.isnan(v))
            for v in vals if v is not None)

        # ── Continuous colormap ───────────────────────────────────────────
        if is_num:
            scalar_arr = np.array([
                float(eid_to_val[e]) if e in eid_to_val else np.nan
                for e in eids], dtype=float)
            valid = scalar_arr[~np.isnan(scalar_arr)]
            clim  = [float(valid.min()), float(valid.max())] if len(valid) else [0.0, 1.0]

            mesh, V = _make_fastener_mesh(p1s, p2s, r, scalar_arr, col)
            self._verts_per_cyl = V
            self._cbush_actor   = self.plotter.add_mesh(
                mesh, scalars=col, cmap="coolwarm",
                smooth_shading=True, opacity=1.0,
                clim=clim, pickable=True, show_scalar_bar=True,
                scalar_bar_args=dict(
                    title=col, vertical=True,
                    position_x=0.88, position_y=0.10,
                    width=0.04, height=0.70, n_labels=5,
                    color=FG, title_font_size=11, label_font_size=10))
            self._scalar_bar = col

        # ── Discrete — one mesh per unique value ──────────────────────────
        else:
            unique = sorted({str(eid_to_val[e]) for e in eids if e in eid_to_val})
            palette = [
                ACCENT,    "#22c55e", "#f59e0b", "#ef4444",
                "#a855f7", "#06b6d4", "#f97316", "#84cc16",
                "#ec4899", "#14b8a6", "#fb923c", "#a3e635",
            ]
            v2c = {v: palette[i % len(palette)] for i, v in enumerate(unique)}

            for val in unique:
                # collect endpoints for this value only
                idx  = [i for i, e in enumerate(eids)
                        if str(eid_to_val.get(e, "")) == val]
                if not idx:
                    continue
                vp1s = p1s[idx]
                vp2s = p2s[idx]
                mesh, V = _make_fastener_mesh(vp1s, vp2s, r)
                self._verts_per_cyl = V
                actor = self.plotter.add_mesh(
                    mesh, color=v2c[val], smooth_shading=True,
                    opacity=1.0, pickable=True, show_scalar_bar=False)
                self._discrete_actors.append(actor)

            # unmatched EIDs → dark grey
            unmatched_idx = [i for i, e in enumerate(eids) if e not in eid_to_val]
            if unmatched_idx:
                vp1s = p1s[unmatched_idx]
                vp2s = p2s[unmatched_idx]
                mesh, V = _make_fastener_mesh(vp1s, vp2s, r)
                actor = self.plotter.add_mesh(
                    mesh, color="#444444", smooth_shading=True,
                    opacity=1.0, pickable=True, show_scalar_bar=False)
                self._discrete_actors.append(actor)

            # ── Legend ───────────────────────────────────────────────────────
            try:
                legend_entries = [[str(v), v2c[v]] for v in unique]
                if unmatched_idx:
                    legend_entries.append(["No data", "#444444"])
                if legend_entries:
                    self.plotter.add_legend(
                        labels=legend_entries,
                        face="rectangle",
                        size=(0.18, min(0.06 + len(legend_entries) * 0.05, 0.50)),
                        loc="upper right",
                        bcolor=CARD,
                        border=True)
            except Exception as e:
                self._log(f"Legend error: {e}")

        self.plotter.render()
        self._refresh_labels()
        self.status_lbl.setText(f"Colored by: {col}")

    # =========================================================================
    # CALCULATE
    # =========================================================================
    def _on_calculate(self):
        if not self.chk_metal.isChecked() and not self.chk_composite.isChecked():
            self.status_lbl.setText("Select at least one: Metallic or Composite.")
            return
        if not self.bdf:
            self.status_lbl.setText("Load a BDF file first.")
            return

        xlsm = self._xlsm_path or self.xlsm_edit.toolTip()
        if not xlsm or not os.path.exists(xlsm):
            self.status_lbl.setText("Please browse a .xlsm calculator file first.")
            return

        fastpph = self._fastpph_path or self.fph_edit.toolTip()
        if not fastpph or not os.path.exists(fastpph):
            self.status_lbl.setText("Please browse the fastpph CSV first.")
            return

        joint = self._joint_path or self.joint_edit.toolTip()
        if not joint or not os.path.exists(joint):
            self.status_lbl.setText("Please browse the JOINT CSV first.")
            return

        # Derive output dir next to the .xlsm; fall back to CWD
        output_dir = os.path.join(
            os.path.dirname(os.path.abspath(xlsm)), "fastener_results")

        self.calc_btn.setEnabled(False)
        self._start_busy("Calculating… please wait")

        self._calc_thread = CalcThread(
            xlsm          = xlsm,
            fastpph_path  = fastpph,
            joint_path    = joint,
            bdf           = self.bdf,
            run_metal     = self.chk_metal.isChecked(),
            run_composite = self.chk_composite.isChecked(),
            output_dir    = output_dir,
        )
        # status signal → _log (cross-thread, queued automatically by Qt)
        self._calc_thread.status.connect(self._log)
        self._calc_thread.done.connect(self._on_calc_done)
        self._calc_thread.start()

    def _on_calc_done(self, result_path: str, err: str):
        # This slot is called on the main thread via Qt's queued connection
        self.calc_btn.setEnabled(True)
        if err:
            self._stop_busy(f"Calculation error: {err}")
            return
        self._stop_busy(f"Done — {result_path}")
        if result_path and os.path.exists(result_path):
            self.out_edit.setText(os.path.basename(result_path))
            self.out_edit.setToolTip(result_path)
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
            cfg = next((c for c in COLUMN_CONFIG if c[0] == col_text), None)
            if cfg is not None:
                df = getattr(self, cfg[1], None)
                if df is not None and "Element ID" in df.columns and col_text in df.columns:
                    row = df[df["Element ID"].astype(int) == eid]
                    if not row.empty:
                        v       = row.iloc[0][col_text]
                        val_str = f"{v:.4g}" if isinstance(v, float) else str(v)
                        text    = f"EID {eid}  {col_text}: {val_str}"
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
        # Stop any running worker threads gracefully before closing the viewport
        if self._calc_thread and self._calc_thread.isRunning():
            self._calc_thread.wait(3000)
        if self._bdf_loader and self._bdf_loader.isRunning():
            self._bdf_loader.wait(3000)
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
