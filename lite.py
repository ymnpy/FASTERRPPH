import pandas as pd
import xlwings as xw
from pyNastran.bdf.bdf import BDF
from collections import Counter
from datetime import datetime
import os

# ============================================================
# CONFIG — update these paths
# ============================================================
FASTPPH_CSV  = r"C:\Users\User\Desktop\newwf\fastpph\load.csv"
JOINT_CSV    = r"C:\Users\User\Desktop\newwf\fastpph\JOINT-235501-20260617.CSV"
BDF_FILE     = r"C:\Users\User\Desktop\newwf\fastpph\fast.bdf"
XLSM_FILE    = r"C:\Users\User\Desktop\newwf\fastpph\calculator.xlsm"   # FIX #1: was missing
OUTPUT_DIR   = r"C:\Users\User\Desktop\newwf\fastpph\output"             # FIX #1: was missing

METAL_SHEET     = "Metal_Joint_Calculation"
COMPOSITE_SHEET = "Composite_Joint_Calculation"

INFO_COLS = [
    "Component Name", "Element ID", "elem 1 Node id",
    "elem 2 id", "elem 2 Node id", "box dimension",
    "file Name", "LoadCase Name"
]

DATA_COLS_METAL = [
    "Fx", "Fy", "Fz",
    "Nx bypass", "Ny bypass", "Nxy bypass",
    "n1", "n2", "n3", "n4"
]

DATA_COLS_COMPOSITE = [
    "Fx", "Fy", "Fz",
    "Nx bypass", "Ny bypass", "Nxy bypass",
    "n1", "n2", "n3", "n4"
]

# ============================================================
# HELPERS
# ============================================================
def parse_comp_name(comp_name):
    parts = str(comp_name).strip().split("_")
    prop_type = parts[0].upper()
    prop_id   = int(parts[-1])
    return prop_type, prop_id


def get_property_info(comp_name, bdf):
    prop_type, pid = parse_comp_name(comp_name)
    mat_id = None
    mat    = None
    n1 = n2 = n3 = n4 = None

    try:
        prop = bdf.properties[pid]
    except KeyError:
        print(f"WARNING: Property {pid} not found in BDF.")
        return mat_id, mat, n1, n2, n3, n4

    if prop_type == "PSHELL":
        t      = prop.t
        mat_id = prop.mid
        mat    = "Al-2024"

        total = round(t / 0.1)
        base  = total // 4
        rem   = total % 4

        n1 = base + rem
        n2 = base
        n3 = base
        n4 = base

    elif prop_type == "PCOMP":
        mat_id = prop.mids[0]
        mat    = "TT84"

        thetas = prop.thetas
        counts = Counter(thetas)

        n1 = counts.get(0,   0)
        n2 = counts.get(45,  0)
        n3 = counts.get(-45, 0)
        n4 = counts.get(90,  0)

    return mat_id, mat, n1, n2, n3, n4


# ============================================================
# CALCULATE METAL
# ============================================================
def calculate_metal(df_metal, wb):
    ws = wb.sheets[METAL_SHEET]

    ws.range("B9").expand("table").clear_contents()

    data_cols  = DATA_COLS_METAL
    paste_data = df_metal[data_cols].values.tolist()
    ws.range("B9").value = paste_data

    wb.app.calculate()

    n_rows   = len(paste_data)
    last_row = 9 + n_rows - 1

    col_names = ws.range("B8:Z8").value
    col_names = [str(c).strip() if c is not None else f"col_{i}"
                 for i, c in enumerate(col_names)]

    results_range = ws.range(f"B9:Z{last_row}").value
    df_results = pd.DataFrame(results_range, columns=col_names)

    df_info = df_metal[INFO_COLS].reset_index(drop=True)
    df_out  = pd.concat([df_info, df_results], axis=1)

    return df_out


# ============================================================
# CALCULATE COMPOSITE
# ============================================================
def calculate_composite(df_composite, wb):
    ws = wb.sheets[COMPOSITE_SHEET]

    ws.range("B9").expand("table").clear_contents()

    data_cols  = DATA_COLS_COMPOSITE
    paste_data = df_composite[data_cols].values.tolist()
    ws.range("B9").value = paste_data

    wb.app.calculate()

    n_rows   = len(paste_data)
    last_row = 9 + n_rows - 1

    col_names = ws.range("B8:Z8").value
    col_names = [str(c).strip() if c is not None else f"col_{i}"
                 for i, c in enumerate(col_names)]

    results_range = ws.range(f"B9:Z{last_row}").value
    df_results = pd.DataFrame(results_range, columns=col_names)

    df_info = df_composite[INFO_COLS].reset_index(drop=True)
    df_out  = pd.concat([df_info, df_results], axis=1)

    return df_out


# ============================================================
# MAIN  (FIX #2: now actually callable and complete)
# ============================================================
def main():
    process_start = datetime.now()

    print("Reading BDF...")
    bdf = BDF()
    bdf.read_bdf(BDF_FILE, xref=True)

    print("Reading fastpph.csv...")
    df_fast = pd.read_csv(FASTPPH_CSV, skiprows=2, sep=None, engine="python")
    df_fast.columns = df_fast.columns.str.strip()

    keep_cols = [
        "Component Name", "elem 1 id", "elem 1 Node id",
        "elem 2 id", "elem 2 Node id", "box dimension",
        "file Name", "LoadCase Name",
        "Fx", "Fy", "Fz",
        "Nx bypass", "Ny bypass", "Nxy bypass",
        "Mx total", "My total", "Mxy total",
        "DLS Ratio"
    ]
    df_fast = df_fast[keep_cols].copy()
    df_fast.rename(columns={"elem 1 id": "Element ID"}, inplace=True)

    print("Merging with JOINT.csv...")
    df_joint = pd.read_csv(JOINT_CSV)
    df = pd.merge(df_fast, df_joint, on="Element ID", how="left")
    df["Pitch"] = df["box dimension"].astype(float) / df["Fastener Diameter"].astype(float)

    print("Reading properties from BDF...")
    results = df["Component Name"].apply(lambda x: get_property_info(x, bdf))
    df[["MAT ID", "MAT", "n1", "n2", "n3", "n4"]] = pd.DataFrame(
        results.tolist(), index=df.index
    )

    df["prop_type"] = df["Component Name"].apply(lambda x: parse_comp_name(x)[0])
    df_metal     = df[df["prop_type"] == "PSHELL"].reset_index(drop=True)
    df_composite = df[df["prop_type"] == "PCOMP"].reset_index(drop=True)

    print(f"Metal rows     : {len(df_metal)}")
    print(f"Composite rows : {len(df_composite)}")

    print("Opening xlsm...")
    app = xw.App(visible=False)
    try:
        wb = xw.Book(XLSM_FILE)

        print("Calculating metal...")
        df_metal_out = calculate_metal(df_metal, wb)

        print("Calculating composite...")
        df_composite_out = calculate_composite(df_composite, wb)

        wb.save()
        wb.close()
    finally:
        app.quit()

    process_end = datetime.now()
    elapsed     = (process_end - process_start).total_seconds()
    ts          = process_end.strftime("%Y%m%d_%H%M%S")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    metal_output     = os.path.join(OUTPUT_DIR, f"Metal_Results_{ts}.xlsx")
    composite_output = os.path.join(OUTPUT_DIR, f"Composite_Results_{ts}.xlsx")

    files_used = {
        "fastpph CSV" : os.path.abspath(FASTPPH_CSV),
        "JOINT CSV"   : os.path.abspath(JOINT_CSV),
        "BDF File"    : os.path.abspath(BDF_FILE),
        "XLSM File"   : os.path.abspath(XLSM_FILE),
    }
    info_data = {
        "Item": list(files_used.keys()) + ["Process Start", "Process End", "Elapsed (s)"],
        "Value": list(files_used.values()) + [
            process_start.strftime("%Y-%m-%d %H:%M:%S"),
            process_end.strftime("%Y-%m-%d %H:%M:%S"),
            f"{elapsed:.2f}"
        ]
    }
    df_info = pd.DataFrame(info_data)

    print(f"Writing {metal_output}...")
    with pd.ExcelWriter(metal_output, engine="openpyxl") as writer:
        df_metal_out.to_excel(writer, sheet_name="Results", index=False)
        df_info.to_excel(writer, sheet_name="Info", index=False)

    print(f"Writing {composite_output}...")
    with pd.ExcelWriter(composite_output, engine="openpyxl") as writer:
        df_composite_out.to_excel(writer, sheet_name="Results", index=False)
        df_info.to_excel(writer, sheet_name="Info", index=False)

    print(f"Done! Elapsed: {elapsed:.2f}s")


# ============================================================
# GUI
# ============================================================
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading

# ── Palette ─────────────────────────────────────────────────
BG         = "#0e1117"   # near-black background
CARD       = "#161b27"   # slightly lighter card surface
BORDER     = "#1e2736"   # subtle card border
ACCENT     = "#3b82f6"   # electric blue — engineering/aerospace feel
ACCENT_HOV = "#2563eb"   # darker blue on hover
FG         = "#e2e8f0"   # primary text
FG_DIM     = "#64748b"   # dimmed labels
FG_MONO    = "#94a3b8"   # monospaced secondary text
SUCCESS    = "#22c55e"   # green
ERROR      = "#ef4444"   # red

FONT_TITLE = ("Segoe UI", 11, "bold")
FONT_LABEL = ("Segoe UI", 9)
FONT_MONO  = ("Consolas", 8)
FONT_BTN   = ("Segoe UI", 9, "bold")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Fastener Joint Calculator")
        self.resizable(False, False)
        self.configure(bg=BG)

        self.fastpph_var = tk.StringVar()
        self.joint_var   = tk.StringVar()
        self.bdf_var     = tk.StringVar()
        self.xlsm_var    = tk.StringVar()

        self.run_metal     = tk.BooleanVar(value=True)
        self.run_composite = tk.BooleanVar(value=True)

        self.status_label = None
        self.progress_bar = None

        self._apply_theme()
        self._build_ui()

    # ── ttk theme overrides ───────────────────────────────────
    def _apply_theme(self):
        style = ttk.Style(self)
        style.theme_use("clam")

        # Progress bar: trough is dark, bar is accent blue
        style.configure("Accent.Horizontal.TProgressbar",
                         troughcolor=BORDER,
                         background=ACCENT,
                         darkcolor=ACCENT,
                         lightcolor=ACCENT,
                         bordercolor=BORDER,
                         thickness=6)

        # Checkbutton
        style.configure("Dark.TCheckbutton",
                         background=CARD,
                         foreground=FG,
                         focuscolor=CARD,
                         font=FONT_LABEL)
        style.map("Dark.TCheckbutton",
                  background=[("active", CARD)],
                  foreground=[("active", FG)])

        # Separator
        style.configure("Dark.TSeparator", background=BORDER)

    # ── helpers ───────────────────────────────────────────────
    def _card(self, parent, **kwargs):
        """A rounded-feel bordered frame that acts as a card."""
        return tk.Frame(parent, bg=CARD,
                        highlightbackground=BORDER,
                        highlightthickness=1, **kwargs)

    def _label(self, parent, text, font=None, fg=None, **kwargs):
        return tk.Label(parent, text=text,
                        bg=parent["bg"],
                        fg=fg or FG,
                        font=font or FONT_LABEL,
                        **kwargs)

    def _entry(self, parent, var):
        e = tk.Entry(parent, textvariable=var, width=44,
                     bg=BG, fg=FG_MONO, insertbackground=FG,
                     relief="flat", font=FONT_MONO,
                     highlightbackground=BORDER,
                     highlightthickness=1,
                     highlightcolor=ACCENT)
        return e

    def _browse_btn(self, parent, var, ftypes):
        btn = tk.Button(parent, text="…", width=3,
                        bg=BORDER, fg=FG, activebackground=ACCENT,
                        activeforeground="#ffffff",
                        relief="flat", font=FONT_BTN, cursor="hand2",
                        command=lambda: self._browse(var, ftypes))
        btn.bind("<Enter>", lambda e: btn.config(bg=ACCENT))
        btn.bind("<Leave>", lambda e: btn.config(bg=BORDER))
        return btn

    # ── UI construction ───────────────────────────────────────
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

        tk.Label(outer, text="Structural analysis automation for metallic & composite joints",
                 bg=BG, fg=FG_DIM, font=FONT_MONO).pack(anchor="w", pady=(0, 18))

        # ── Input files card ────────────────────────────────
        files_card = self._card(outer)
        files_card.pack(fill="x", pady=(0, 10))

        self._label(files_card, "  INPUT FILES",
                    font=("Consolas", 8, "bold"), fg=FG_DIM).pack(
            anchor="w", padx=12, pady=(10, 6))

        tk.Frame(files_card, bg=BORDER, height=1).pack(fill="x", padx=12)

        fields = [
            ("fastpph CSV", self.fastpph_var, [("CSV Files", "*.csv")]),
            ("JOINT CSV",   self.joint_var,   [("CSV Files", "*.csv")]),
            ("BDF File",    self.bdf_var,     [("BDF Files", "*.bdf"), ("All Files", "*.*")]),
            ("XLSM File",   self.xlsm_var,    [("Excel Macro Files", "*.xlsm")]),
        ]

        for label, var, ftypes in fields:
            row = tk.Frame(files_card, bg=CARD)
            row.pack(fill="x", padx=12, pady=5)

            self._label(row, label, fg=FG_DIM, width=13, anchor="w").pack(side="left")
            self._entry(row, var).pack(side="left", padx=(6, 6))
            self._browse_btn(row, var, ftypes).pack(side="left")

        tk.Frame(files_card, bg=BG, height=8).pack()   # bottom padding

        # ── Analysis type card ──────────────────────────────
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
            cb = ttk.Checkbutton(chk_row, text=text, variable=var,
                                 style="Dark.TCheckbutton")
            cb.pack(side="left", padx=(0, 28))

        # ── Run button ──────────────────────────────────────
        run_btn = tk.Button(outer, text="▶  RUN ANALYSIS",
                            command=self._on_run,
                            bg=ACCENT, fg="#ffffff",
                            activebackground=ACCENT_HOV,
                            activeforeground="#ffffff",
                            relief="flat", font=FONT_BTN,
                            cursor="hand2", pady=9)
        run_btn.pack(fill="x", pady=(0, 14))
        run_btn.bind("<Enter>", lambda e: run_btn.config(bg=ACCENT_HOV))
        run_btn.bind("<Leave>", lambda e: run_btn.config(bg=ACCENT))

        # ── Progress bar (always visible) ───────────────────
        pb_frame = tk.Frame(outer, bg=BG)
        pb_frame.pack(fill="x", pady=(0, 6))

        self.progress_bar = ttk.Progressbar(
            pb_frame, mode="indeterminate", length=500,
            style="Accent.Horizontal.TProgressbar")
        self.progress_bar.pack(fill="x")

        # ── Status label ────────────────────────────────────
        self.status_var = tk.StringVar(value="Ready — select files and press Run.")
        self.status_label = tk.Label(
            outer, textvariable=self.status_var,
            bg=BG, fg=FG_DIM,
            font=FONT_MONO, anchor="w")
        self.status_label.pack(anchor="w")

    # ── Event handlers ────────────────────────────────────────
    def _browse(self, var, filetypes):
        path = filedialog.askopenfilename(filetypes=filetypes)
        if path:
            var.set(path)

    def _start_progress(self):
        self.progress_bar.start(12)

    def _stop_progress(self):
        self.progress_bar.stop()
        self.progress_bar["value"] = 0

    def _set_status(self, msg, color=None):
        self.status_var.set(msg)
        self.status_label.config(foreground=color or FG_DIM)
        self.update_idletasks()

    def _on_run(self):
        paths = {
            "fastpph CSV" : self.fastpph_var.get(),
            "JOINT CSV"   : self.joint_var.get(),
            "BDF File"    : self.bdf_var.get(),
            "XLSM File"   : self.xlsm_var.get(),
        }
        for name, path in paths.items():
            if not path or not os.path.exists(path):
                messagebox.showerror("Missing File", f"Please select a valid {name}.")
                return

        if not self.run_metal.get() and not self.run_composite.get():
            messagebox.showerror("Nothing selected",
                                 "Select at least one of Metallic or Composite.")
            return

        thread = threading.Thread(target=self._run_process, args=(paths,), daemon=True)
        thread.start()
        self._start_progress()

    def _run_process(self, paths):
        try:
            self._set_status("Initialising…")

            run_metal     = self.run_metal.get()
            run_composite = self.run_composite.get()
            process_start = datetime.now()

            self._set_status("Reading BDF model…")
            bdf = BDF()
            bdf.read_bdf(paths["BDF File"], xref=True)

            self._set_status("Reading fastpph CSV…")
            df_fast = pd.read_csv(paths["fastpph CSV"], skiprows=2,
                                  sep=None, engine="python")
            df_fast.columns = df_fast.columns.str.strip()

            keep_cols = [
                "Component Name", "elem 1 id", "elem 1 Node id",
                "elem 2 id", "elem 2 Node id", "box dimension",
                "file Name", "LoadCase Name",
                "Fx", "Fy", "Fz",
                "Nx bypass", "Ny bypass", "Nxy bypass",
                "Mx total", "My total", "Mxy total",
                "DLS Ratio"
            ]
            df_fast = df_fast[keep_cols].copy()
            df_fast.rename(columns={"elem 1 id": "Element ID"}, inplace=True)

            self._set_status("Merging with JOINT CSV…")
            df_joint = pd.read_csv(paths["JOINT CSV"])
            df = pd.merge(df_fast, df_joint, on="Element ID", how="left")
            df["Pitch"] = (df["box dimension"].astype(float) /
                           df["Fastener Diameter"].astype(float))

            self._set_status("Extracting properties from BDF…")
            results = df["Component Name"].apply(
                lambda x: get_property_info(x, bdf))
            df[["MAT ID", "MAT", "n1", "n2", "n3", "n4"]] = pd.DataFrame(
                results.tolist(), index=df.index)

            df["prop_type"] = df["Component Name"].apply(
                lambda x: parse_comp_name(x)[0])
            df_metal     = df[df["prop_type"] == "PSHELL"].reset_index(drop=True)
            df_composite = df[df["prop_type"] == "PCOMP"].reset_index(drop=True)

            self._set_status("Opening XLSM workbook…")
            app = xw.App(visible=False)

            try:
                wb = xw.Book(paths["XLSM File"])
                df_metal_out     = None
                df_composite_out = None

                if run_metal:
                    self._set_status("Calculating metallic joints…")
                    df_metal_out = calculate_metal(df_metal, wb)

                if run_composite:
                    self._set_status("Calculating composite joints…")
                    df_composite_out = calculate_composite(df_composite, wb)

                wb.save()
                wb.close()
            finally:
                app.quit()

            process_end = datetime.now()
            elapsed     = (process_end - process_start).total_seconds()
            ts          = process_end.strftime("%Y%m%d_%H%M%S")
            script_dir  = os.path.dirname(os.path.abspath(__file__))

            files_used = {
                "fastpph CSV" : os.path.abspath(paths["fastpph CSV"]),
                "JOINT CSV"   : os.path.abspath(paths["JOINT CSV"]),
                "BDF File"    : os.path.abspath(paths["BDF File"]),
                "XLSM File"   : os.path.abspath(paths["XLSM File"]),
            }
            info_data = {
                "Item": list(files_used.keys()) + [
                    "Process Start", "Process End", "Elapsed (s)"],
                "Value": list(files_used.values()) + [
                    process_start.strftime("%Y-%m-%d %H:%M:%S"),
                    process_end.strftime("%Y-%m-%d %H:%M:%S"),
                    f"{elapsed:.2f}"
                ]
            }
            df_info = pd.DataFrame(info_data)

            if run_metal and df_metal_out is not None:
                self._set_status("Writing metallic results…")
                out_path = os.path.join(script_dir, f"Metal_Results_{ts}.xlsx")
                with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
                    df_metal_out.to_excel(writer, sheet_name="Results", index=False)
                    df_info.to_excel(writer, sheet_name="Info", index=False)

            if run_composite and df_composite_out is not None:
                self._set_status("Writing composite results…")
                out_path = os.path.join(script_dir, f"Composite_Results_{ts}.xlsx")
                with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
                    df_composite_out.to_excel(writer, sheet_name="Results", index=False)
                    df_info.to_excel(writer, sheet_name="Info", index=False)

            self._stop_progress()
            self._set_status(
                f"✔  Done in {elapsed:.1f}s  ·  {process_end.strftime('%H:%M:%S')}",
                color=SUCCESS)

        except Exception as e:
            self._stop_progress()
            self._set_status(f"✖  {str(e)}", color=ERROR)
            messagebox.showerror("Error", str(e))


# ============================================================
# ENTRY POINT
# ============================================================
if __name__ == "__main__":
    app = App()
    app.mainloop()
