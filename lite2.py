"""
Fastener Joint Calculator
=========================
Reads fastpph CSV + JOINT CSV + BDF, routes rows to metallic / composite
Excel sheets via xlwings, and writes timestamped result workbooks.

Thread model
------------
  Main thread  : Tkinter mainloop only — NO widget touches from worker.
  Worker thread: all I/O and xlwings work — communicates back via self.after().
"""

import os
import sys
import threading
import tkinter as tk
from collections import Counter
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

import pandas as pd
import xlwings as xw
from pyNastran.bdf.bdf import BDF

# ============================================================
# SHEET / COLUMN CONFIGURATION
# ============================================================
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

# Columns that must survive the groupby agg for INFO_COLS downstream
AGG_FIRST_EXTRA = ["elem 2 id", "elem 2 Node id", "box dimension",
                   "Fastener Diameter", "Pitch", "DLS Ratio"]

# ============================================================
# PALETTE
# ============================================================
BG         = "#0e1117"
CARD       = "#161b27"
BORDER     = "#1e2736"
ACCENT     = "#3b82f6"
ACCENT_HOV = "#2563eb"
FG         = "#e2e8f0"
FG_DIM     = "#64748b"
FG_MONO    = "#94a3b8"
SUCCESS    = "#22c55e"
ERROR      = "#ef4444"

FONT_TITLE = ("Segoe UI", 11, "bold")
FONT_LABEL = ("Segoe UI", 9)
FONT_MONO  = ("Consolas", 8)
FONT_BTN   = ("Segoe UI", 9, "bold")


# ============================================================
# HELPERS
# ============================================================
def _col_letter(col_index: int) -> str:
    """
    Convert a 1-based column index to an Excel letter (A, B, … Z, AA, …).
    Works across all xlwings versions — avoids the utils API change in 0.28+.
    """
    result = ""
    while col_index > 0:
        col_index, rem = divmod(col_index - 1, 26)
        result = chr(65 + rem) + result
    return result


def parse_comp_name(comp_name: str):
    """
    Split 'PSHELL_123' → ('PSHELL', 123).
    Returns (None, None) on any parse failure so the caller can skip the row.
    """
    try:
        parts = str(comp_name).strip().split("_")
        if len(parts) < 2:
            raise ValueError
        prop_type = parts[0].upper()
        prop_id   = int(parts[-1])
        return prop_type, prop_id
    except (ValueError, IndexError):
        return None, None


def get_property_info(comp_name: str, bdf: BDF):
    """
    Extract material id, material name, and ply counts from BDF property.
    Returns a 6-tuple; all fields are None on any failure — callers must guard.
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

            total = round(t / 0.1)
            base  = total // 4
            rem   = total % 4
            n1    = base + rem
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


# ============================================================
# EXCEL CALCULATION HELPERS
# (called from worker thread; wb is already open)
# ============================================================
def _read_sheet_results(ws, n_rows: int, header_row: int = 8,
                         data_start_row: int = 9) -> pd.DataFrame:
    """
    Dynamically detect the last used column, read the header and result block,
    and return a DataFrame.  Handles the xlwings single-row edge-case where
    .value returns a flat list instead of a list-of-lists.
    """
    # Detect last used column from header row starting at B
    last_col_idx  = ws.range(f"B{header_row}").end("right").column
    last_col_ltr  = _col_letter(last_col_idx)
    last_data_row = data_start_row + n_rows - 1

    raw_headers = ws.range(f"B{header_row}:{last_col_ltr}{header_row}").value
    col_names   = [
        str(c).strip() if c is not None else f"col_{i}"
        for i, c in enumerate(raw_headers)
    ]

    raw_data = ws.range(
        f"B{data_start_row}:{last_col_ltr}{last_data_row}").value

    # xlwings returns a flat list for a single-row read — normalise to 2-D
    if n_rows == 1:
        raw_data = [raw_data]

    return pd.DataFrame(raw_data, columns=col_names)


def calculate_metal(df_metal: pd.DataFrame, wb) -> pd.DataFrame:
    ws = wb.sheets[METAL_SHEET]
    ws.range("B9").expand("table").clear_contents()

    paste_data = df_metal[DATA_COLS_METAL].values.tolist()
    ws.range("B9").value = paste_data
    wb.app.calculate()          # single explicit recalc after all writes

    df_results = _read_sheet_results(ws, n_rows=len(paste_data))
    df_info    = df_metal[INFO_COLS].reset_index(drop=True)
    return pd.concat([df_info, df_results], axis=1)


def calculate_composite(df_composite: pd.DataFrame, wb) -> pd.DataFrame:
    ws = wb.sheets[COMPOSITE_SHEET]
    ws.range("B9").expand("table").clear_contents()

    paste_data = df_composite[DATA_COLS_COMPOSITE].values.tolist()
    ws.range("B9").value = paste_data
    wb.app.calculate()

    df_results = _read_sheet_results(ws, n_rows=len(paste_data))
    df_info    = df_composite[INFO_COLS].reset_index(drop=True)
    return pd.concat([df_info, df_results], axis=1)


# ============================================================
# GUI
# ============================================================
class App(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("Fastener Joint Calculator")
        self.resizable(False, False)
        self.configure(bg=BG)

        # Input path vars
        self.fastpph_var   = tk.StringVar()
        self.joint_var     = tk.StringVar()
        self.bdf_var       = tk.StringVar()
        self.xlsm_var      = tk.StringVar()

        # Analysis toggles
        self.run_metal     = tk.BooleanVar(value=True)
        self.run_composite = tk.BooleanVar(value=True)

        # Widget references (set in _build_ui)
        self.status_label  = None
        self.status_var    = None
        self.progress_bar  = None
        self.run_btn       = None

        # Thread tracking
        self._worker_thread: threading.Thread | None = None

        self._apply_theme()
        self._build_ui()

        # Intercept window close to prevent killing a live xlwings session
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # =========================================================
    # WINDOW CLOSE GUARD
    # =========================================================
    def _on_close(self):
        if self._worker_thread and self._worker_thread.is_alive():
            messagebox.showwarning(
                "Calculation Running",
                "A calculation is in progress.\nPlease wait for it to finish before closing.")
            return
        self.destroy()

    # =========================================================
    # THEME
    # =========================================================
    def _apply_theme(self):
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure("Accent.Horizontal.TProgressbar",
                        troughcolor=BORDER, background=ACCENT,
                        darkcolor=ACCENT,   lightcolor=ACCENT,
                        bordercolor=BORDER, thickness=6)

        style.configure("Dark.TCheckbutton",
                        background=CARD, foreground=FG,
                        focuscolor=CARD, font=FONT_LABEL)
        style.map("Dark.TCheckbutton",
                  background=[("active", CARD)],
                  foreground=[("active", FG)])

    # =========================================================
    # WIDGET FACTORIES
    # =========================================================
    def _card(self, parent, **kwargs):
        return tk.Frame(parent, bg=CARD,
                        highlightbackground=BORDER,
                        highlightthickness=1, **kwargs)

    def _label(self, parent, text, font=None, fg=None, **kwargs):
        return tk.Label(parent, text=text,
                        bg=parent["bg"], fg=fg or FG,
                        font=font or FONT_LABEL, **kwargs)

    def _entry(self, parent, var):
        return tk.Entry(parent, textvariable=var, width=44,
                        bg=BG, fg=FG_MONO, insertbackground=FG,
                        relief="flat", font=FONT_MONO,
                        highlightbackground=BORDER,
                        highlightthickness=1,
                        highlightcolor=ACCENT)

    def _browse_btn(self, parent, var, ftypes):
        btn = tk.Button(parent, text="…", width=3,
                        bg=BORDER, fg=FG,
                        activebackground=ACCENT, activeforeground="#ffffff",
                        relief="flat", font=FONT_BTN, cursor="hand2",
                        command=lambda: self._browse(var, ftypes))
        btn.bind("<Enter>", lambda _e: btn.config(bg=ACCENT))
        btn.bind("<Leave>", lambda _e: btn.config(bg=BORDER))
        return btn

    # =========================================================
    # UI CONSTRUCTION
    # =========================================================
    def _build_ui(self):
        outer = tk.Frame(self, bg=BG)
        outer.pack(padx=20, pady=20)

        # ── Header ──────────────────────────────────────────
        hdr = tk.Frame(outer, bg=BG)
        hdr.pack(fill="x", pady=(0, 16))
        tk.Label(hdr, text="FASTENER JOINT", bg=BG, fg=ACCENT,
                 font=("Consolas", 18, "bold")).pack(side="left")
        tk.Label(hdr, text=" CALCULATOR", bg=BG, fg=FG,
                 font=("Segoe UI", 18, "bold")).pack(side="left")

        tk.Label(outer,
                 text="Structural analysis automation for metallic & composite joints",
                 bg=BG, fg=FG_DIM, font=FONT_MONO).pack(anchor="w", pady=(0, 18))

        # ── Input files card ─────────────────────────────────
        files_card = self._card(outer)
        files_card.pack(fill="x", pady=(0, 10))

        self._label(files_card, "  INPUT FILES",
                    font=("Consolas", 8, "bold"), fg=FG_DIM).pack(
            anchor="w", padx=12, pady=(10, 6))
        tk.Frame(files_card, bg=BORDER, height=1).pack(fill="x", padx=12)

        fields = [
            ("fastpph CSV", self.fastpph_var, [("CSV Files", "*.csv")]),
            ("JOINT CSV",   self.joint_var,   [("CSV Files", "*.csv")]),
            ("BDF File",    self.bdf_var,     [("BDF Files", "*.bdf"),
                                               ("All Files", "*.*")]),
            ("XLSM File",   self.xlsm_var,    [("Excel Macro Files", "*.xlsm")]),
        ]
        for label, var, ftypes in fields:
            row = tk.Frame(files_card, bg=CARD)
            row.pack(fill="x", padx=12, pady=5)
            self._label(row, label, fg=FG_DIM, width=13, anchor="w").pack(side="left")
            self._entry(row, var).pack(side="left", padx=(6, 6))
            self._browse_btn(row, var, ftypes).pack(side="left")

        tk.Frame(files_card, bg=BG, height=8).pack()

        # ── Analysis type card ───────────────────────────────
        opt_card = self._card(outer)
        opt_card.pack(fill="x", pady=(0, 14))

        self._label(opt_card, "  ANALYSIS TYPE",
                    font=("Consolas", 8, "bold"), fg=FG_DIM).pack(
            anchor="w", padx=12, pady=(10, 6))
        tk.Frame(opt_card, bg=BORDER, height=1).pack(fill="x", padx=12)

        chk_row = tk.Frame(opt_card, bg=CARD)
        chk_row.pack(fill="x", padx=12, pady=10)
        for text, var in [("Metallic  (PSHELL)", self.run_metal),
                          ("Composite (PCOMP)",  self.run_composite)]:
            ttk.Checkbutton(chk_row, text=text, variable=var,
                            style="Dark.TCheckbutton").pack(
                side="left", padx=(0, 28))

        # ── Run button ───────────────────────────────────────
        self.run_btn = tk.Button(
            outer, text="▶  RUN ANALYSIS",
            command=self._on_run,
            bg=ACCENT, fg="#ffffff",
            activebackground=ACCENT_HOV, activeforeground="#ffffff",
            relief="flat", font=FONT_BTN, cursor="hand2", pady=9)
        self.run_btn.pack(fill="x", pady=(0, 14))
        self.run_btn.bind("<Enter>", lambda _e: self.run_btn.config(bg=ACCENT_HOV))
        self.run_btn.bind("<Leave>", lambda _e: self.run_btn.config(bg=ACCENT))

        # ── Progress bar ─────────────────────────────────────
        pb_frame = tk.Frame(outer, bg=BG)
        pb_frame.pack(fill="x", pady=(0, 6))
        self.progress_bar = ttk.Progressbar(
            pb_frame, mode="indeterminate", length=500,
            style="Accent.Horizontal.TProgressbar")
        self.progress_bar.pack(fill="x")

        # ── Status label ─────────────────────────────────────
        self.status_var = tk.StringVar(value="Ready — select files and press Run.")
        self.status_label = tk.Label(
            outer, textvariable=self.status_var,
            bg=BG, fg=FG_DIM, font=FONT_MONO, anchor="w")
        self.status_label.pack(anchor="w")

    # =========================================================
    # THREAD-SAFE UI UPDATERS
    # All widget mutations go through self.after() so they always
    # execute on the main thread, never from the worker thread.
    # =========================================================
    def _set_status(self, msg: str, color: str | None = None):
        """Safe to call from any thread."""
        self.after(0, self._set_status_main, msg, color)

    def _set_status_main(self, msg: str, color: str | None = None):
        """Must only be called on the main thread (via self.after)."""
        self.status_var.set(msg)
        self.status_label.config(foreground=color or FG_DIM)

    def _start_progress(self):
        """Called from main thread only (_on_run)."""
        self.progress_bar.start(12)

    def _stop_progress(self):
        """Safe to call from any thread."""
        self.after(0, self._stop_progress_main)

    def _stop_progress_main(self):
        self.progress_bar.stop()
        self.progress_bar["value"] = 0

    # =========================================================
    # RUN DONE / ERROR  (scheduled onto main thread via after)
    # =========================================================
    def _on_run_done(self, elapsed: float, ts: str):
        self._stop_progress_main()
        self._set_status_main(
            f"✔  Done in {elapsed:.1f}s  ·  {ts}", color=SUCCESS)
        self.run_btn.config(state="normal")

    def _on_run_error(self, msg: str):
        self._stop_progress_main()
        self._set_status_main(f"✖  {msg}", color=ERROR)
        self.run_btn.config(state="normal")
        messagebox.showerror("Error", msg)

    # =========================================================
    # FILE BROWSE
    # =========================================================
    def _browse(self, var: tk.StringVar, filetypes: list):
        path = filedialog.askopenfilename(filetypes=filetypes)
        if path:
            var.set(path)

    # =========================================================
    # RUN HANDLER  (main thread)
    # =========================================================
    def _on_run(self):
        paths = {
            "fastpph CSV": self.fastpph_var.get(),
            "JOINT CSV":   self.joint_var.get(),
            "BDF File":    self.bdf_var.get(),
            "XLSM File":   self.xlsm_var.get(),
        }
        for name, path in paths.items():
            if not path or not os.path.exists(path):
                messagebox.showerror("Missing File",
                                     f"Please select a valid {name}.")
                return

        if not self.run_metal.get() and not self.run_composite.get():
            messagebox.showerror("Nothing Selected",
                                 "Select at least one of Metallic or Composite.")
            return

        # Disable button and start animation BEFORE spawning thread
        self.run_btn.config(state="disabled")
        self._start_progress()

        # Non-daemon so the close guard can detect it and xlwings finally blocks run
        self._worker_thread = threading.Thread(
            target=self._run_process, args=(paths,), daemon=False)
        self._worker_thread.start()

    # =========================================================
    # WORKER THREAD  — zero Tkinter widget touches here
    # =========================================================
    def _run_process(self, paths: dict):
        try:
            process_start  = datetime.now()
            run_metal      = self.run_metal.get()
            run_composite  = self.run_composite.get()

            # ── BDF ───────────────────────────────────────────
            self._set_status("Reading BDF model…")
            bdf = BDF()
            bdf.read_bdf(paths["BDF File"], xref=True)

            # ── fastpph CSV ───────────────────────────────────
            self._set_status("Reading fastpph CSV…")
            df_fast = pd.read_csv(
                paths["fastpph CSV"], skiprows=2, sep=None, engine="python")
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

            # ── JOINT CSV ─────────────────────────────────────
            self._set_status("Reading JOINT CSV…")
            df_joint = pd.read_csv(paths["JOINT CSV"])
            df_joint.columns = df_joint.columns.str.strip()

            # ── Merge ─────────────────────────────────────────
            self._set_status("Merging datasets…")
            df = pd.merge(df_fast, df_joint, on="Element ID", how="left")

            if "Fastener Diameter" not in df.columns:
                raise ValueError(
                    "JOINT CSV must contain a 'Fastener Diameter' column.")

            df["Pitch"] = (df["box dimension"].astype(float) /
                           df["Fastener Diameter"].astype(float))

            # ── Combine duplicate CBUSH rows ──────────────────
            # GROUP_KEYS become the index; every other INFO_COLS field is
            # preserved via "first", load cols are summed, DLS is max.
            self._set_status("Aggregating duplicate CBUSH rows…")

            agg_spec = {c: (c, "sum") for c in LOAD_COLS}
            for col in AGG_FIRST_EXTRA:
                if col in df.columns:
                    agg_spec[col] = (col, "first" if col != "DLS Ratio" else "max")

            df = df.groupby(GROUP_KEYS, as_index=False).agg(**agg_spec)

            # ── BDF properties ────────────────────────────────
            self._set_status("Extracting BDF properties…")
            prop_results = df["Component Name"].apply(
                lambda x: get_property_info(x, bdf))
            df[["MAT ID", "MAT", "n1", "n2", "n3", "n4"]] = pd.DataFrame(
                prop_results.tolist(), index=df.index)

            # Flag rows where property lookup failed
            bad_props = df["MAT ID"].isna().sum()
            if bad_props:
                self._set_status(
                    f"WARNING: {bad_props} rows had unresolvable BDF properties.")

            # ── Split metal / composite ────────────────────────
            df["prop_type"] = df["Component Name"].apply(
                lambda x: parse_comp_name(x)[0])

            df_metal     = df[df["prop_type"] == "PSHELL"].reset_index(drop=True)
            df_composite = df[df["prop_type"] == "PCOMP" ].reset_index(drop=True)

            # Guard: nothing to do
            if run_metal and df_metal.empty:
                self._set_status("WARNING: No PSHELL rows found — metallic skipped.")
                run_metal = False
            if run_composite and df_composite.empty:
                self._set_status("WARNING: No PCOMP rows found — composite skipped.")
                run_composite = False

            if not run_metal and not run_composite:
                raise ValueError(
                    "No PSHELL or PCOMP rows found after filtering — nothing to calculate.")

            # ── xlwings ───────────────────────────────────────
            self._set_status("Opening XLSM workbook…")
            xw_app = xw.App(visible=False, add_book=False)
            xw_app.display_alerts  = False
            xw_app.screen_updating = False
            xw_app.calculation     = "manual"   # we call calculate() explicitly

            df_metal_out     = None
            df_composite_out = None

            try:
                wb = xw_app.books.open(
                    paths["XLSM File"], update_links=False)

                try:
                    if run_metal:
                        self._set_status(
                            f"Calculating {len(df_metal)} metallic joints…")
                        df_metal_out = calculate_metal(df_metal, wb)

                    if run_composite:
                        self._set_status(
                            f"Calculating {len(df_composite)} composite joints…")
                        df_composite_out = calculate_composite(df_composite, wb)

                finally:
                    # Never save the calculator — only close it
                    wb.close(save_changes=False)

            finally:
                xw_app.screen_updating = True
                xw_app.quit()

            # ── Write output workbooks ─────────────────────────
            process_end = datetime.now()
            elapsed     = (process_end - process_start).total_seconds()
            ts          = process_end.strftime("%Y%m%d_%H%M%S")

            # Output next to the script; fall back to CWD if frozen / interactive
            try:
                script_dir = os.path.dirname(os.path.abspath(__file__))
            except NameError:
                script_dir = os.getcwd()

            files_used = {k: os.path.abspath(v) for k, v in paths.items()}
            df_info = pd.DataFrame({
                "Item": list(files_used.keys()) + [
                    "Process Start", "Process End", "Elapsed (s)"],
                "Value": list(files_used.values()) + [
                    process_start.strftime("%Y-%m-%d %H:%M:%S"),
                    process_end.strftime("%Y-%m-%d %H:%M:%S"),
                    f"{elapsed:.2f}",
                ],
            })

            if df_metal_out is not None:
                self._set_status("Writing metallic results…")
                out_path = os.path.join(script_dir, f"Metal_Results_{ts}.xlsx")
                with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
                    df_metal_out.to_excel(writer, sheet_name="Results", index=False)
                    df_info.to_excel(writer,      sheet_name="Info",    index=False)

            if df_composite_out is not None:
                self._set_status("Writing composite results…")
                out_path = os.path.join(script_dir, f"Composite_Results_{ts}.xlsx")
                with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
                    df_composite_out.to_excel(writer, sheet_name="Results", index=False)
                    df_info.to_excel(writer,          sheet_name="Info",    index=False)

            # Schedule the done callback onto the main thread
            self.after(0, self._on_run_done,
                       elapsed, process_end.strftime("%H:%M:%S"))

        except Exception as exc:
            self.after(0, self._on_run_error, str(exc))


# ============================================================
# ENTRY POINT
# ============================================================
if __name__ == "__main__":
    app = App()
    app.mainloop()
