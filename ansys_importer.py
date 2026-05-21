"""
mixins/ansys_importer.py  —  Unified ANSYS File Importer
=========================================================
Reads ALL ANSYS binary/ASCII formats supported by ansys-mapdl-reader
WITHOUT any ANSYS installation.

    pip install ansys-mapdl-reader

Supported formats
-----------------
  .rst / .rth   Structural / Thermal result files
  .full         Full stiffness-mass matrix (K, M sparse matrices)
  .emat         Element matrices data file
  .cdb / .dat   MAPDL ASCII block archive / Workbench input

Each extracted dataset opens as a normal tab in GridPilot.
All features (pivot, plot, heatmap, export, file watch) work on results.

Integration
-----------
  from mixins.ansys_importer import ANSYSImporterMixin
  class TableEditor(..., ANSYSImporterMixin, ...):
      ...
  # View menu:
  view_menu.add_command(label="⚙ Import ANSYS File…",
                        command=app.import_ansys_file)
"""

import os
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
import pandas as pd
import numpy as np
import ttkbootstrap as tb

# ── rst_reader: lightweight built-in RST reader (no mapdl required) ──────────
try:
    from rst_compat import read_rst_compat
    HAS_RST_READER = True
except ImportError:
    HAS_RST_READER = False

# ── ansys-mapdl-reader: still used for .full / .emat / .cdb / .dat ───────────
try:
    from ansys.mapdl.reader import read_binary as _mapdl_read_binary
    from ansys.mapdl.reader import archive as _archive_mod
    HAS_MAPDL_READER = True
except ImportError:
    HAS_MAPDL_READER = False

# HAS_READER: True when at least RST reading is available
HAS_READER = HAS_RST_READER


# ── DOF label maps ────────────────────────────────────────────────────────────
_DOF_STRUCT = ["UX","UY","UZ","ROTX","ROTY","ROTZ"]
_DOF_THERMAL= ["TEMP"]
_STRESS_COLS= ["SX","SY","SZ","SXY","SYZ","SXZ"]
_PSTRESS_COLS=["S1","S2","S3","SINT","SEQV"]
_STRAIN_EL  = ["EPELX","EPELY","EPELZ","EPELXY","EPELYZ","EPELXZ","EPEQV"]
_STRAIN_PL  = ["EPPLX","EPPLY","EPPLZ","EPPLXY","EPPLYZ","EPPLXZ","EPEQV"]
_STRAIN_TH  = ["EPTHX","EPTHY","EPTHZ","EPTHXY","EPTHYZ","EPTHXZ"]


# ═══════════════════════════════════════════════════════════════════════════════
# Per-format extractors  — each returns list of (tab_name_suffix, DataFrame)
# ═══════════════════════════════════════════════════════════════════════════════

def _safe(fn, *args, **kw):
    """Call fn, return None on any error (result type unavailable etc.)."""
    try:
        return fn(*args, **kw)
    except Exception:
        return None


# ── RST / RTH ─────────────────────────────────────────────────────────────────

def extract_rst(rst, selections: set, rnums: list) -> list:
    """Extract selected result types from an RST/RTH result object.

    Works with both:
      • RSTCompat  (our lightweight rst_reader wrapper)
      • ansys.mapdl.reader RST objects  (if mapdl-reader installed)
    """
    base  = []
    mesh  = rst.mesh

    # ── Mesh ──────────────────────────────────────────────────────────────────
    if "Node Coordinates" in selections:
        df = pd.DataFrame(mesh.nodes, columns=["X","Y","Z"])
        df.insert(0, "NodeID", mesh.nnum)
        # Flag CMS files where coordinates are unavailable
        if getattr(rst, "is_cms", False):
            df["Note"] = "CMS superelement — physical XYZ not stored in RST"
        base.append(("Nodes", df))

    if "Element Connectivity" in selections:
        rows = []
        for i, eid in enumerate(mesh.enum):
            conn = mesh.elem[i]
            rows.append([int(eid), int(mesh.etype[i])] + [int(n) for n in conn])
        max_n = max(len(mesh.elem[i]) for i in range(len(mesh.enum)))
        cols  = ["ElemID","ElemType"] + [f"N{j+1}" for j in range(max_n)]
        base.append(("Elements", pd.DataFrame(rows, columns=cols)))

    if "Node Components" in selections:
        nc  = mesh.node_components
        if nc:
            rows = [(k, len(v), " ".join(str(x) for x in v[:10])
                     + ("…" if len(v)>10 else ""))
                    for k, v in nc.items()]
            base.append(("NodeComps",
                          pd.DataFrame(rows, columns=["Component","Count","NodeIDs"])))

    if "Element Components" in selections:
        ec = mesh.element_components
        if ec:
            rows = [(k, len(v), " ".join(str(x) for x in v[:10])
                     + ("…" if len(v)>10 else ""))
                    for k, v in ec.items()]
            base.append(("ElemComps",
                          pd.DataFrame(rows, columns=["Component","Count","ElemIDs"])))

    if "Materials" in selections:
        rows = []
        for mat_id, props in rst.materials.items():
            for prop, val in props.items():
                rows.append({"MatID": int(mat_id), "Property": prop, "Value": val})
        if rows:
            base.append(("Materials", pd.DataFrame(rows)))

    if "Solution Summary" in selections:
        rows = []
        for rn in rnums:
            info = _safe(rst.solution_info, rn)
            if info:
                row = {"ResultIndex": rn}
                for k, v in info.items():
                    try:    row[k] = float(v) if hasattr(v,'__float__') else str(v)
                    except: row[k] = str(v)
                rows.append(row)
        if rows:
            base.append(("SolnSummary", pd.DataFrame(rows)))

    # ── Nodal results ─────────────────────────────────────────────────────────
    NODAL = [
        ("Nodal Displacement",      "nodal_displacement",
         _DOF_STRUCT),
        ("Nodal Stress",            "nodal_stress",
         _STRESS_COLS),
        ("Principal Nodal Stress",  "principal_nodal_stress",
         _PSTRESS_COLS),
        ("Nodal Elastic Strain",    "nodal_elastic_strain",
         _STRAIN_EL),
        ("Nodal Plastic Strain",    "nodal_plastic_strain",
         _STRAIN_PL),
        ("Nodal Thermal Strain",    "nodal_thermal_strain",
         _STRAIN_TH),
        ("Nodal Temperature",       "nodal_temperature",
         ["TEMP"]),
        ("Nodal Velocity",          "nodal_velocity",
         ["VX","VY","VZ"]),
        ("Nodal Acceleration",      "nodal_acceleration",
         ["AX","AY","AZ"]),
        ("Nodal Input Force",       "nodal_input_force",
         ["FX","FY","FZ","MX","MY","MZ"]),
        ("Nodal Static Forces",     "nodal_static_forces",
         ["FX","FY","FZ"]),
        ("Nodal Boundary Conditions","nodal_boundary_conditions",
         None),
    ]

    for label, method, col_names in NODAL:
        if label not in selections:
            continue
        fn = getattr(rst, method, None)
        if fn is None:
            continue
        all_rows = []
        for rn in rnums:
            result = _safe(fn, rn)
            if result is None:
                continue
            nnum, data = result
            data = np.atleast_2d(data) if data.ndim == 1 else data
            if data.ndim == 1:
                data = data.reshape(-1, 1)
            # Build column names
            if col_names:
                cols = col_names[:data.shape[1]]
                while len(cols) < data.shape[1]:
                    cols.append(f"V{len(cols)}")
            else:
                cols = [f"V{j}" for j in range(data.shape[1])]
            for i, nid in enumerate(nnum):
                row = {"ResultIndex": rn, "NodeID": int(nid)}
                for j, c in enumerate(cols):
                    row[c] = float(data[i, j]) if j < data.shape[1] else np.nan
                all_rows.append(row)
        if all_rows:
            suffix = f"_{rnums[0]}" if len(rnums)==1 else "_all"
            base.append((label.replace(" ","_") + suffix,
                          pd.DataFrame(all_rows)))

    # ── Element stress ─────────────────────────────────────────────────────────
    if "Element Stress" in selections:
        all_rows = []
        for rn in rnums:
            result = _safe(rst.element_stress, rn)
            if result is None:
                continue
            enum_r, edata = result
            for eid, vals in zip(enum_r, edata):
                if vals is None:
                    continue
                arr = np.atleast_1d(vals)
                row = {"ResultIndex": rn, "ElemID": int(eid)}
                for j, c in enumerate(_STRESS_COLS):
                    row[c] = float(arr[j]) if j < len(arr) else np.nan
                all_rows.append(row)
        if all_rows:
            suffix = f"_{rnums[0]}" if len(rnums)==1 else "_all"
            base.append(("Element_Stress"+suffix, pd.DataFrame(all_rows)))

    return base


# ── FULL ──────────────────────────────────────────────────────────────────────

def extract_full(fl, selections: set) -> list:
    results = []

    if "DOF Reference Table" in selections:
        # dof_ref[:,0]=node, dof_ref[:,1]=dof index
        df = pd.DataFrame(fl.dof_ref, columns=["NodeID","DOF_Index"])
        df["DOF_Name"] = df["DOF_Index"].map(
            {0:"UX",1:"UY",2:"UZ",3:"ROTX",4:"ROTY",5:"ROTZ",
             6:"TEMP",7:"PRES",8:"VOLT"})
        results.append(("DOF_Reference", df))

    if "Constrained DOFs" in selections:
        df = pd.DataFrame(fl.const, columns=["NodeID","DOF_Index"])
        df["DOF_Name"] = df["DOF_Index"].map(
            {0:"UX",1:"UY",2:"UZ",3:"ROTX",4:"ROTY",5:"ROTZ",
             6:"TEMP",7:"PRES",8:"VOLT"})
        results.append(("Constrained_DOFs", df))

    if "Load Vector" in selections:
        df = pd.DataFrame({"DOF_Index": range(len(fl.load_vector)),
                           "Load": fl.load_vector})
        results.append(("Load_Vector", df))

    if "Stiffness Matrix K (sparse→dense)" in selections:
        k_dense = fl.k.toarray()
        df = pd.DataFrame(k_dense)
        df.index   = [f"DOF_{i}" for i in range(k_dense.shape[0])]
        df.columns = [f"DOF_{i}" for i in range(k_dense.shape[1])]
        results.append(("Stiffness_K", df))

    if "Mass Matrix M (sparse→dense)" in selections:
        m_dense = fl.m.toarray()
        df = pd.DataFrame(m_dense)
        df.index   = [f"DOF_{i}" for i in range(m_dense.shape[0])]
        df.columns = [f"DOF_{i}" for i in range(m_dense.shape[1])]
        results.append(("Mass_M", df))

    if "K Sparse Triplets (row,col,val)" in selections:
        k = fl.k.tocoo()
        df = pd.DataFrame({"Row":k.row,"Col":k.col,"Value":k.data})
        results.append(("K_Sparse_Triplets", df))

    if "M Sparse Triplets (row,col,val)" in selections:
        m = fl.m.tocoo()
        df = pd.DataFrame({"Row":m.row,"Col":m.col,"Value":m.data})
        results.append(("M_Sparse_Triplets", df))

    return results


# ── EMAT ──────────────────────────────────────────────────────────────────────

def extract_emat(em, selections: set) -> list:
    results = []

    if "File Header / Summary" in selections:
        hdr = _safe(em.read_header) or {}
        rows = [(k, str(v)) for k, v in hdr.items()]
        rows += [
            ("n_elements",  em.n_elements),
            ("n_nodes",     em.n_nodes),
            ("n_dof",       em.n_dof),
        ]
        results.append(("EMAT_Header",
                         pd.DataFrame(rows, columns=["Property","Value"])))

    if "Node Equivalence Table" in selections:
        df = pd.DataFrame({
            "SequentialID": range(len(em.nnum)),
            "ANSYS_NodeID": em.nnum,
        })
        results.append(("Node_Equivalence", df))

    if "Element Equivalence Table" in selections:
        df = pd.DataFrame({
            "SequentialID": range(len(em.enum)),
            "ANSYS_ElemID": em.enum,
        })
        results.append(("Elem_Equivalence", df))

    if "Global Applied Force" in selections:
        force = em.global_applied_force
        cols  = [f"DOF_{j}" for j in range(force.shape[1])]
        df = pd.DataFrame(force, columns=cols)
        df.insert(0, "NodeID", em.nnum)
        results.append(("Global_Applied_Force", df))

    if "Element Matrices Index Table" in selections:
        tbl = _safe(em.element_matrices_index_table)
        if tbl is not None:
            df = pd.DataFrame(tbl)
            results.append(("Elem_Matrix_Index", df))

    if "Element Matrices (first 100 elements)" in selections:
        rows = []
        for idx in range(min(100, em.n_elements)):
            result = _safe(em.read_element, idx)
            if result is None:
                continue
            row = {"ElemIndex": idx,
                   "ANSYS_ElemID": int(em.enum[idx])}
            if hasattr(result, '__len__'):
                for mi, mat in enumerate(result):
                    if hasattr(mat, 'shape'):
                        row[f"Matrix{mi}_shape"] = str(mat.shape)
                        if mat.size > 0:
                            row[f"Matrix{mi}_norm"] = float(np.linalg.norm(mat))
            rows.append(row)
        if rows:
            results.append(("Element_Matrices_Summary",
                             pd.DataFrame(rows)))

    return results


# ── CDB / DAT ─────────────────────────────────────────────────────────────────

def extract_cdb(ar, selections: set) -> list:
    results = []

    if "Node Coordinates" in selections:
        df = pd.DataFrame(ar.nodes, columns=["X","Y","Z"])
        df.insert(0, "NodeID", ar.nnum)
        if ar.node_angles is not None and len(ar.node_angles):
            angles = np.atleast_2d(ar.node_angles)
            for j, col in enumerate(["THXY","THYZ","THZX"]):
                if j < angles.shape[1]:
                    df[col] = angles[:, j]
        results.append(("Nodes", df))

    if "Element Connectivity" in selections:
        rows = []
        for i, eid in enumerate(ar.enum):
            conn = ar.elem[i]
            rows.append(
                [int(eid), int(ar.etype[i]),
                 int(ar.material_type[i]),
                 int(ar.elem_real_constant[i]),
                 int(ar.section[i])]
                + [int(n) for n in conn])
        max_n = max(len(ar.elem[i]) for i in range(len(ar.enum)))
        cols  = (["ElemID","ElemType","MatID","RealConst","Section"]
                 + [f"N{j+1}" for j in range(max_n)])
        results.append(("Elements", pd.DataFrame(rows, columns=cols)))

    if "Element Type Keys" in selections:
        rows = [(int(ek[0]), int(ek[1])) for ek in ar.ekey]
        results.append(("ElemTypeKeys",
                         pd.DataFrame(rows, columns=["ET_ID","ElemType"])))

    if "Node Components" in selections:
        nc = ar.node_components
        if nc:
            rows = [(k, len(v),
                     " ".join(str(x) for x in v[:15])
                     + ("…" if len(v)>15 else ""))
                    for k, v in nc.items()]
            results.append(("NodeComps",
                             pd.DataFrame(rows, columns=["Component","Count","NodeIDs"])))

    if "Element Components" in selections:
        ec = ar.element_components
        if ec:
            rows = [(k, len(v),
                     " ".join(str(x) for x in v[:15])
                     + ("…" if len(v)>15 else ""))
                    for k, v in ec.items()]
            results.append(("ElemComps",
                             pd.DataFrame(rows, columns=["Component","Count","ElemIDs"])))

    if "Real Constants (RLBLOCK)" in selections:
        if ar.rlblock is not None and len(ar.rlblock):
            df = pd.DataFrame(ar.rlblock)
            df.insert(0, "RealConstID", ar.rlblock_num
                      if ar.rlblock_num is not None
                      else range(len(ar.rlblock)))
            results.append(("RealConstants", df))

    if "Parameters" in selections:
        try:
            params = ar.parameters
            if params:
                rows = [(k, str(v)) for k, v in params.items()]
                results.append(("Parameters",
                                 pd.DataFrame(rows, columns=["Name","Value"])))
        except AttributeError:
            pass

    if "Mesh Quality" in selections:
        qual = _safe(lambda: ar.quality)
        if qual is not None:
            df = pd.DataFrame({
                "ElemID":  ar.enum,
                "MinScaledJacobian": qual,
            })
            results.append(("MeshQuality", df))

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# Dialog
# ═══════════════════════════════════════════════════════════════════════════════

# Selection definitions per format
_SELECTIONS = {
    ".rst": {
        "MESH": [
            "Node Coordinates",
            "Element Connectivity",
            "Node Components",
            "Element Components",
            "Materials",
            "Solution Summary",
        ],
        "NODAL RESULTS": [
            "Nodal Displacement",
            "Nodal Stress",
            "Principal Nodal Stress",
            "Nodal Elastic Strain",
            "Nodal Plastic Strain",
            "Nodal Thermal Strain",
            "Nodal Temperature",
            "Nodal Velocity",
            "Nodal Acceleration",
            "Nodal Input Force",
            "Nodal Static Forces",
            "Nodal Boundary Conditions",
        ],
        "ELEMENT RESULTS": [
            "Element Stress",
        ],
    },
    ".rth": {
        "MESH": [
            "Node Coordinates",
            "Element Connectivity",
            "Node Components",
            "Element Components",
            "Materials",
            "Solution Summary",
        ],
        "NODAL RESULTS": [
            "Nodal Displacement",
            "Nodal Temperature",
            "Nodal Boundary Conditions",
        ],
        "ELEMENT RESULTS": [],
    },
    ".full": {
        "MATRICES": [
            "DOF Reference Table",
            "Constrained DOFs",
            "Load Vector",
            "K Sparse Triplets (row,col,val)",
            "M Sparse Triplets (row,col,val)",
            "Stiffness Matrix K (sparse→dense)",
            "Mass Matrix M (sparse→dense)",
        ],
    },
    ".emat": {
        "ELEMENT MATRICES": [
            "File Header / Summary",
            "Node Equivalence Table",
            "Element Equivalence Table",
            "Global Applied Force",
            "Element Matrices Index Table",
            "Element Matrices (first 100 elements)",
        ],
    },
    ".cdb": {
        "MESH": [
            "Node Coordinates",
            "Element Connectivity",
            "Element Type Keys",
            "Node Components",
            "Element Components",
            "Real Constants (RLBLOCK)",
            "Parameters",
            "Mesh Quality",
        ],
    },
    ".dat": {
        "MESH": [
            "Node Coordinates",
            "Element Connectivity",
            "Element Type Keys",
            "Node Components",
            "Element Components",
            "Real Constants (RLBLOCK)",
            "Parameters",
            "Mesh Quality",
        ],
    },
}

_DEFAULT_ON = {
    "Node Coordinates", "Solution Summary",
    "Nodal Displacement", "Nodal Stress",
    "DOF Reference Table", "Constrained DOFs", "Load Vector",
    "K Sparse Triplets (row,col,val)", "M Sparse Triplets (row,col,val)",
    "File Header / Summary", "Node Equivalence Table", "Global Applied Force",
}

_SECTION_COLORS = {
    "MESH":             ("#37474F", "white"),
    "NODAL RESULTS":    ("#1565C0", "white"),
    "ELEMENT RESULTS":  ("#4A148C", "white"),
    "MATRICES":         ("#1B5E20", "white"),
    "ELEMENT MATRICES": ("#E65100", "white"),
}


class _ANSYSImportDialog:

    def __init__(self, master, app):
        self.master = master
        self.app    = app
        self._obj   = None       # opened reader object
        self._path  = None
        self._ext   = None
        self._check_vars = {}

        self.win = tb.Toplevel(master)
        self.win.title("⚙  ANSYS File Importer")
        self.win.geometry("920x660")
        self.win.minsize(760, 520)
        self.win.grab_set()

        self._build()

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build(self):
        # Header
        hdr = tk.Frame(self.win, bg="#1565C0")
        hdr.pack(fill="x")
        tk.Label(hdr, text="  ⚙  ANSYS File Importer  —  No ANSYS licence required",
                 font=("Segoe UI", 10, "bold"),
                 bg="#1565C0", fg="white", pady=8).pack(side="left")
        if HAS_RST_READER and HAS_MAPDL_READER:
            lib = "✔ rst_reader + mapdl-reader ready"
        elif HAS_RST_READER:
            lib = "✔ rst_reader (RST/RTH)  |  ✘ mapdl-reader (FULL/EMAT/CDB)"
        elif HAS_MAPDL_READER:
            lib = "✘ rst_reader missing  |  ✔ mapdl-reader ready"
        else:
            lib = "✘ No reader available"
        tk.Label(hdr, text=lib, font=("Segoe UI", 8),
                 bg="#1565C0", fg="#90CAF9", pady=8).pack(side="right", padx=12)

        # File row
        fr = tk.Frame(self.win)
        fr.pack(fill="x", padx=12, pady=(10,4))
        tk.Label(fr, text="File:", font=("Segoe UI", 9, "bold")).pack(side="left")
        self._path_var = tk.StringVar()
        tk.Entry(fr, textvariable=self._path_var, font=("Segoe UI", 9),
                 width=52, relief="solid", bd=1).pack(side="left", padx=6)
        tk.Button(fr, text="Browse…", command=self._browse,
                  font=("Segoe UI", 8), relief="flat",
                  bg="#1565C0", fg="white", padx=8, pady=3, cursor="hand2"
                  ).pack(side="left")
        tk.Button(fr, text="  Probe  ", command=self._probe,
                  font=("Segoe UI", 8, "bold"), relief="flat",
                  bg="#2E7D32", fg="white", padx=8, pady=3, cursor="hand2"
                  ).pack(side="left", padx=4)

        # Load step row (only relevant for RST/RTH)
        ls_fr = tk.Frame(self.win)
        ls_fr.pack(fill="x", padx=12, pady=(0,4))
        tk.Label(ls_fr, text="Result index:",
                 font=("Segoe UI", 8)).pack(side="left")
        self._rnum_var = tk.StringVar(value="0")
        self._rnum_cb  = ttk.Combobox(ls_fr, textvariable=self._rnum_var,
                                       values=["0"], state="readonly", width=8,
                                       font=("Segoe UI", 8))
        self._rnum_cb.pack(side="left", padx=4)
        self._all_rnums_var = tk.BooleanVar(value=True)
        tk.Checkbutton(ls_fr, text="All result sets",
                       variable=self._all_rnums_var,
                       font=("Segoe UI", 8)
                       ).pack(side="left", padx=6)
        self._ls_frame = ls_fr   # hidden for non-RST formats

        # Main split
        pane = tk.PanedWindow(self.win, orient="horizontal",
                              sashwidth=5, sashrelief="raised")
        pane.pack(fill="both", expand=True, padx=12, pady=4)

        left  = tk.Frame(pane)
        right = tk.Frame(pane)
        pane.add(left,  minsize=320, stretch="always")
        pane.add(right, minsize=260, stretch="always")
        pane.update_idletasks()
        pane.sash_place(0, 430, 0)

        # Left: selection checklist
        tk.Label(left, text="Select data to import:",
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(0,4))

        self._checklist_outer = tk.Frame(left)
        self._checklist_outer.pack(fill="both", expand=True)
        self._build_checklist_placeholder()

        # Select All / None
        sa = tk.Frame(left)
        sa.pack(fill="x", pady=4)
        tk.Button(sa, text="Select All", font=("Segoe UI", 7),
                  relief="flat", cursor="hand2",
                  command=lambda: [v.set(True) for v in self._check_vars.values()]
                  ).pack(side="left", padx=2)
        tk.Button(sa, text="Clear All", font=("Segoe UI", 7),
                  relief="flat", cursor="hand2",
                  command=lambda: [v.set(False) for v in self._check_vars.values()]
                  ).pack(side="left")

        # Right: info panel
        tk.Label(right, text="File Information:",
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(0,4))
        self._info = tk.Text(right, font=("Consolas", 8), wrap="word",
                              state="disabled",
                              bg="#1e1e1e", fg="#d4d4d4",
                              relief="solid", bd=1)
        vsb = tk.Scrollbar(right, command=self._info.yview)
        self._info.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self._info.pack(fill="both", expand=True)

        # Bottom bar
        bot = tk.Frame(self.win, relief="groove", bd=1)
        bot.pack(fill="x", side="bottom", padx=12, pady=8)
        tk.Button(bot, text="⚙  Import Selected",
                  command=self._import,
                  bg="#1B5E20", fg="white",
                  font=("Segoe UI", 10, "bold"),
                  relief="flat", padx=16, pady=6, cursor="hand2"
                  ).pack(side="left", padx=4)
        tk.Button(bot, text="Cancel", command=self.win.destroy,
                  font=("Segoe UI", 9), relief="flat",
                  padx=12, pady=6, cursor="hand2"
                  ).pack(side="right", padx=4)
        self._status = tk.Label(bot, text="Open a file to begin.",
                                 font=("Segoe UI", 8), fg="#555", anchor="w")
        self._status.pack(side="left", padx=10)

    def _build_checklist_placeholder(self):
        for w in self._checklist_outer.winfo_children():
            w.destroy()
        tk.Label(self._checklist_outer,
                 text="Probe a file to see available data.",
                 font=("Segoe UI", 9), fg="#888"
                 ).pack(pady=20)

    def _build_checklist(self, ext):
        """Rebuild the checklist for the detected file extension."""
        for w in self._checklist_outer.winfo_children():
            w.destroy()
        self._check_vars.clear()

        sections = _SELECTIONS.get(ext, {})
        if not sections:
            tk.Label(self._checklist_outer,
                     text=f"No selection config for '{ext}'.",
                     fg="#888").pack(pady=10)
            return

        canvas = tk.Canvas(self._checklist_outer, highlightthickness=0)
        vsb    = tk.Scrollbar(self._checklist_outer, command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(canvas)
        canvas.create_window((0,0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        for sec_name, items in sections.items():
            if not items:
                continue
            bg, fg = _SECTION_COLORS.get(sec_name, ("#555","white"))
            tk.Label(inner, text=sec_name, font=("Segoe UI", 8, "bold"),
                     bg=bg, fg=fg, padx=6, pady=3, anchor="w"
                     ).pack(fill="x", pady=(6,1))
            for item in items:
                var = tk.BooleanVar(value=(item in _DEFAULT_ON))
                self._check_vars[item] = var
                tk.Checkbutton(inner, text=item, variable=var,
                               font=("Segoe UI", 8)
                               ).pack(anchor="w", padx=12, pady=1)

    # ── Browse / Probe ────────────────────────────────────────────────────────

    def _browse(self):
        path = filedialog.askopenfilename(
            title="Open ANSYS File",
            filetypes=[
                ("All ANSYS files",
                 "*.rst *.RST *.rth *.RTH *.full *.FULL "
                 "*.emat *.EMAT *.cdb *.CDB *.dat *.DAT"),
                ("RST/RTH Result",    "*.rst *.rth"),
                ("FULL Matrix",       "*.full"),
                ("EMAT Element Mat.", "*.emat"),
                ("CDB/DAT Archive",   "*.cdb *.dat"),
                ("All Files",         "*.*"),
            ],
            parent=self.win)
        if path:
            self._path_var.set(path)
            self._probe()

    def _probe(self):
        path = self._path_var.get().strip()
        if not path or not os.path.isfile(path):
            messagebox.showerror("Not found", f"File not found:\n{path}",
                                 parent=self.win)
            return

        ext = Path(path).suffix.lower()

        # Check library availability per file type
        if ext in (".rst", ".rth") and not HAS_RST_READER:
            messagebox.showerror(
                "Missing module",
                "rst_reader.py / rst_compat.py not found.\n"
                "Place them in the same folder as ansys_importer.py.",
                parent=self.win)
            return
        if ext in (".full", ".emat", ".cdb", ".dat") and not HAS_MAPDL_READER:
            messagebox.showerror(
                "Missing library",
                f"ansys-mapdl-reader is required for {ext.upper()} files.\n\n"
                "  pip install ansys-mapdl-reader\n\n"
                "No ANSYS licence needed.",
                parent=self.win)
            return

        self._set_status("Reading file…")
        self.win.update_idletasks()

        try:
            if ext in (".rst", ".rth"):
                obj = read_rst_compat(path)          # our lightweight reader
            elif ext in (".cdb", ".dat"):
                obj = _archive_mod.Archive(path, read_parameters=True)
            else:
                obj = _mapdl_read_binary(path)       # mapdl-reader for FULL/EMAT
        except Exception as exc:
            messagebox.showerror("Read error", str(exc), parent=self.win)
            self._set_status("Failed.")
            return

        self._obj  = obj
        self._path = path
        self._ext  = ext

        # Build checklist for this format
        self._build_checklist(ext)
        # Show/hide load step controls
        if ext in (".rst", ".rth"):
            self._ls_frame.pack(fill="x", padx=12, pady=(0,4))
            n = getattr(obj, "n_results", 1)
            self._rnum_cb.configure(values=[str(i) for i in range(n)])
            self._rnum_var.set("0")
        else:
            self._ls_frame.pack_forget()

        self._populate_info(obj, path, ext)
        self._set_status("Ready — select data and click Import.")

    def _populate_info(self, obj, path, ext):
        lines = []
        lines.append(f"Path:   {path}")
        lines.append(f"Size:   {os.path.getsize(path)/1e6:.2f} MB")
        lines.append(f"Format: {ext.upper()}")

        if ext in (".rst", ".rth"):
            lines += self._info_rst(obj)
        elif ext == ".full":
            lines += self._info_full(obj)
        elif ext == ".emat":
            lines += self._info_emat(obj)
        elif ext in (".cdb", ".dat"):
            lines += self._info_cdb(obj)

        self._set_info("\n".join(lines))

    def _info_rst(self, rst):
        lines = []
        # Reader type
        is_compat = hasattr(rst, "is_cms")
        lines.append(f"Reader: {'rst_reader (built-in)' if is_compat else 'ansys-mapdl-reader'}")
        if is_compat and rst.is_cms:
            lines.append("Type:   CMS Superelement")
        try: lines.append(f"ANSYS:  {rst.version}")
        except: pass
        try: lines.append(f"Results:{rst.n_results}")
        except: pass
        try:
            mesh = rst.mesh
            lines.append(f"Nodes:  {mesh.n_node:,}")
            lines.append(f"Elems:  {mesh.n_elem:,}")
        except: pass
        try:
            tv = rst.time_values
            shown = tv[:6].tolist()
            lines.append(f"Time:   {[round(t,4) for t in shown]}"
                         + (f" …[{len(tv)}]" if len(tv)>6 else ""))
        except: pass
        try:
            av = str(rst.available_results)
            lines.append(f"\nAvailable:\n{av}")
        except: pass
        try:
            nc = list(rst.mesh.node_components.keys())
            if nc: lines.append(f"\nNode comps: {nc}")
        except: pass
        try:
            ec = list(rst.mesh.element_components.keys())
            if ec: lines.append(f"Elem comps: {ec}")
        except: pass
        try:
            si = rst.solution_info(0)
            lines.append("\nSolution info (set 0):")
            for k,v in list(si.items())[:10]:
                lines.append(f"  {k}: {v}")
        except: pass
        return lines

    def _info_full(self, fl):
        return [
            f"Equations: {fl.neqn}",
            f"K shape:   {fl.k.shape}  nnz={fl.k.nnz:,}",
            f"M shape:   {fl.m.shape}  nnz={fl.m.nnz:,}",
            f"Load vec:  {fl.load_vector.shape[0]} DOFs",
            f"Const DOFs:{fl.const.shape[0]}",
            f"\nDOF ref (first 8):\n{fl.dof_ref[:8]}",
        ]

    def _info_emat(self, em):
        lines = [
            f"Elements: {em.n_elements:,}",
            f"Nodes:    {em.n_nodes:,}",
            f"DOF/node: {em.n_dof}",
        ]
        hdr = _safe(em.read_header)
        if hdr:
            lines.append("\nHeader:")
            for k,v in list(hdr.items())[:12]:
                lines.append(f"  {k}: {v}")
        return lines

    def _info_cdb(self, ar):
        lines = [
            f"Nodes:    {ar.n_node:,}",
            f"Elements: {ar.n_elem:,}",
        ]
        nc = ar.node_components
        if nc: lines.append(f"Node comps: {list(nc.keys())}")
        ec = ar.element_components
        if ec: lines.append(f"Elem comps: {list(ec.keys())}")
        lines.append(f"Elem types: {[int(e[1]) for e in ar.ekey]}")
        qual = _safe(lambda: ar.quality)
        if qual is not None:
            lines.append(f"Mesh quality min={qual.min():.3f} "
                         f"mean={qual.mean():.3f}")
        return lines

    # ── Import ────────────────────────────────────────────────────────────────

    def _import(self):
        if self._obj is None:
            messagebox.showinfo("No file", "Probe a file first.",
                                parent=self.win)
            return

        selected = {k for k, v in self._check_vars.items() if v.get()}
        if not selected:
            messagebox.showinfo("Nothing selected",
                                "Tick at least one item.",
                                parent=self.win)
            return

        ext    = self._ext
        obj    = self._obj
        base   = Path(self._path).stem
        loaded, errors = 0, []

        # Determine result indices for RST/RTH
        if ext in (".rst", ".rth"):
            n = getattr(obj, "n_results", 1)
            rnums = list(range(n)) if self._all_rnums_var.get() \
                    else [int(self._rnum_var.get())]
        else:
            rnums = [0]

        self._set_status("Extracting…")
        self.win.update_idletasks()

        try:
            if ext in (".rst", ".rth"):
                pairs = extract_rst(obj, selected, rnums)
            elif ext == ".full":
                pairs = extract_full(obj, selected)
            elif ext == ".emat":
                pairs = extract_emat(obj, selected)
            elif ext in (".cdb", ".dat"):
                pairs = extract_cdb(obj, selected)
            else:
                pairs = []
        except Exception as exc:
            messagebox.showerror("Extract error", str(exc), parent=self.win)
            self._set_status("Failed.")
            return

        for suffix, df in pairs:
            try:
                self._push_tab(f"{base}_{suffix}", df)
                loaded += 1
            except Exception as exc:
                errors.append(f"{suffix}: {exc}")

        msg = f"✓ {loaded} tab(s) imported"
        if errors:
            msg += f"  |  {len(errors)} error(s)"
            self._set_info("Errors:\n" + "\n".join(errors))
        self._set_status(msg)
        self.app.set_status(
            f"⚙ ANSYS import: {loaded} tab(s) from {os.path.basename(self._path)}")

        if loaded > 0 and not errors:
            self.win.after(500, self.win.destroy)

    def _push_tab(self, tab_name: str, df: pd.DataFrame):
        df = df.copy()
        df.columns = [str(c) for c in df.columns]
        base = tab_name; n = 1
        name = base
        while name in self.app.workbook_sheets:
            name = f"{base}_{n}"; n += 1
        widget = self.app._create_sheet_tab(
            name, df.copy(), file_path=None, sep=",")
        self.app._populate_sheet(widget, df)
        tabs = self.app.sheet_notebook.tabs()
        self.app.sheet_notebook.select(tabs[-1])
        self.app._on_sheet_change()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _set_info(self, text):
        self._info.configure(state="normal")
        self._info.delete("1.0", "end")
        self._info.insert("end", text)
        self._info.configure(state="disabled")

    def _set_status(self, msg):
        self._status.configure(text=msg)


# ═══════════════════════════════════════════════════════════════════════════════
# Mixin
# ═══════════════════════════════════════════════════════════════════════════════

class ANSYSImporterMixin:
    """Add to TableEditor. Replaces RSTImporterMixin."""

    def import_ansys_file(self, path=None):
        # RST reader is built-in; mapdl-reader only needed for FULL/EMAT/CDB
        if not HAS_RST_READER and not HAS_MAPDL_READER:
            messagebox.showerror(
                "No reader available",
                "RST reading: place rst_reader.py + rst_compat.py alongside "
                "ansys_importer.py.\n\n"
                "For .full / .emat / .cdb files:\n"
                "  pip install ansys-mapdl-reader",
                parent=self.root)
            return
        dlg = _ANSYSImportDialog(self.root, self)
        if path:
            dlg._path_var.set(path)
            dlg._probe()

    # Keep old name for backward compatibility
    def import_rst_file(self, path=None):
        self.import_ansys_file(path)
