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
import time
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
from pyNastran.bdf.bdf import read_bdf
from pyvistaqt import QtInteractor

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QGroupBox, QFileDialog,
    QSplitter, QComboBox, QProgressBar, QFrame, QCheckBox,
    QSizePolicy, QSlider, QTextEdit,
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer

# =============================================================================
# THEME DEFINITIONS
# =============================================================================
THEMES = {
    "Dark": dict(
        BG="#0e1117", CARD="#161b27", BORDER="#1e2736",
        ACCENT="#3b82f6", ACCENT_HOV="#2563eb",
        FG="#e2e8f0", FG_DIM="#64748b", FG_MONO="#94a3b8",
        SUCCESS="#22c55e", WARN="#f59e0b",
        COLOR_SHELL="#2a3f5f", COLOR_SHELL_EDGE="#1e2736",
        VP_BG="#0e1117", AXES_COLOR="#64748b",
    ),
    "Light": dict(
        BG="#f8fafc", CARD="#ffffff", BORDER="#e2e8f0",
        ACCENT="#3b82f6", ACCENT_HOV="#2563eb",
        FG="#1e293b", FG_DIM="#64748b", FG_MONO="#475569",
        SUCCESS="#16a34a", WARN="#d97706",
        COLOR_SHELL="#94a3b8", COLOR_SHELL_EDGE="#cbd5e1",
        VP_BG="#f0f4f8", AXES_COLOR="#475569",
    ),
    # Cyber: magenta/cyan on near-black
    "Cyber": dict(
        BG="#0A0A14", CARD="#12121F", BORDER="#FF2D78",
        ACCENT="#FF2D78", ACCENT_HOV="#cc0055",
        FG="#E0E0FF", FG_DIM="#6060AA", FG_MONO="#00F5FF",
        SUCCESS="#00F5FF", WARN="#FFD700",
        COLOR_SHELL="#1A1A2E", COLOR_SHELL_EDGE="#FF2D78",
        VP_BG="#070710", AXES_COLOR="#00F5FF",
    ),
    # Half-Life: valve orange on charcoal/black
    "Half-Life": dict(
        BG="#111111", CARD="#1a1a1a", BORDER="#cf6a00",
        ACCENT="#ff6a00", ACCENT_HOV="#e05800",
        FG="#e8dcc8", FG_DIM="#7a6a55", FG_MONO="#ffaa44",
        SUCCESS="#ff6a00", WARN="#ffcc00",
        COLOR_SHELL="#2a2218", COLOR_SHELL_EDGE="#cf6a00",
        VP_BG="#0d0d0d", AXES_COLOR="#ff6a00",
    ),
    # Solarized: warm amber/teal on deep navy — the surprise
    "Solarized": dict(
        BG="#002b36", CARD="#073642", BORDER="#586e75",
        ACCENT="#b58900", ACCENT_HOV="#8a6800",
        FG="#fdf6e3", FG_DIM="#657b83", FG_MONO="#2aa198",
        SUCCESS="#859900", WARN="#cb4b16",
        COLOR_SHELL="#073642", COLOR_SHELL_EDGE="#586e75",
        VP_BG="#001e26", AXES_COLOR="#2aa198",
    ),
    # Windows 95: classic teal desktop, silver widgets, navy title bar
    "Win95": dict(
        BG="#008080", CARD="#c0c0c0", BORDER="#808080",
        ACCENT="#000080", ACCENT_HOV="#00007a",
        FG="#000000", FG_DIM="#444444", FG_MONO="#000080",
        SUCCESS="#008000", WARN="#808000",
        COLOR_SHELL="#c0c0c0", COLOR_SHELL_EDGE="#808080",
        VP_BG="#008080", AXES_COLOR="#000080",
    ),
}

THEME_ICONS = {
    "Dark":      "🌙",
    "Light":     "☀",
    "Cyber":     "⚡",
    "Half-Life": "☢",
    "Solarized": "🌅",
    "Win95":     "🖥",
}

# Active theme globals — mutated only by _apply_theme on the main thread
_T = THEMES["Dark"].copy()
BG          = _T["BG"];          CARD        = _T["CARD"]
BORDER      = _T["BORDER"];      ACCENT      = _T["ACCENT"]
ACCENT_HOV  = _T["ACCENT_HOV"]; FG          = _T["FG"]
FG_DIM      = _T["FG_DIM"];     FG_MONO     = _T["FG_MONO"]
SUCCESS     = _T["SUCCESS"];     WARN        = _T["WARN"]
COLOR_SHELL = _T["COLOR_SHELL"]; COLOR_SHELL_EDGE = _T["COLOR_SHELL_EDGE"]
COLOR_CBUSH = "#3b82f6"

# =============================================================================
# SHEET / COLUMN CONFIGURATION
# =============================================================================
METAL_SHEET     = "Metallic Bolted J.-Automation"
COMPOSITE_SHEET = "Composite Bolted J.-Automation"

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
    result = ""
    while col_index > 0:
        col_index, rem = divmod(col_index - 1, 26)
        result = chr(65 + rem) + result
    return result


def parse_comp_name(comp_name: str):
    try:
        parts = str(comp_name).strip().split("_")
        if len(parts) < 2:
            raise ValueError
        return parts[0].upper(), int(parts[-1])
    except (ValueError, IndexError):
        return None, None


def get_property_info(comp_name: str, bdf: BDF):
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
            mat_id = prop.mid1
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
# BDF READER
# =============================================================================
def my_read_bdf(path: str) -> BDF:
    try:
        try:
            return read_bdf(path, punch=True, xref=True)
        except Exception:
            return read_bdf(path, punch=True, xref=False)
    except Exception:
        try:
            return read_bdf(path, punch=False, xref=True)
        except Exception:
            return read_bdf(path, punch=False, xref=False)


# =============================================================================
# GEOMETRY BUILDERS
# =============================================================================
def _rotation_to_align(v_from: np.ndarray, v_to: np.ndarray) -> np.ndarray:
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


_CYL_RESOLUTION = 16


def _make_fastener_mesh(p1_arr: np.ndarray, p2_arr: np.ndarray,
                        radius: float,
                        scalar_vals: np.ndarray = None,
                        scalar_name: str = "val") -> pv.PolyData:
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
    V  = len(tv)
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
    return mesh, V


def _cbush_radius(bounds, scale: float = 1.0) -> float:
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
def _clear_range_dynamic(ws, start_row: int, col_from: str, col_to: str):
    try:
        last_row = ws.range(f"{col_from}{start_row}").end("down").row
        if last_row > start_row:
            ws.range(f"{col_from}{start_row}:{col_to}{last_row}").clear_contents()
    except Exception:
        pass


def rf_joint_metal(wb, df_join: pd.DataFrame):
    start = 9 if df_join.shape[0] != 0 else 11
    begin = time.time()

    def mat_fix(df):
        df = df.copy()
        df["MAT"] = df["MAT"].apply(
            lambda x: '2050-T84, 2050-T852, 7050-T7451' if str(x).startswith('2050') else x)
        df["MAT"] = df["MAT"].apply(
            lambda x: 'Ti-6Al-4V Plate/Forging' if str(x).startswith('Ti-6Al-4V') else x)
        df["MAT"] = df["MAT"].apply(
            lambda x: '2024-T42 Clad' if "clad" in str(x).lower() else x)
        df["MAT"] = df["MAT"].apply(
            lambda x: 'PH 13-8 Mo' if str(x).startswith('PH') else x)
        df["Side"] = df["Side"].apply(
            lambda x: 'Head Side' if str(x).startswith('H') else x)
        df["Side"] = df["Side"].apply(
            lambda x: 'Tail Side' if str(x).startswith('T') else x)
        return df

    ttb_sheet = METAL_SHEET
    ws = wb.sheets[ttb_sheet]

    df_join = df_join[
        df_join["Element Type"].str.contains('cap', regex=True, case=False, na=False)
    ]
    df_join = df_join[
        ~df_join["MAT"].str.contains('m21|m91|quartz', regex=True, case=False, na=False)
    ]
    df_join = df_join[
        ~df_join["Property ID"].astype("str").str[2].isin(['0'])
    ]
    df_join = df_join[df_join["W"] > 2]

    rows = df_join.shape[0] + start - 1
    if rows < start:
        print("WARNING: MBJ -> rows<start, check if your misc and inputs are matching.")
        rows = start + 1

    _clear_range_dynamic(ws, start + 1, 'B', 'AK')
    ws.range(f'B{start}:AK{start}').api.AutoFill(
        Destination=ws.range(f'B{start}:AK{rows}').api, Type=0)

    df_join = mat_fix(df_join)

    ws.range(f'F{start}').value = np.array(df_join["MAT"]).reshape(-1, 1)
    try:
        ws.range(f'G{start}').value = np.array(df_join["Bar Thickness"]).reshape(-1, 1)
    except Exception as e:
        print(f"ERROR {e} - can't find bar thicknesses")

    ws.range(f'B{start}').value = np.array(df_join["Pin"]).reshape(-1, 1)
    ws.range(f'C{start}').value = np.array(df_join["Collar"]).reshape(-1, 1)
    ws.range(f'E{start}').value = np.array(df_join["Side"]).reshape(-1, 1)
    ws.range(f'D{start}').value = np.array(df_join["Diameter"]).reshape(-1, 1)
    ws.range(f'K{start}').value = np.array(df_join["Pitch"]).reshape(-1, 1)
    ws.range(f'M{start}').value = np.array(df_join["Application"]).reshape(-1, 1)

    try:
        ws.range(f'I{start}').value = np.array(df_join["Shim"]).reshape(-1, 1)
    except Exception:
        ws.range(f'I{start}').value = 1.2

    try:
        ws.range(f'P{start}').value = np.array(df_join["Prying"]).reshape(-1, 1)
    except Exception:
        ws.range(f'P{start}:P{rows}').value = "YES"

    ws.range(f'H{start}:H{rows}').value = 2
    ws.range(f'J{start}:J{rows}').value = "Wet"
    ws.range(f'L{start}:L{rows}').value = "Structural fit"
    ws.range(f'Q{start}:Q{rows}').value = 0

    df_join["Shear"] = (df_join["F Bearing X"] ** 2 + df_join["F Bearing Y"] ** 2) ** 0.5
    ws.range(f'N{start}').value = np.array(df_join["Shear"]).reshape(-1, 1)
    ws.range(f'O{start}').value = np.array(df_join["F Bearing Z"]).reshape(-1, 1)

    wb.app.calculate()
    end = time.time()

    columns = ws.range(f'B{start - 1}:AK{start - 1}').value
    df_out  = pd.DataFrame(ws.range(f'B{start}:AK{rows}').value, columns=columns)

    all_contain_error = (
        df_out["RF Combined"].astype(str)
        .str.contains('ERROR', case=False, na=False).all()
    )
    if all_contain_error:
        print("WARNING - all joints gave ERROR label as result, trying again.")
        ws.range(f'D{start}:D{rows}').value = 6.35
        wb.app.calculate()
        ws.range(f'D{start}').value = np.array(df_join["Diameter"]).reshape(-1, 1)
        wb.app.calculate()
        columns = ws.range(f'B{start - 1}:BM{start - 1}').value
        df_out  = pd.DataFrame(ws.range(f'B{start}:BM{rows}').value, columns=columns)

    _clear_range_dynamic(ws, start + 1, 'B', 'AK')

    df_addition = df_join[
        ["Element ID (PBARL)", "Property ID (PBARL)", "Subcase ID"]
    ].reset_index(drop=True)
    df_addition.rename(columns={
        "Element ID (PBARL)": "Element ID",
        "Property ID (PBARL)": "Property ID",
    }, inplace=True)

    df_out = pd.concat((df_addition, df_out), axis=1)

    if "RF Combined" in df_out.columns and "RF" not in df_out.columns:
        df_out.rename(columns={"RF Combined": "RF"}, inplace=True)

    dt = end - begin
    return "Results", df_out, dt


def rf_joint_composite(wb, df_join: pd.DataFrame):
    start = 9 if df_join.shape[0] != 0 else 11
    begin = time.time()

    ttb_sheet = COMPOSITE_SHEET
    ws = wb.sheets[ttb_sheet]

    skin_rows = df_join[
        df_join["Element Type"].str.contains('skin', regex=True, case=False, na=False)
    ]
    if skin_rows.shape[0] != 0:
        df_join = skin_rows
    df_join = df_join[
        df_join["MAT"].str.contains('m21|m91|quartz', regex=True, case=False, na=False)
    ]
    df_join = df_join[df_join["W"] > 2]

    rows = df_join.shape[0] + start - 1
    if rows < start:
        print("WARNING: CBJ -> rows<start, check if your misc and inputs are matching.")
        rows = start + 1

    _clear_range_dynamic(ws, start + 1, 'B', 'BM')
    ws.range(f'B{start}:BM{start}').api.AutoFill(
        Destination=ws.range(f'B{start}:BM{rows}').api, Type=0)

    ws.range(f'B{start}').value = np.array(df_join["MAT"]).reshape(-1, 1)
    ws.range(f'C{start}').value = np.array(df_join["n1"]).reshape(-1, 1)
    ws.range(f'D{start}').value = np.array(df_join["n2"]).reshape(-1, 1)
    ws.range(f'E{start}').value = np.array(df_join["n3"]).reshape(-1, 1)
    ws.range(f'F{start}').value = np.array(df_join["n4"]).reshape(-1, 1)
    ws.range(f'K{start}').value = np.array(df_join["aoff (deg)"]).reshape(-1, 1)

    try:
        df_join["Temperature (C)"] = df_join["Temperature (C)"].fillna(100)
        ws.range(f'L{start}').value = np.array(df_join["Temperature (C)"]).reshape(-1, 1)
    except Exception as e:
        print(f"{e} - 'Temperature (C)' column does not exist")

    ws.range(f'N{start}').value = np.array(df_join["Pin"]).reshape(-1, 1)
    ws.range(f'O{start}').value = np.array(df_join["Collar"]).reshape(-1, 1)
    ws.range(f'Q{start}').value = np.array(df_join["Side"]).reshape(-1, 1)
    ws.range(f'M{start}:M{rows}').value = "HW"

    for col_letter in ["AJ", "AK", "AL", "AM", "AN", "AO", "AP"]:
        ws.range(f'{col_letter}{start}:{col_letter}{rows}').value = "y"

    ws.range(f'P{start}').value = np.array(df_join["Diameter"]).reshape(-1, 1)
    ws.range(f'R{start}').value = np.array(df_join["Pitch"] * df_join["Diameter"]).reshape(-1, 1)
    ws.range(f'S{start}').value = np.array(df_join["Pitch"] * df_join["Diameter"]).reshape(-1, 1)
    ws.range(f'U{start}').value = np.array(2.5 * df_join["Diameter"]).reshape(-1, 1)
    ws.range(f'T{start}').value = np.array(2.5 * df_join["Diameter"]).reshape(-1, 1)

    try:
        ws.range(f'AH{start}').value = np.array(df_join["Prying"]).reshape(-1, 1)
    except Exception:
        ws.range(f'AH{start}:AH{rows}').value = "YES"

    ws.range(f'AE{start}:AE{rows}').value = "SLS Supported"

    df_join = df_join.copy()
    df_join["Application"] = df_join["Application"].apply(
        lambda x: 2 if "single" in str(x).lower() else 1.3)
    ws.range(f'AG{start}').value = np.array(df_join["Application"]).reshape(-1, 1)

    try:
        ws.range(f'V{start}').value = np.array(df_join["Shim"]).reshape(-1, 1)
    except Exception:
        ws.range(f'V{start}').value = 1.2

    ws.range(f'W{start}').value = np.array(df_join["Nx Bypass"]).reshape(-1, 1)
    ws.range(f'X{start}').value = np.array(df_join["Ny Bypass"]).reshape(-1, 1)
    ws.range(f'Y{start}').value = np.array(df_join["Nxy Bypass"]).reshape(-1, 1)
    ws.range(f'AC{start}').value = np.array(df_join["F Bearing X"]).reshape(-1, 1)
    ws.range(f'AD{start}').value = np.array(df_join["F Bearing Y"]).reshape(-1, 1)
    ws.range(f'AF{start}').value = np.array(df_join["F Bearing Z"]).reshape(-1, 1)

    try:
        ws.range(f'Z{start}').value  = np.array(df_join["Mx"]).reshape(-1, 1)
        ws.range(f'AA{start}').value = np.array(df_join["My"]).reshape(-1, 1)
        ws.range(f'AB{start}').value = np.array(df_join["Mxy"]).reshape(-1, 1)
    except Exception:
        print("WARNING - Moments are missing, using 0 instead.")
        ws.range(f'Z{start}:Z{rows}').value  = 0
        ws.range(f'AA{start}:AA{rows}').value = 0
        ws.range(f'AB{start}:AB{rows}').value = 0

    ws.range(f'AI{start}:AI{rows}').value = 0

    wb.app.calculate()

    columns = ws.range(f'B{start - 1}:BM{start - 1}').value
    df_out  = pd.DataFrame(ws.range(f'B{start}:BM{rows}').value, columns=columns)

    all_contain_error = (
        df_out["Failure Mode"].astype(str)
        .str.contains('ERROR', case=False, na=False).all()
    )
    if all_contain_error:
        print("WARNING - all joints gave ERROR label as result, trying again.")
        ws.range(f'P{start}:P{rows}').value = 6.35
        wb.app.calculate()
        ws.range(f'P{start}').value = np.array(df_join["Diameter"]).reshape(-1, 1)
        wb.app.calculate()
        columns = ws.range(f'B{start - 1}:BM{start - 1}').value
        df_out  = pd.DataFrame(ws.range(f'B{start}:BM{rows}').value, columns=columns)

    _clear_range_dynamic(ws, start + 1, 'B', 'BM')

    df_addition = df_join[
        ["Element ID (PBARL)", "Property ID (PBARL)", "Property ID", "Subcase ID"]
    ].reset_index(drop=True)
    df_addition.rename(columns={
        "Element ID (PBARL)": "Element ID",
        "Property ID (PBARL)": "Property ID",
    }, inplace=True)

    df_out = pd.concat((df_addition, df_out), axis=1)
    end = time.time()
    dt  = end - begin
    return "Results", df_out, dt


# =============================================================================
# BDF LOADER THREAD
# =============================================================================
class BDFLoader(QThread):
    done = pyqtSignal(object, str)

    def __init__(self, path: str):
        super().__init__()
        self.path = path

    def run(self):
        try:
            bdf = my_read_bdf(self.path)
            self.done.emit(bdf, "")
        except Exception as exc:
            self.done.emit(None, str(exc))


# =============================================================================
# CALC THREAD
# =============================================================================
class CalcThread(QThread):
    done    = pyqtSignal(str, str)
    status  = pyqtSignal(str)

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

            self.status.emit("Reading fastpph CSV…")
            df_fast = pd.read_csv(
                self.fastpph_path, skiprows=2, sep=None, engine="python")
            df_fast.columns = df_fast.columns.str.strip()
            df_fast.rename(columns={"elem 1 id": "Element ID"}, inplace=True)

            fastpph_required = ["Element ID", "Component Name", "LoadCase Name",
                                 "Fx", "Fy", "Fz"]
            missing = [c for c in fastpph_required if c not in df_fast.columns]
            if missing:
                raise ValueError(f"fastpph CSV missing columns: {missing}")

            df_fast["Element ID"] = pd.to_numeric(
                df_fast["Element ID"], errors="coerce").astype("Int64")

            df_fast["Element ID (PBARL)"] = df_fast["Element ID"]
            if "Property ID" in df_fast.columns:
                df_fast["Property ID (PBARL)"] = df_fast["Property ID"]
            if "Subcase ID" not in df_fast.columns and "LoadCase Name" in df_fast.columns:
                df_fast["Subcase ID"] = df_fast["LoadCase Name"]

            self.status.emit("Reading JOINT CSV…")
            df_joint = pd.read_csv(self.joint_path)
            df_joint.columns = df_joint.columns.str.strip()
            df_joint["Element ID"] = pd.to_numeric(
                df_joint["Element ID"], errors="coerce").astype("Int64")

            joint_keep = ["Element ID"]
            for col in ("Fastener Name", "Fastener Diameter", "Collar Name"):
                if col in df_joint.columns:
                    joint_keep.append(col)
                else:
                    self.status.emit(f"WARNING: JOINT CSV missing '{col}'")
            if "Fastener Diameter" not in df_joint.columns:
                raise ValueError("JOINT CSV must contain 'Fastener Diameter'.")
            df_joint = df_joint[joint_keep]

            self.status.emit("Merging datasets…")
            df = pd.merge(df_fast, df_joint, on="Element ID", how="left")

            if "box dimension" in df.columns:
                df["Pitch"] = (df["box dimension"].astype(float) /
                               df["Fastener Diameter"].astype(float))

            self.status.emit("Extracting BDF properties…")
            prop_results = df["Component Name"].apply(
                lambda x: get_property_info(x, self.bdf))
            df[["MAT ID", "MAT_bdf", "n1_bdf", "n2_bdf", "n3_bdf", "n4_bdf"]] = \
                pd.DataFrame(prop_results.tolist(), index=df.index)
            if "MAT" not in df.columns:
                df["MAT"] = df["MAT_bdf"]

            bad = df["MAT ID"].isna().sum()
            if bad:
                self.status.emit(f"WARNING: {bad} rows had unresolvable BDF properties.")

            df["prop_type"] = df["Component Name"].apply(
                lambda x: parse_comp_name(x)[0])
            df_metal     = df[df["prop_type"] == "PSHELL"].reset_index(drop=True)
            df_composite = df[df["prop_type"] == "PCOMP" ].reset_index(drop=True)

            df_metal.to_excel("metal.xlsx")
            df_composite.to_excel("composite.xlsx")

            run_metal     = self.run_metal     and not df_metal.empty
            run_composite = self.run_composite and not df_composite.empty

            if not run_metal and not run_composite:
                raise ValueError("No PSHELL or PCOMP rows found — nothing to calculate.")

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
                        self.status.emit(f"Calculating {len(df_metal)} metallic joints…")
                        _, df_metal_out, dt_m = rf_joint_metal(wb, df_metal)
                        self.status.emit(
                            f"Metallic done in {dt_m:.1f}s — {len(df_metal_out)} rows")

                    if run_composite:
                        self.status.emit(f"Calculating {len(df_composite)} composite joints…")
                        _, df_composite_out, dt_c = rf_joint_composite(wb, df_composite)
                        self.status.emit(
                            f"Composite done in {dt_c:.1f}s — {len(df_composite_out)} rows")
                finally:
                    wb.close(save_changes=False)
            finally:
                xw_app.screen_updating = True
                xw_app.quit()

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            os.makedirs(self.output_dir, exist_ok=True)

            elapsed = (datetime.now() - process_start).total_seconds()
            df_info = pd.DataFrame({
                "Item": ["fastpph CSV", "JOINT CSV", "XLSM File",
                         "Process Start", "Elapsed (s)"],
                "Value": [
                    os.path.abspath(self.fastpph_path),
                    os.path.abspath(self.joint_path),
                    os.path.abspath(self.xlsm),
                    process_start.strftime("%Y-%m-%d %H:%M:%S"),
                    f"{elapsed:.2f}",
                ],
            })

            last_out = ""
            if df_metal_out is not None:
                self.status.emit("Writing metallic results…")
                last_out = os.path.join(self.output_dir, f"Metal_Results_{ts}.xlsx")
                with pd.ExcelWriter(last_out, engine="openpyxl") as writer:
                    df_metal_out.to_excel(writer, sheet_name="Results", index=False)
                    df_info.to_excel(writer, sheet_name="Info", index=False)

            if df_composite_out is not None:
                self.status.emit("Writing composite results…")
                last_out = os.path.join(self.output_dir, f"Composite_Results_{ts}.xlsx")
                with pd.ExcelWriter(last_out, engine="openpyxl") as writer:
                    df_composite_out.to_excel(writer, sheet_name="Results", index=False)
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

        self.bdf            = None
        self.df_fastpph     = None
        self.df_joint       = None
        self.df_output      = None
        self._xlsm_path     = None
        self._fastpph_path  = None
        self._joint_path    = None
        self._discrete_actors = []

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
        self._verts_per_cyl = None
        self._legend_actor  = None

        self._theme_name = "Dark"
        self._init_ui()
        self._apply_theme("Dark")   # sets globals + calls _restyle_all

        self._progress_timer = QTimer()
        self._progress_timer.timeout.connect(self._swing_progress)
        self._progress_val  = 0
        self._progress_dir  = 1

    # =========================================================================
    # THEME
    # =========================================================================
    def _apply_theme(self, name: str):
        global BG, CARD, BORDER, ACCENT, ACCENT_HOV
        global FG, FG_DIM, FG_MONO, SUCCESS, WARN
        global COLOR_SHELL, COLOR_SHELL_EDGE, COLOR_CBUSH

        if name not in THEMES:
            return
        p = THEMES[name]
        BG, CARD, BORDER     = p["BG"], p["CARD"], p["BORDER"]
        ACCENT, ACCENT_HOV   = p["ACCENT"], p["ACCENT_HOV"]
        FG, FG_DIM, FG_MONO  = p["FG"], p["FG_DIM"], p["FG_MONO"]
        SUCCESS, WARN        = p["SUCCESS"], p["WARN"]
        COLOR_SHELL          = p["COLOR_SHELL"]
        COLOR_SHELL_EDGE     = p["COLOR_SHELL_EDGE"]
        COLOR_CBUSH          = ACCENT          # fasteners inherit accent colour
        self._theme_name     = name
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

        # Sync the bottom-bar theme combo to the active theme
        if hasattr(self, "theme_combo"):
            self.theme_combo.blockSignals(True)
            self.theme_combo.setCurrentText(
                f"{THEME_ICONS[self._theme_name]}  {self._theme_name}")
            self.theme_combo.blockSignals(False)

        if self.plotter:
            vp_bg = THEMES[self._theme_name]["VP_BG"]
            self.plotter.set_background(vp_bg)
            axes_color = THEMES[self._theme_name]["AXES_COLOR"]
            try:
                self.plotter.add_axes(color=axes_color,
                                      viewport=(0.0, 0.0, 0.10, 0.13))
            except Exception:
                pass
            if self._shell_actor:
                try:
                    self._shell_actor.GetProperty().SetColor(
                        pv.Color(COLOR_SHELL).float_rgb)
                    self._shell_actor.GetProperty().SetEdgeColor(
                        pv.Color(COLOR_SHELL_EDGE).float_rgb)
                except Exception:
                    pass
            self.plotter.render()

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
        if self._theme_name == "Win95":
            return f"""
                QWidget {{ background-color:{CARD}; border-top:2px solid #808080; }}
                QLabel  {{ font-size:10px; color:{FG}; background:transparent;
                           font-family:'MS Sans Serif', Arial; }}
                QComboBox {{
                    border:2px solid; border-color:#808080 #ffffff #ffffff #808080;
                    border-radius:0px; padding:2px 6px; font-size:10px;
                    background:#ffffff; color:#000000;
                    font-family:'MS Sans Serif', Arial;
                }}
                QComboBox::drop-down {{ border:none; width:16px; }}
                QComboBox QAbstractItemView {{
                    background:#ffffff; color:#000000;
                    border:1px solid #808080;
                    selection-background-color:{ACCENT};
                    selection-color:#ffffff;
                    font-family:'MS Sans Serif', Arial;
                }}
                QPushButton {{
                    border:2px solid; border-color:#ffffff #808080 #808080 #ffffff;
                    border-radius:0px; padding:3px 10px; font-size:10px;
                    background:{CARD}; color:{FG}; font-weight:600;
                    font-family:'MS Sans Serif', Arial;
                }}
                QPushButton:hover   {{ background:#d4d0c8; }}
                QPushButton:checked {{
                    border-color:#808080 #ffffff #ffffff #808080;
                    background:#d4d0c8;
                }}
                QPushButton:pressed {{
                    border-color:#808080 #ffffff #ffffff #808080;
                }}
                QSlider::groove:horizontal {{
                    height:4px; background:#808080; border-radius:0px;
                }}
                QSlider::handle:horizontal {{
                    background:{CARD}; border:2px solid;
                    border-color:#ffffff #808080 #808080 #ffffff;
                    width:12px; height:16px; margin:-6px 0; border-radius:0px;
                }}
                QSlider::sub-page:horizontal {{
                    background:{ACCENT}; border-radius:0px;
                }}
            """
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

        bl.addWidget(self._bar_label("Load case:"))
        self.loadcase_combo = QComboBox()
        self.loadcase_combo.setFixedWidth(160)
        self.loadcase_combo.addItem("All")
        self.loadcase_combo.setEnabled(False)
        self.loadcase_combo.currentIndexChanged.connect(self._on_loadcase_changed)
        bl.addWidget(self.loadcase_combo)
        bl.addWidget(self._vsep())

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

        bl.addWidget(self._bar_label("Theme:"))
        self.theme_combo = QComboBox()
        self.theme_combo.setFixedWidth(130)
        for name, icon in THEME_ICONS.items():
            self.theme_combo.addItem(f"{icon}  {name}")
        self.theme_combo.setCurrentText(f"{THEME_ICONS[self._theme_name]}  {self._theme_name}")
        self.theme_combo.currentTextChanged.connect(
            lambda text: self._apply_theme(text.split("  ", 1)[-1].strip()))
        bl.addWidget(self.theme_combo)
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
        if self._theme_name == "Win95":
            return f"""
            QGroupBox {{
                background-color:{CARD};
                border:2px solid; border-color:#ffffff #808080 #808080 #ffffff;
                border-radius:0px;
                margin-top:12px; padding-top:12px;
                font-weight:bold; font-size:10px; color:{FG};
                font-family:"MS Sans Serif", Arial;
            }}
            QGroupBox::title {{
                subcontrol-origin:margin; subcontrol-position:top left;
                left:8px; padding:0 4px; color:{FG};
                background-color:{CARD};
            }}
            QLabel    {{ font-size:11px; color:{FG}; background:transparent;
                         font-family:"MS Sans Serif", Arial; }}
            QCheckBox {{ font-size:11px; color:{FG}; background:transparent;
                         font-family:"MS Sans Serif", Arial; }}
            QCheckBox::indicator {{
                width:13px; height:13px;
                border:2px solid; border-color:#808080 #ffffff #ffffff #808080;
                background:{BG};
            }}
            QCheckBox::indicator:checked {{ background:{ACCENT}; }}
            """
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
        if self._theme_name == "Win95":
            return (f"border:2px solid; border-color:#808080 #ffffff #ffffff #808080;"
                    f"border-radius:0px; padding:3px 6px; font-size:11px;"
                    f"background:#ffffff; color:#000000;"
                    f"font-family:'MS Sans Serif', Arial;")
        return (f"border:1px solid {BORDER}; border-radius:4px;"
                f"padding:4px 7px; font-size:11px; background:{BG}; color:{FG};")

    def _btn_style(self):
        if self._theme_name == "Win95":
            return (f"border:2px solid; border-color:#ffffff #808080 #808080 #ffffff;"
                    f"border-radius:0px; padding:4px 8px; font-size:10px;"
                    f"background:{CARD}; color:{FG}; font-weight:600;"
                    f"font-family:'MS Sans Serif', Arial;")
        return (f"border:1px solid {BORDER}; border-radius:4px;"
                f"padding:5px 8px; font-size:10px;"
                f"background:{BG}; color:{FG}; font-weight:600;")

    def _small_btn_style(self):
        return (f"border:1px solid {BORDER}; border-radius:3px;"
                f"background:{BG}; color:{FG}; font-size:10px;")

    def _calc_btn_style(self, disabled: bool = False):
        if self._theme_name == "Win95":
            if disabled:
                return (f"QPushButton {{ background-color:{CARD}; color:#808080;"
                        f"border:2px solid; border-color:#ffffff #808080 #808080 #ffffff;"
                        f"border-radius:0px; padding:7px; font-weight:bold; font-size:11px;"
                        f"font-family:'MS Sans Serif', Arial; }}")
            return f"""
                QPushButton {{
                    background-color:{CARD}; color:{FG};
                    border:2px solid; border-color:#ffffff #808080 #808080 #ffffff;
                    border-radius:0px; padding:7px;
                    font-weight:bold; font-size:11px;
                    font-family:'MS Sans Serif', Arial;
                }}
                QPushButton:hover    {{ background-color:#d4d0c8; }}
                QPushButton:pressed  {{
                    border-color:#808080 #ffffff #ffffff #808080;
                    padding:8px 6px 6px 8px;
                }}
                QPushButton:disabled {{ color:#808080; }}
            """
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
    # LOG / PROGRESS
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
        self._legend_actor  = None

        pts, nmap = self._build_pts()

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
    # OPACITY SLIDER
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
    # LOAD CASE FILTERING  +  ENVELOPE (All)
    # =========================================================================
    def _get_filtered_fastpph(self) -> "pd.DataFrame | None":
        if self.df_fastpph is None:
            return None

        lc = self.loadcase_combo.currentText() if hasattr(self, "loadcase_combo") else "All"

        if lc != "All" and "LoadCase Name" in self.df_fastpph.columns:
            filtered = self.df_fastpph[
                self.df_fastpph["LoadCase Name"].astype(str) == lc
            ]
            return filtered.reset_index(drop=True)

        if "Element ID" not in self.df_fastpph.columns:
            return self.df_fastpph

        numeric_cols = self.df_fastpph.select_dtypes(include=[np.number]).columns.tolist()
        primary_col  = "Fx" if "Fx" in numeric_cols else (numeric_cols[0] if numeric_cols else None)

        envelope_rows = []
        for eid, grp in self.df_fastpph.groupby("Element ID"):
            if primary_col is not None and primary_col in grp.columns:
                numeric = pd.to_numeric(grp[primary_col], errors="coerce")
                idx_max_abs = numeric.abs().idxmax()
                base_row    = grp.loc[idx_max_abs].copy()
            else:
                base_row = grp.iloc[0].copy()

            for col in numeric_cols:
                if col not in grp.columns:
                    continue
                series = pd.to_numeric(grp[col], errors="coerce").dropna()
                if series.empty:
                    continue
                extreme_idx = series.abs().idxmax()
                base_row[col] = series.loc[extreme_idx]

            envelope_rows.append(base_row)

        if not envelope_rows:
            return self.df_fastpph

        df_env = pd.DataFrame(envelope_rows).reset_index(drop=True)
        if "LoadCase Name" in df_env.columns:
            df_env["LoadCase Name"] = "Envelope"
        return df_env

    def _on_loadcase_changed(self):
        self._update_loadcase_info()
        self._recolor_cbush()

    def _update_loadcase_info(self):
        df = self._get_filtered_fastpph()
        lc = self.loadcase_combo.currentText() if hasattr(self, "loadcase_combo") else "All"
        label = "Envelope (all LCs)" if lc == "All" else f"Load case '{lc}'"
        if df is None or df.empty:
            self._log(f"{label}: no fastpph data.")
            return
        lines = [f"{label} — {len(df)} elements"]
        for col in ("Fx", "Fy", "Fz"):
            if col in df.columns:
                s = pd.to_numeric(df[col], errors="coerce").dropna()
                if not s.empty:
                    lines.append(
                        f"  {col}: min={s.min():.4g}  max={s.max():.4g}  mean={s.mean():.4g}")
        self._log("\n".join(lines))

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
            df["Element ID"] = pd.to_numeric(
                df["Element ID"], errors="coerce").astype("Int64")
            self.df_joint    = df
            self._joint_path = fn
            self._update_col_combo()
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
    # COLUMN COMBO  (also populates load case combo)
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

        if self.df_fastpph is not None and "LoadCase Name" in self.df_fastpph.columns:
            prev_lc = self.loadcase_combo.currentText()
            self.loadcase_combo.blockSignals(True)
            self.loadcase_combo.clear()
            self.loadcase_combo.addItem("All")
            for lc in sorted(self.df_fastpph["LoadCase Name"].dropna().unique()):
                self.loadcase_combo.addItem(str(lc))
            self.loadcase_combo.setEnabled(True)
            lc_idx = self.loadcase_combo.findText(prev_lc)
            self.loadcase_combo.setCurrentIndex(lc_idx if lc_idx >= 0 else 0)
            self.loadcase_combo.blockSignals(False)

    def _on_col_changed(self):
        pass

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
                if cfg[1] == "df_fastpph":
                    df = self._get_filtered_fastpph()
                else:
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

        if not hasattr(self, '_discrete_actors'):
            self._discrete_actors = []
        for a in self._discrete_actors:
            try:
                self.plotter.remove_actor(a)
            except Exception:
                pass
        self._discrete_actors = []

        if self._legend_actor is not None:
            try:
                self.plotter.remove_actor(self._legend_actor)
            except Exception:
                pass
            self._legend_actor = None

        col_text = self.col_combo.currentText()
        eids = list(self._cbush_endpoints.keys())
        p1s  = np.array([self._cbush_endpoints[e][0] for e in eids], dtype=float)
        p2s  = np.array([self._cbush_endpoints[e][1] for e in eids], dtype=float)
        r    = (self._base_radius or _cbush_radius(self._cached_bounds, 1.0)) \
            * self._radius_scale

        if col_text == "Default":
            mesh, V = _make_fastener_mesh(p1s, p2s, r)
            self._verts_per_cyl = V
            self._cbush_actor   = self.plotter.add_mesh(
                mesh, color=COLOR_CBUSH, smooth_shading=True,
                opacity=1.0, pickable=True, show_scalar_bar=False)
            self.plotter.render()
            self._refresh_labels()
            return

        cfg = next((c for c in COLUMN_CONFIG if c[0] == col_text), None)
        if cfg is None:
            self._stop_busy(f"Column '{col_text}' not in configuration.")
            return

        col, df_attr, is_discrete = cfg

        if df_attr == "df_fastpph":
            df = self._get_filtered_fastpph()
        else:
            df = getattr(self, df_attr, None)

        if df is None or col not in df.columns:
            self._stop_busy(f"Column '{col}' not available.")
            return

        eid_to_val = {}
        for _, row in df.iterrows():
            try:
                eid_to_val[int(row["Element ID"])] = row[col]
            except Exception:
                pass

        self._log(f"eid_to_val keys sample: {list(eid_to_val.keys())[:5]}")
        self._log(f"eids sample: {eids[:5]}")
        matched = [e for e in eids if e in eid_to_val]
        self._log(f"matched: {len(matched)}/{len(eids)}")

        vals   = [eid_to_val.get(e) for e in eids]
        is_num = not is_discrete and all(
            isinstance(v, (int, float)) and not (isinstance(v, float) and np.isnan(v))
            for v in vals if v is not None)

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

        else:
            unique = sorted({str(eid_to_val[e]) for e in eids if e in eid_to_val})
            palette = [
                ACCENT,    "#22c55e", "#f59e0b", "#ef4444",
                "#a855f7", "#06b6d4", "#f97316", "#84cc16",
                "#ec4899", "#14b8a6", "#fb923c", "#a3e635",
            ]
            v2c = {v: palette[i % len(palette)] for i, v in enumerate(unique)}

            for val in unique:
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

            unmatched_idx = [i for i, e in enumerate(eids) if e not in eid_to_val]
            if unmatched_idx:
                vp1s = p1s[unmatched_idx]
                vp2s = p2s[unmatched_idx]
                mesh, V = _make_fastener_mesh(vp1s, vp2s, r)
                actor = self.plotter.add_mesh(
                    mesh, color="#444444", smooth_shading=True,
                    opacity=1.0, pickable=True, show_scalar_bar=False)
                self._discrete_actors.append(actor)

            try:
                legend_entries = [[str(v), v2c[v]] for v in unique]
                if unmatched_idx:
                    legend_entries.append(["No data", "#444444"])
                if legend_entries:
                    self._legend_actor = self.plotter.add_legend(
                        labels=legend_entries,
                        face="rectangle",
                        size=(0.18, min(0.06 + len(legend_entries) * 0.05, 0.50)),
                        loc="upper right",
                        bcolor=None,   # transparent background
                        border=False)  # no border
            except Exception as e:
                self._log(f"Legend error: {e}")

        self.plotter.render()
        self._refresh_labels()

        lc = self.loadcase_combo.currentText() if hasattr(self, "loadcase_combo") else "All"
        lc_tag = " [Envelope]" if lc == "All" else f" [{lc}]"
        self.status_lbl.setText(f"Colored by: {col}{lc_tag}")

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
        self._calc_thread.status.connect(self._log)
        self._calc_thread.done.connect(self._on_calc_done)
        self._calc_thread.start()

    def _on_calc_done(self, result_path: str, err: str):
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
                if cfg[1] == "df_fastpph":
                    df = self._get_filtered_fastpph()
                else:
                    df = getattr(self, cfg[1], None)
                if df is not None and "Element ID" in df.columns and col_text in df.columns:
                    row = df[df["Element ID"].astype(int) == eid]
                    if not row.empty:
                        v       = row.iloc[0][col_text]
                        val_str = f"{v:.4g}" if isinstance(v, float) else str(v)
                        extras = []
                        for fc in ("Fx", "Fy", "Fz"):
                            if fc in df.columns and fc != col_text:
                                fv = row.iloc[0][fc]
                                extras.append(
                                    f"{fc}={fv:.4g}" if isinstance(fv, float) else f"{fc}={fv}")
                        extra_str = "  " + "  ".join(extras) if extras else ""
                        lc = self.loadcase_combo.currentText() if hasattr(self, "loadcase_combo") else "All"
                        lc_tag = "  [Envelope]" if lc == "All" else f"  LC:{lc}"
                        text = f"EID {eid}  {col_text}: {val_str}{extra_str}{lc_tag}"
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
