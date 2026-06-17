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
JOINT_CSV    = r"C:\Users\User\Desktop\newwf\fastpph\JOINT-235501-20260617.CSV"   # your set output
BDF_FILE     = r"C:\Users\User\Desktop\newwf\fastpph\fast.bdf"
OUTPUT_CSV   = r"C:\Users\User\Desktop\newwf\fastpph\merged_output.csv"

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


def build_info_sheet_data(files_used, elapsed):
    return {
        "File": list(files_used.keys()),
        "Path": list(files_used.values()),
        "Process Time (s)": [""] * (len(files_used) - 1) + [f"{elapsed:.2f}"]
    }


# ============================================================
# CALCULATE METAL
# ============================================================
def calculate_metal(df_metal, wb):
    ws = wb.sheets[METAL_SHEET]

    # Clear old data from B9 downward
    ws.range("B9").expand("table").clear_contents()

    # Paste data starting at B9
    data_cols = DATA_COLS_METAL
    paste_data = df_metal[data_cols].values.tolist()
    ws.range("B9").value = paste_data

    # Trigger Excel calculation
    wb.app.calculate()

    # Find how many rows were pasted
    n_rows = len(paste_data)
    last_row = 9 + n_rows - 1

    # Read results back from B9:Z{last_row}
    # TODO: adjust end column from Z to wherever your results actually end
    # Read column names from row 8
    col_names = ws.range("B8:Z8").value
    col_names = [str(c).strip() if c is not None else f"col_{i}" 
                for i, c in enumerate(col_names)]

    # Read results back from B9:Z{last_row}
    results_range = ws.range(f"B9:Z{last_row}").value
    df_results = pd.DataFrame(results_range, columns=col_names)

    # Attach info columns on the left
    df_info = df_metal[INFO_COLS].reset_index(drop=True)
    df_out  = pd.concat([df_info, df_results], axis=1)

    return df_out


# ============================================================
# CALCULATE COMPOSITE
# ============================================================
def calculate_composite(df_composite, wb):
    ws = wb.sheets[COMPOSITE_SHEET]

    # Clear old data from B9 downward
    ws.range("B9").expand("table").clear_contents()

    # Paste data starting at B9
    data_cols = DATA_COLS_COMPOSITE
    paste_data = df_composite[data_cols].values.tolist()
    ws.range("B9").value = paste_data

    # Trigger Excel calculation
    wb.app.calculate()

    # Find how many rows were pasted
    n_rows = len(paste_data)
    last_row = 9 + n_rows - 1

    # Read results back from B9:Z{last_row}
    # TODO: adjust end column from Z to wherever your results actually end
    # Read column names from row 8
    col_names = ws.range("B8:Z8").value
    col_names = [str(c).strip() if c is not None else f"col_{i}" 
                for i, c in enumerate(col_names)]

    # Read results back from B9:Z{last_row}
    results_range = ws.range(f"B9:Z{last_row}").value
    df_results = pd.DataFrame(results_range, columns=col_names)

    # Attach info columns on the left
    df_info = df_composite[INFO_COLS].reset_index(drop=True)
    df_out  = pd.concat([df_info, df_results], axis=1)

    return df_out


# ============================================================
# MAIN
# ============================================================
def main():
    process_start = datetime.now()

    # --- 1. Load BDF ---
    print("Reading BDF...")
    bdf = BDF()
    bdf.read_bdf(BDF_FILE, xref=True)

    # --- 2. Read fastpph.csv ---
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

    # --- 3. Merge with JOINT.csv ---
    print("Merging with JOINT.csv...")
    df_joint = pd.read_csv(JOINT_CSV)
    df = pd.merge(df_fast, df_joint, on="Element ID", how="left")

    # --- 4. Pitch ---
    df["Pitch"] = df["box dimension"].astype(float) / df["Fastener Diameter"].astype(float)

    # --- 5. Property info from BDF ---
    print("Reading properties from BDF...")
    results = df["Component Name"].apply(lambda x: get_property_info(x, bdf))
    df[["MAT ID", "MAT", "n1", "n2", "n3", "n4"]] = pd.DataFrame(
        results.tolist(), index=df.index
    )

    # --- 6. Split into PSHELL and PCOMP ---
    df["prop_type"] = df["Component Name"].apply(lambda x: parse_comp_name(x)[0])
    df_metal     = df[df["prop_type"] == "PSHELL"].reset_index(drop=True)
    df_composite = df[df["prop_type"] == "PCOMP"].reset_index(drop=True)

    print(f"Metal rows     : {len(df_metal)}")
    print(f"Composite rows : {len(df_composite)}")

    # --- 7. Open xlsm and run calculations ---
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

    # --- 8. Build output filenames with timestamp ---
    process_end = datetime.now()
    elapsed     = (process_end - process_start).total_seconds()
    ts          = process_end.strftime("%Y%m%d_%H%M%S")

    metal_output     = os.path.join(OUTPUT_DIR, f"Metal_Results_{ts}.xlsx")
    composite_output = os.path.join(OUTPUT_DIR, f"Composite_Results_{ts}.xlsx")

    # --- 9. Files used info ---
    files_used = {
        "fastpph CSV" : os.path.abspath(FASTPPH_CSV),
        "JOINT CSV"   : os.path.abspath(JOINT_CSV),
        "BDF File"    : os.path.abspath(BDF_FILE),
        "XLSM File"   : os.path.abspath(XLSM_FILE),
    }

    info_data = {
        "Item"  : list(files_used.keys()) + ["Process Start", "Process End", "Elapsed (s)"],
        "Value" : list(files_used.values()) + [
            process_start.strftime("%Y-%m-%d %H:%M:%S"),
            process_end.strftime("%Y-%m-%d %H:%M:%S"),
            f"{elapsed:.2f}"
        ]
    }
    df_info = pd.DataFrame(info_data)

    # --- 10. Write output xlsx files ---
    print(f"Writing {metal_output}...")
    with pd.ExcelWriter(metal_output, engine="openpyxl") as writer:
        df_metal_out.to_excel(writer, sheet_name="Results", index=False)
        df_info.to_excel(writer, sheet_name="Info", index=False)

    print(f"Writing {composite_output}...")
    with pd.ExcelWriter(composite_output, engine="openpyxl") as writer:
        df_composite_out.to_excel(writer, sheet_name="Results", index=False)
        df_info.to_excel(writer, sheet_name="Info", index=False)

    print(f"Done! Elapsed: {elapsed:.2f}s")


import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import os
from datetime import datetime

# ============================================================
# IMPORT YOUR MAIN LOGIC
# ============================================================
# assumes all the functions above are in the same file
# if separate file: from your_script import main_logic

# ============================================================
# GUI
# ============================================================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Fastener Joint Calculator")
        self.resizable(False, False)
        self.configure(padx=20, pady=20)

        # --- File path variables ---
        self.fastpph_var = tk.StringVar()
        self.joint_var   = tk.StringVar()
        self.bdf_var     = tk.StringVar()
        self.xlsm_var    = tk.StringVar()

        # --- Checkboxes ---
        self.run_metal     = tk.BooleanVar(value=True)
        self.run_composite = tk.BooleanVar(value=True)

        self._build_ui()

    def _build_ui(self):

        # --- Title ---
        tk.Label(self, text="Fastener Joint Calculator", 
                 font=("Helvetica", 13, "bold")).grid(
            row=0, column=0, columnspan=3, pady=(0, 16), sticky="w")

        # --- File inputs ---
        fields = [
            ("fastpph CSV",  self.fastpph_var, [("CSV Files", "*.csv")]),
            ("JOINT CSV",    self.joint_var,   [("CSV Files", "*.csv")]),
            ("BDF File",     self.bdf_var,     [("BDF Files", "*.bdf"), ("All Files", "*.*")]),
            ("XLSM File",    self.xlsm_var,    [("Excel Macro Files", "*.xlsm")]),
        ]

        for i, (label, var, ftypes) in enumerate(fields, start=1):
            tk.Label(self, text=label, anchor="w", width=14).grid(
                row=i, column=0, sticky="w", pady=4)
            tk.Entry(self, textvariable=var, width=48).grid(
                row=i, column=1, padx=8, pady=4)
            tk.Button(self, text="Browse",
                      command=lambda v=var, f=ftypes: self._browse(v, f)).grid(
                row=i, column=2, pady=4)

        # --- Checkboxes ---
        sep = ttk.Separator(self, orient="horizontal")
        sep.grid(row=5, column=0, columnspan=3, sticky="ew", pady=12)

        tk.Label(self, text="Run for:").grid(
            row=6, column=0, sticky="w")

        tk.Checkbutton(self, text="Metallic",   variable=self.run_metal).grid(
            row=6, column=1, sticky="w", padx=(0, 0))
        tk.Checkbutton(self, text="Composite", variable=self.run_composite).grid(
            row=6, column=1, sticky="w", padx=(90, 0))

        # --- Run button ---
        sep2 = ttk.Separator(self, orient="horizontal")
        sep2.grid(row=7, column=0, columnspan=3, sticky="ew", pady=12)

        tk.Button(self, text="Run", width=20,
                  command=self._on_run,
                  font=("Helvetica", 10, "bold")).grid(
            row=8, column=0, columnspan=3, pady=(0, 12))

        # --- Status bar ---
        self.status_var = tk.StringVar(value="Ready.")
        tk.Label(self, textvariable=self.status_var,
                 anchor="w", foreground="gray",
                 font=("Helvetica", 9)).grid(
            row=9, column=0, columnspan=3, sticky="ew")

    def _browse(self, var, filetypes):
        path = filedialog.askopenfilename(filetypes=filetypes)
        if path:
            var.set(path)

    def _set_status(self, msg, color="gray"):
        self.status_var.set(msg)
        self.nametowidget(".").update_idletasks()

    def _on_run(self):
        # --- Validate inputs ---
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
                                 "Please select at least one of Metallic or Composite.")
            return

        # --- Run in thread so GUI doesn't freeze ---
        thread = threading.Thread(target=self._run_process, args=(paths,), daemon=True)
        thread.start()

    def _run_process(self, paths):
        try:
            self._set_status("Running... please wait.")

            run_metal     = self.run_metal.get()
            run_composite = self.run_composite.get()

            process_start = datetime.now()

            # --- Load BDF ---
            self._set_status("Reading BDF...")
            from pyNastran.bdf.bdf import BDF
            bdf = BDF()
            bdf.read_bdf(paths["BDF File"], xref=True)

            # --- Read fastpph ---
            self._set_status("Reading fastpph CSV...")
            import pandas as pd
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

            # --- Merge with JOINT ---
            self._set_status("Merging with JOINT CSV...")
            df_joint = pd.read_csv(paths["JOINT CSV"])
            df = pd.merge(df_fast, df_joint, on="Element ID", how="left")
            df["Pitch"] = (df["box dimension"].astype(float) / 
                           df["Fastener Diameter"].astype(float))

            # --- BDF properties ---
            self._set_status("Reading properties from BDF...")
            results = df["Component Name"].apply(
                lambda x: get_property_info(x, bdf))
            df[["MAT ID", "MAT", "n1", "n2", "n3", "n4"]] = pd.DataFrame(
                results.tolist(), index=df.index)

            # --- Split ---
            df["prop_type"] = df["Component Name"].apply(
                lambda x: parse_comp_name(x)[0])
            df_metal     = df[df["prop_type"] == "PSHELL"].reset_index(drop=True)
            df_composite = df[df["prop_type"] == "PCOMP"].reset_index(drop=True)

            # --- Open xlsm ---
            self._set_status("Opening XLSM...")
            import xlwings as xw
            app = xw.App(visible=False)

            try:
                wb = xw.Book(paths["XLSM File"])

                df_metal_out     = None
                df_composite_out = None

                if run_metal:
                    self._set_status("Calculating metallic...")
                    df_metal_out = calculate_metal(df_metal, wb)

                if run_composite:
                    self._set_status("Calculating composite...")
                    df_composite_out = calculate_composite(df_composite, wb)

                wb.save()
                wb.close()
            finally:
                app.quit()

            # --- Write outputs ---
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
                self._set_status("Writing Metal output...")
                out_path = os.path.join(script_dir, f"Metal_Results_{ts}.xlsx")
                with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
                    df_metal_out.to_excel(writer, sheet_name="Results", index=False)
                    df_info.to_excel(writer, sheet_name="Info", index=False)

            if run_composite and df_composite_out is not None:
                self._set_status("Writing Composite output...")
                out_path = os.path.join(script_dir, f"Composite_Results_{ts}.xlsx")
                with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
                    df_composite_out.to_excel(writer, sheet_name="Results", index=False)
                    df_info.to_excel(writer, sheet_name="Info", index=False)

            self._set_status(
                f"Done! Completed in {elapsed:.2f}s  —  {ts}", color="green")

        except Exception as e:
            self._set_status(f"Error: {str(e)}", color="red")
            messagebox.showerror("Error", str(e))


# ============================================================
# ENTRY POINT
# ============================================================
if __name__ == "__main__":
    app = App()
    app.mainloop()
