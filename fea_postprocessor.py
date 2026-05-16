"""
FEA Post-Processor — Professional 3D Finite Element Analysis Viewer
====================================================================
Integrates the ANSYS file importer so that real .rst / .rth / .full /
.emat / .cdb / .dat files can be loaded directly into the 3D viewport
and data grid — no ANSYS installation or licence required.

Dependencies (core):
    pip install pyvista vtk tksheet pandas numpy

Dependencies (ANSYS import):
    pip install ansys-mapdl-reader

Usage:
    python fea_postprocessor.py

Keyboard shortcuts:
    Ctrl+O  →  Open ANSYS file
    Ctrl+R  →  Reset camera
    Ctrl+E  →  Toggle mesh edges
    F5      →  Auto-scale colorbar
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from pathlib import Path
import os
import numpy as np
import pandas as pd

# ── Optional VTK / PyVista ────────────────────────────────────────────────────
try:
    import pyvista as pv
    import vtk
    from vtk.tk.vtkTkRenderWindowInteractor import vtkTkRenderWindowInteractor
    VTK_AVAILABLE = True
except ImportError:
    VTK_AVAILABLE = False

# ── Optional tksheet ──────────────────────────────────────────────────────────
try:
    import tksheet
    TKSHEET_AVAILABLE = True
except ImportError:
    TKSHEET_AVAILABLE = False

# ── Optional ANSYS reader ─────────────────────────────────────────────────────
try:
    from ansys.mapdl.reader import read_binary
    from ansys.mapdl.reader import archive as _archive_mod
    HAS_ANSYS_READER = True
except ImportError:
    HAS_ANSYS_READER = False


# ═══════════════════════════════════════════════════════════════════════════════
# § 1  DOF / RESULT COLUMN MAPS
# ═══════════════════════════════════════════════════════════════════════════════

_DOF_STRUCT   = ['UX', 'UY', 'UZ', 'ROTX', 'ROTY', 'ROTZ']
_STRESS_COLS  = ['SX', 'SY', 'SZ', 'SXY', 'SYZ', 'SXZ']
_PSTRESS_COLS = ['S1', 'S2', 'S3', 'SINT', 'SEQV']
_STRAIN_EL    = ['EPELX', 'EPELY', 'EPELZ', 'EPELXY', 'EPELYZ', 'EPELXZ',
                 'EPEQV']
_STRAIN_PL    = ['EPPLX', 'EPPLY', 'EPPLZ', 'EPPLXY', 'EPPLYZ', 'EPPLXZ',
                 'EPEQV']
_STRAIN_TH    = ['EPTHX', 'EPTHY', 'EPTHZ', 'EPTHXY', 'EPTHYZ', 'EPTHXZ']

# Ordered list of nodal result extractors used by extract_rst()
_NODAL_EXTRACTORS = [
    ('Nodal Displacement',        'nodal_displacement',        _DOF_STRUCT),
    ('Nodal Stress',              'nodal_stress',               _STRESS_COLS),
    ('Principal Nodal Stress',    'principal_nodal_stress',     _PSTRESS_COLS),
    ('Nodal Elastic Strain',      'nodal_elastic_strain',       _STRAIN_EL),
    ('Nodal Plastic Strain',      'nodal_plastic_strain',       _STRAIN_PL),
    ('Nodal Thermal Strain',      'nodal_thermal_strain',       _STRAIN_TH),
    ('Nodal Temperature',         'nodal_temperature',          ['TEMP']),
    ('Nodal Velocity',            'nodal_velocity',             ['VX','VY','VZ']),
    ('Nodal Acceleration',        'nodal_acceleration',         ['AX','AY','AZ']),
    ('Nodal Input Force',         'nodal_input_force',
     ['FX','FY','FZ','MX','MY','MZ']),
    ('Nodal Static Forces',       'nodal_static_forces',        ['FX','FY','FZ']),
    ('Nodal Boundary Conditions', 'nodal_boundary_conditions',  None),
]

# Default-checked items in the import dialog
_ANSYS_DEFAULT_ON = {
    'Node Coordinates', 'Solution Summary',
    'Nodal Displacement', 'Nodal Stress', 'Principal Nodal Stress',
    'Nodal Temperature',
    'DOF Reference Table', 'Constrained DOFs', 'Load Vector',
    'K Sparse Triplets (row,col,val)', 'M Sparse Triplets (row,col,val)',
    'File Header / Summary', 'Node Equivalence Table', 'Global Applied Force',
}

# Checklist sections per file extension
_ANSYS_SELECTIONS: dict[str, dict[str, list]] = {
    '.rst': {
        'MESH': [
            'Node Coordinates', 'Element Connectivity',
            'Node Components', 'Element Components',
            'Materials', 'Solution Summary',
        ],
        'NODAL RESULTS': [
            'Nodal Displacement', 'Nodal Stress', 'Principal Nodal Stress',
            'Nodal Elastic Strain', 'Nodal Plastic Strain',
            'Nodal Thermal Strain', 'Nodal Temperature',
            'Nodal Velocity', 'Nodal Acceleration',
            'Nodal Input Force', 'Nodal Static Forces',
            'Nodal Boundary Conditions',
        ],
        'ELEMENT RESULTS': ['Element Stress'],
    },
    '.rth': {
        'MESH': [
            'Node Coordinates', 'Element Connectivity',
            'Node Components', 'Element Components',
            'Materials', 'Solution Summary',
        ],
        'NODAL RESULTS': [
            'Nodal Displacement', 'Nodal Temperature',
            'Nodal Boundary Conditions',
        ],
        'ELEMENT RESULTS': [],
    },
    '.full': {
        'MATRICES': [
            'DOF Reference Table', 'Constrained DOFs', 'Load Vector',
            'K Sparse Triplets (row,col,val)',
            'M Sparse Triplets (row,col,val)',
            'Stiffness Matrix K (sparse\u2192dense)',
            'Mass Matrix M (sparse\u2192dense)',
        ],
    },
    '.emat': {
        'ELEMENT MATRICES': [
            'File Header / Summary', 'Node Equivalence Table',
            'Element Equivalence Table', 'Global Applied Force',
            'Element Matrices Index Table',
            'Element Matrices (first 100 elements)',
        ],
    },
    '.cdb': {
        'MESH': [
            'Node Coordinates', 'Element Connectivity', 'Element Type Keys',
            'Node Components', 'Element Components',
            'Real Constants (RLBLOCK)', 'Parameters', 'Mesh Quality',
        ],
    },
    '.dat': {
        'MESH': [
            'Node Coordinates', 'Element Connectivity', 'Element Type Keys',
            'Node Components', 'Element Components',
            'Real Constants (RLBLOCK)', 'Parameters', 'Mesh Quality',
        ],
    },
}

_SECTION_COLORS: dict[str, tuple] = {
    'MESH':             ('#37474F', 'white'),
    'NODAL RESULTS':    ('#1565C0', 'white'),
    'ELEMENT RESULTS':  ('#4A148C', 'white'),
    'MATRICES':         ('#1B5E20', 'white'),
    'ELEMENT MATRICES': ('#E65100', 'white'),
}

# VTK cell-type lookup by number of nodes per element
_VTK_CELL_TYPE_MAP: dict[int, int] = {}  # populated after vtk import


def _init_vtk_cell_map() -> None:
    """Populate _VTK_CELL_TYPE_MAP once VTK is confirmed available."""
    global _VTK_CELL_TYPE_MAP
    if VTK_AVAILABLE and not _VTK_CELL_TYPE_MAP:
        _VTK_CELL_TYPE_MAP = {
            4:  vtk.VTK_TETRA,
            6:  vtk.VTK_WEDGE,
            8:  vtk.VTK_HEXAHEDRON,
            10: vtk.VTK_QUADRATIC_TETRA,
            20: vtk.VTK_QUADRATIC_HEXAHEDRON,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# § 2  ANSYS EXTRACTION HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _safe(fn, *args, **kwargs):
    """Call fn(*args, **kwargs); return None silently on any exception."""
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


def extract_rst(rst, selections: set, rnums: list) -> list:
    """
    Extract selected result types from an RST/RTH reader object.

    Parameters
    ----------
    rst        : ansys.mapdl.reader.rst.ResultFile
    selections : set[str]   — checked item names from the dialog
    rnums      : list[int]  — result-set indices to extract

    Returns
    -------
    list of (tab_name_suffix: str, df: pd.DataFrame)
    """
    base = []
    mesh = rst.mesh

    # ── Mesh geometry ─────────────────────────────────────────────────────────
    if 'Node Coordinates' in selections:
        df = pd.DataFrame(mesh.nodes, columns=['X', 'Y', 'Z'])
        df.insert(0, 'NodeID', mesh.nnum)
        base.append(('Nodes', df))

    if 'Element Connectivity' in selections:
        rows = []
        for i, eid in enumerate(mesh.enum):
            conn = mesh.elem[i]
            rows.append([int(eid), int(mesh.etype[i])]
                        + [int(n) for n in conn])
        max_n = max(len(mesh.elem[i]) for i in range(len(mesh.enum)))
        cols  = ['ElemID', 'ElemType'] + [f'N{j+1}' for j in range(max_n)]
        base.append(('Elements', pd.DataFrame(rows, columns=cols)))

    if 'Node Components' in selections:
        nc = mesh.node_components
        if nc:
            rows = [(k, len(v),
                     ' '.join(str(x) for x in v[:10])
                     + ('…' if len(v) > 10 else ''))
                    for k, v in nc.items()]
            base.append(('NodeComps', pd.DataFrame(
                rows, columns=['Component', 'Count', 'NodeIDs'])))

    if 'Element Components' in selections:
        ec = mesh.element_components
        if ec:
            rows = [(k, len(v),
                     ' '.join(str(x) for x in v[:10])
                     + ('…' if len(v) > 10 else ''))
                    for k, v in ec.items()]
            base.append(('ElemComps', pd.DataFrame(
                rows, columns=['Component', 'Count', 'ElemIDs'])))

    if 'Materials' in selections:
        rows = []
        for mat_id, props in rst.materials.items():
            for prop, val in props.items():
                rows.append({'MatID': int(mat_id),
                             'Property': prop, 'Value': val})
        if rows:
            base.append(('Materials', pd.DataFrame(rows)))

    if 'Solution Summary' in selections:
        rows = []
        for rn in rnums:
            info = _safe(rst.solution_info, rn)
            if info:
                row = {'ResultIndex': rn}
                for k, v in info.items():
                    try:
                        row[k] = (float(v)
                                  if hasattr(v, '__float__') else str(v))
                    except Exception:
                        row[k] = str(v)
                rows.append(row)
        if rows:
            base.append(('SolnSummary', pd.DataFrame(rows)))

    # ── Nodal result arrays ───────────────────────────────────────────────────
    for label, method, col_names in _NODAL_EXTRACTORS:
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
            if col_names:
                cols = list(col_names[:data.shape[1]])
                while len(cols) < data.shape[1]:
                    cols.append(f'V{len(cols)}')
            else:
                cols = [f'V{j}' for j in range(data.shape[1])]
            for i, nid in enumerate(nnum):
                row = {'ResultIndex': rn, 'NodeID': int(nid)}
                for j, c in enumerate(cols):
                    row[c] = (float(data[i, j])
                              if j < data.shape[1] else np.nan)
                all_rows.append(row)
        if all_rows:
            suffix = f'_{rnums[0]}' if len(rnums) == 1 else '_all'
            base.append((label.replace(' ', '_') + suffix,
                         pd.DataFrame(all_rows)))

    # ── Element stress ────────────────────────────────────────────────────────
    if 'Element Stress' in selections:
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
                row = {'ResultIndex': rn, 'ElemID': int(eid)}
                for j, c in enumerate(_STRESS_COLS):
                    row[c] = float(arr[j]) if j < len(arr) else np.nan
                all_rows.append(row)
        if all_rows:
            suffix = f'_{rnums[0]}' if len(rnums) == 1 else '_all'
            base.append(('Element_Stress' + suffix, pd.DataFrame(all_rows)))

    return base


def extract_full(fl, selections: set) -> list:
    """Extract DOF reference, load vector, and K/M matrices from a FULL file."""
    results = []
    _dof_map = {0: 'UX', 1: 'UY', 2: 'UZ', 3: 'ROTX', 4: 'ROTY', 5: 'ROTZ',
                6: 'TEMP', 7: 'PRES', 8: 'VOLT'}

    if 'DOF Reference Table' in selections:
        df = pd.DataFrame(fl.dof_ref, columns=['NodeID', 'DOF_Index'])
        df['DOF_Name'] = df['DOF_Index'].map(_dof_map)
        results.append(('DOF_Reference', df))

    if 'Constrained DOFs' in selections:
        df = pd.DataFrame(fl.const, columns=['NodeID', 'DOF_Index'])
        df['DOF_Name'] = df['DOF_Index'].map(_dof_map)
        results.append(('Constrained_DOFs', df))

    if 'Load Vector' in selections:
        results.append(('Load_Vector', pd.DataFrame(
            {'DOF_Index': range(len(fl.load_vector)),
             'Load': fl.load_vector})))

    if 'Stiffness Matrix K (sparse\u2192dense)' in selections:
        k = fl.k.toarray()
        results.append(('Stiffness_K', pd.DataFrame(
            k,
            index=[f'DOF_{i}' for i in range(k.shape[0])],
            columns=[f'DOF_{i}' for i in range(k.shape[1])])))

    if 'Mass Matrix M (sparse\u2192dense)' in selections:
        m = fl.m.toarray()
        results.append(('Mass_M', pd.DataFrame(
            m,
            index=[f'DOF_{i}' for i in range(m.shape[0])],
            columns=[f'DOF_{i}' for i in range(m.shape[1])])))

    if 'K Sparse Triplets (row,col,val)' in selections:
        k = fl.k.tocoo()
        results.append(('K_Sparse_Triplets', pd.DataFrame(
            {'Row': k.row, 'Col': k.col, 'Value': k.data})))

    if 'M Sparse Triplets (row,col,val)' in selections:
        m = fl.m.tocoo()
        results.append(('M_Sparse_Triplets', pd.DataFrame(
            {'Row': m.row, 'Col': m.col, 'Value': m.data})))

    return results


def extract_emat(em, selections: set) -> list:
    """Extract element-matrix data from an EMAT file."""
    results = []

    if 'File Header / Summary' in selections:
        hdr  = _safe(em.read_header) or {}
        rows = ([(k, str(v)) for k, v in hdr.items()]
                + [('n_elements', em.n_elements),
                   ('n_nodes',    em.n_nodes),
                   ('n_dof',      em.n_dof)])
        results.append(('EMAT_Header', pd.DataFrame(
            rows, columns=['Property', 'Value'])))

    if 'Node Equivalence Table' in selections:
        results.append(('Node_Equivalence', pd.DataFrame(
            {'SequentialID': range(len(em.nnum)),
             'ANSYS_NodeID': em.nnum})))

    if 'Element Equivalence Table' in selections:
        results.append(('Elem_Equivalence', pd.DataFrame(
            {'SequentialID': range(len(em.enum)),
             'ANSYS_ElemID': em.enum})))

    if 'Global Applied Force' in selections:
        force = em.global_applied_force
        df    = pd.DataFrame(force,
                             columns=[f'DOF_{j}' for j in range(force.shape[1])])
        df.insert(0, 'NodeID', em.nnum)
        results.append(('Global_Applied_Force', df))

    if 'Element Matrices Index Table' in selections:
        tbl = _safe(em.element_matrices_index_table)
        if tbl is not None:
            results.append(('Elem_Matrix_Index', pd.DataFrame(tbl)))

    if 'Element Matrices (first 100 elements)' in selections:
        rows = []
        for idx in range(min(100, em.n_elements)):
            res = _safe(em.read_element, idx)
            if res is None:
                continue
            row = {'ElemIndex': idx, 'ANSYS_ElemID': int(em.enum[idx])}
            if hasattr(res, '__len__'):
                for mi, mat in enumerate(res):
                    if hasattr(mat, 'shape'):
                        row[f'Matrix{mi}_shape'] = str(mat.shape)
                        if mat.size > 0:
                            row[f'Matrix{mi}_norm'] = float(
                                np.linalg.norm(mat))
            rows.append(row)
        if rows:
            results.append(('Element_Matrices_Summary', pd.DataFrame(rows)))

    return results


def extract_cdb(ar, selections: set) -> list:
    """Extract mesh data from a CDB / DAT MAPDL archive."""
    results = []

    if 'Node Coordinates' in selections:
        df = pd.DataFrame(ar.nodes, columns=['X', 'Y', 'Z'])
        df.insert(0, 'NodeID', ar.nnum)
        if ar.node_angles is not None and len(ar.node_angles):
            angles = np.atleast_2d(ar.node_angles)
            for j, col in enumerate(['THXY', 'THYZ', 'THZX']):
                if j < angles.shape[1]:
                    df[col] = angles[:, j]
        results.append(('Nodes', df))

    if 'Element Connectivity' in selections:
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
        cols  = (['ElemID', 'ElemType', 'MatID', 'RealConst', 'Section']
                 + [f'N{j+1}' for j in range(max_n)])
        results.append(('Elements', pd.DataFrame(rows, columns=cols)))

    if 'Element Type Keys' in selections:
        rows = [(int(ek[0]), int(ek[1])) for ek in ar.ekey]
        results.append(('ElemTypeKeys', pd.DataFrame(
            rows, columns=['ET_ID', 'ElemType'])))

    if 'Node Components' in selections:
        nc = ar.node_components
        if nc:
            rows = [(k, len(v),
                     ' '.join(str(x) for x in v[:15])
                     + ('…' if len(v) > 15 else ''))
                    for k, v in nc.items()]
            results.append(('NodeComps', pd.DataFrame(
                rows, columns=['Component', 'Count', 'NodeIDs'])))

    if 'Element Components' in selections:
        ec = ar.element_components
        if ec:
            rows = [(k, len(v),
                     ' '.join(str(x) for x in v[:15])
                     + ('…' if len(v) > 15 else ''))
                    for k, v in ec.items()]
            results.append(('ElemComps', pd.DataFrame(
                rows, columns=['Component', 'Count', 'ElemIDs'])))

    if 'Real Constants (RLBLOCK)' in selections:
        if ar.rlblock is not None and len(ar.rlblock):
            df   = pd.DataFrame(ar.rlblock)
            rnum = (ar.rlblock_num if ar.rlblock_num is not None
                    else range(len(ar.rlblock)))
            df.insert(0, 'RealConstID', rnum)
            results.append(('RealConstants', df))

    if 'Parameters' in selections:
        try:
            params = ar.parameters
            if params:
                rows = [(k, str(v)) for k, v in params.items()]
                results.append(('Parameters', pd.DataFrame(
                    rows, columns=['Name', 'Value'])))
        except AttributeError:
            pass

    if 'Mesh Quality' in selections:
        qual = _safe(lambda: ar.quality)
        if qual is not None:
            results.append(('MeshQuality', pd.DataFrame(
                {'ElemID': ar.enum, 'MinScaledJacobian': qual})))

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# § 3  RST → VIEWER DATA BRIDGE
# ═══════════════════════════════════════════════════════════════════════════════

def rst_to_viewer_dataframes(
    rst,
    result_index: int = 0,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, list]:
    """
    Convert an open RST / CDB reader object into the three DataFrames
    the 3D viewer needs, plus the list of renderable scalar names.

    Extracts (in order of priority):
        Von_Mises_Stress  ← SEQV from principal_nodal_stress
        SX … SXZ          ← nodal_stress components
        Temperature        ← nodal_temperature
        Disp_Magnitude     ← |UX, UY, UZ|  from nodal_displacement
        UX, UY, UZ         ← individual displacement components

    Any result type absent in the file is silently skipped.

    Parameters
    ----------
    rst          : ansys.mapdl.reader result or archive object
    result_index : load-step index to extract (default 0)

    Returns
    -------
    nodes_df, elements_df, results_df, scalar_cols
    """
    mesh = rst.mesh

    # ── Nodes ─────────────────────────────────────────────────────────────────
    nodes_df = pd.DataFrame(mesh.nodes, columns=['X', 'Y', 'Z'])
    nodes_df.insert(0, 'Node_ID', mesh.nnum)

    # ── Elements ──────────────────────────────────────────────────────────────
    max_conn = max(
        (len(mesh.elem[i]) for i in range(len(mesh.enum))), default=8)
    rows = []
    for i, eid in enumerate(mesh.enum):
        conn = list(mesh.elem[i])
        conn += [0] * (max_conn - len(conn))   # pad to uniform width
        rows.append([int(eid)] + conn)
    elem_cols   = ['Element_ID'] + [f'N{j+1}' for j in range(max_conn)]
    elements_df = pd.DataFrame(rows, columns=elem_cols)

    # ── Results ───────────────────────────────────────────────────────────────
    results_df  = pd.DataFrame({'Node_ID': mesh.nnum})
    scalar_cols: list[str] = []

    def _merge(series: pd.Series) -> None:
        """Left-merge a Series (indexed by ANSYS node number) into results_df."""
        nonlocal results_df
        tmp = (series.reset_index()
               .rename(columns={'index': 'Node_ID', 0: series.name}))
        results_df = results_df.merge(tmp, on='Node_ID', how='left')
        scalar_cols.append(series.name)

    # Von Mises stress (SEQV = column index 4 in principal_nodal_stress)
    pns = _safe(rst.principal_nodal_stress, result_index)
    if pns is not None:
        nnum, data = pns
        data = np.atleast_2d(data)
        idx  = min(4, data.shape[1] - 1)
        _merge(pd.Series(data[:, idx], index=nnum, name='Von_Mises_Stress'))

    # Individual stress components
    ns = _safe(rst.nodal_stress, result_index)
    if ns is not None:
        nnum, data = ns
        data = np.atleast_2d(data)
        for j, col in enumerate(_STRESS_COLS[:data.shape[1]]):
            _merge(pd.Series(data[:, j], index=nnum, name=col))

    # Temperature
    nt = _safe(rst.nodal_temperature, result_index)
    if nt is not None:
        nnum, data = nt
        _merge(pd.Series(np.atleast_1d(data), index=nnum, name='Temperature'))

    # Displacement magnitude + components
    nd = _safe(rst.nodal_displacement, result_index)
    if nd is not None:
        nnum, data = nd
        data = np.atleast_2d(data)
        mag  = np.linalg.norm(data[:, :3], axis=1)
        _merge(pd.Series(mag, index=nnum, name='Disp_Magnitude'))
        for j, col in enumerate(['UX', 'UY', 'UZ']):
            if j < data.shape[1]:
                _merge(pd.Series(data[:, j], index=nnum, name=col))

    # Sanitise NaN values introduced by node-ID mismatches
    for col in scalar_cols:
        if col in results_df.columns:
            results_df[col] = (results_df[col]
                               .fillna(0.0)
                               .astype(np.float64))

    return nodes_df, elements_df, results_df, scalar_cols


# ═══════════════════════════════════════════════════════════════════════════════
# § 4  SYNTHETIC DEMO DATA
# ═══════════════════════════════════════════════════════════════════════════════

def generate_synthetic_fea_data(
    nx: int = 8,
    ny: int = 5,
    nz: int = 4,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Build a structured Hex8 brick mesh with two analytical scalar fields.
    Used as the demo dataset on startup and via File → Load Demo.

    Returns
    -------
    nodes_df    : [Node_ID, X, Y, Z]
    elements_df : [Element_ID, N1…N8]
    results_df  : [Node_ID, Von_Mises_Stress, Temperature]
    """
    px = np.linspace(0.0, 100.0, nx + 1)
    py = np.linspace(0.0,  60.0, ny + 1)
    pz = np.linspace(0.0,  40.0, nz + 1)

    node_ids, xs, ys, zs = [], [], [], []
    node_index: dict = {}
    nid = 0
    for iz in range(nz + 1):
        for iy in range(ny + 1):
            for ix in range(nx + 1):
                node_index[(ix, iy, iz)] = nid
                node_ids.append(nid + 1)
                xs.append(px[ix])
                ys.append(py[iy])
                zs.append(pz[iz])
                nid += 1

    nodes_df = pd.DataFrame(
        {'Node_ID': node_ids, 'X': xs, 'Y': ys, 'Z': zs})

    elem_ids, connectivity = [], []
    eid = 1
    for iz in range(nz):
        for iy in range(ny):
            for ix in range(nx):
                def n(dx, dy, dz):
                    return node_index[(ix+dx, iy+dy, iz+dz)] + 1
                connectivity.append([
                    n(0,0,0), n(1,0,0), n(1,1,0), n(0,1,0),
                    n(0,0,1), n(1,0,1), n(1,1,1), n(0,1,1),
                ])
                elem_ids.append(eid)
                eid += 1

    elements_df = pd.DataFrame(
        connectivity, columns=[f'N{j+1}' for j in range(8)])
    elements_df.insert(0, 'Element_ID', elem_ids)

    x_arr = nodes_df['X'].values
    y_arr = nodes_df['Y'].values
    z_arr = nodes_df['Z'].values
    cx, cy, cz = 100.0, 30.0, 20.0
    dist = np.sqrt((x_arr-cx)**2 + (y_arr-cy)**2 + (z_arr-cz)**2)
    rng  = np.random.default_rng(42)
    vm   = 450.0 * np.exp(-dist / 55.0) + 50.0 * rng.random(len(x_arr))
    temp = 20.0 + 1.8 * x_arr + 12.0 * np.sin(np.pi * z_arr / 40.0)

    results_df = pd.DataFrame({
        'Node_ID':          nodes_df['Node_ID'],
        'Von_Mises_Stress': vm.round(4),
        'Temperature':      temp.round(4),
    })
    return nodes_df, elements_df, results_df


# ═══════════════════════════════════════════════════════════════════════════════
# § 5  VTK UNSTRUCTURED GRID BUILDER
# ═══════════════════════════════════════════════════════════════════════════════

def build_vtk_unstructured_grid(
    nodes_df:    pd.DataFrame,
    elements_df: pd.DataFrame,
    results_df:  pd.DataFrame,
    scalar_cols: list | None = None,
) -> 'pv.UnstructuredGrid':
    """
    Convert tabular FEA data into a PyVista UnstructuredGrid via
    a pure-NumPy flat-array pipeline (no Python-level VTK cell loop).

    Element type is auto-detected from the number of N-columns:
        4 nodes  → Tet4   (VTK_TETRA)
        6 nodes  → Wedge6 (VTK_WEDGE)
        8 nodes  → Hex8   (VTK_HEXAHEDRON)   ← typical RST / synthetic
       10 nodes  → Tet10  (VTK_QUADRATIC_TETRA)
       20 nodes  → Hex20  (VTK_QUADRATIC_HEXAHEDRON)

    Parameters
    ----------
    nodes_df    : DataFrame with [Node_ID, X, Y, Z]
    elements_df : DataFrame with [Element_ID, N1…Nn]
    results_df  : DataFrame with [Node_ID, <scalar columns…>]
    scalar_cols : columns to attach; None → all non-ID columns

    Returns
    -------
    pv.UnstructuredGrid with all scalars attached as point_data
    """
    _init_vtk_cell_map()

    # ── Points (N × 3, float64) ───────────────────────────────────────────────
    points = nodes_df[['X', 'Y', 'Z']].values.astype(np.float64)

    # ── Node-ID → 0-based position map ───────────────────────────────────────
    node_id_to_idx = {int(nid): i
                      for i, nid in enumerate(nodes_df['Node_ID'])}

    # ── Detect node columns ───────────────────────────────────────────────────
    node_cols = [c for c in elements_df.columns
                 if c.startswith('N') and c[1:].isdigit()]
    n_per_elem = len(node_cols)
    cell_type_id = _VTK_CELL_TYPE_MAP.get(n_per_elem, vtk.VTK_HEXAHEDRON)

    # ── Connectivity → flat VTK cell array [count, n0, n1, …, count, …] ──────
    raw  = elements_df[node_cols].values.astype(np.int64)
    n_el = len(raw)
    conn_0 = np.vectorize(lambda x: node_id_to_idx.get(int(x), 0))(raw)
    prefix = np.full((n_el, 1), n_per_elem, dtype=np.int64)
    cells  = np.hstack([prefix, conn_0]).ravel()
    ctypes = np.full(n_el, cell_type_id, dtype=np.uint8)

    grid = pv.UnstructuredGrid(cells, ctypes, points)

    # ── Attach scalar arrays ──────────────────────────────────────────────────
    res_idx  = results_df.set_index('Node_ID').reindex(nodes_df['Node_ID'])
    to_attach = (scalar_cols if scalar_cols
                 else [c for c in results_df.columns if c != 'Node_ID'])
    first = None
    for col in to_attach:
        if col in res_idx.columns:
            vals = res_idx[col].fillna(0.0).values.astype(np.float64)
            grid.point_data[col] = vals
            if first is None:
                first = col

    if first:
        grid.set_active_scalars(first)

    return grid


# ═══════════════════════════════════════════════════════════════════════════════
# § 6  ANSYS IMPORT DIALOG
# ═══════════════════════════════════════════════════════════════════════════════

class ANSYSImportDialog:
    """
    Modal dialog for loading an ANSYS binary/ASCII file.

    Two exit paths for the user:
        "Import → Data Grid"   — pushes raw tables into the right panel only.
        "Import + Render in 3D"— also rebuilds the VTK mesh and re-renders.

    On completion calls:
        app._on_ansys_import_complete(pairs, base_name,
                                      nodes_df, elements_df, results_df,
                                      scalar_cols, source_path, ext)
    """

    def __init__(self, master: tk.Tk, app: 'FEAPostProcessor') -> None:
        self.master = master
        self.app    = app
        self._obj   = None
        self._path  = None
        self._ext   = None
        self._check_vars: dict[str, tk.BooleanVar] = {}

        self.win = tk.Toplevel(master)
        self.win.title('⚙  ANSYS File Importer')
        self.win.geometry('970x690')
        self.win.minsize(800, 530)
        self.win.grab_set()
        self.win.configure(bg='#1a1d23')
        self._build()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        # Header strip
        hdr = tk.Frame(self.win, bg='#1565C0')
        hdr.pack(fill='x')
        tk.Label(hdr,
            text='  ⚙  ANSYS File Importer  —  No ANSYS licence required',
            font=('Segoe UI', 10, 'bold'),
            bg='#1565C0', fg='white', pady=8).pack(side='left')
        lib_txt = ('✔ ansys-mapdl-reader ready'
                   if HAS_ANSYS_READER
                   else '✘  pip install ansys-mapdl-reader')
        tk.Label(hdr, text=lib_txt,
            font=('Segoe UI', 8),
            bg='#1565C0', fg='#90CAF9', pady=8).pack(side='right', padx=12)

        # File selection row
        fr = tk.Frame(self.win, bg='#1a1d23')
        fr.pack(fill='x', padx=12, pady=(10, 4))
        tk.Label(fr, text='File:',
            font=('Segoe UI', 9, 'bold'),
            bg='#1a1d23', fg='#cdd2dc').pack(side='left')
        self._path_var = tk.StringVar()
        tk.Entry(fr, textvariable=self._path_var,
            font=('Segoe UI', 9), width=56,
            bg='#252b34', fg='#e8eaf0',
            insertbackground='white',
            relief='flat', bd=1).pack(side='left', padx=6)
        for label, cmd, clr in [
            ('Browse…',   self._browse, '#1565C0'),
            ('  Probe  ', self._probe,  '#2E7D32'),
        ]:
            tk.Button(fr, text=label, command=cmd,
                font=('Segoe UI', 8, 'bold'), relief='flat',
                bg=clr, fg='white',
                activebackground='#42A5F5',
                activeforeground='white',
                padx=8, pady=3, cursor='hand2').pack(side='left', padx=3)

        # Load-step row (RST/RTH only)
        self._ls_frame = tk.Frame(self.win, bg='#1a1d23')
        self._ls_frame.pack(fill='x', padx=12, pady=(0, 4))
        tk.Label(self._ls_frame, text='Result index:',
            font=('Segoe UI', 8), bg='#1a1d23',
            fg='#8892a4').pack(side='left')
        self._rnum_var = tk.StringVar(value='0')
        self._rnum_cb  = ttk.Combobox(self._ls_frame,
            textvariable=self._rnum_var,
            values=['0'], state='readonly', width=8,
            font=('Segoe UI', 8))
        self._rnum_cb.pack(side='left', padx=4)
        self._all_rnums_var = tk.BooleanVar(value=False)
        tk.Checkbutton(self._ls_frame,
            text='All result sets (one tab per set)',
            variable=self._all_rnums_var,
            font=('Segoe UI', 8),
            bg='#1a1d23', fg='#8892a4',
            selectcolor='#2d3440',
            activebackground='#1a1d23').pack(side='left', padx=6)

        # Centre split pane
        pane = tk.PanedWindow(self.win, orient='horizontal',
            sashwidth=5, sashrelief='raised', bg='#1a1d23')
        pane.pack(fill='both', expand=True, padx=12, pady=4)

        left  = tk.Frame(pane, bg='#1a1d23')
        right = tk.Frame(pane, bg='#1a1d23')
        pane.add(left,  minsize=350)
        pane.add(right, minsize=260)
        pane.update_idletasks()
        pane.sash_place(0, 460, 0)

        # Left: scrollable checklist
        tk.Label(left, text='Select data to import:',
            font=('Segoe UI', 9, 'bold'),
            bg='#1a1d23', fg='#4fc3f7').pack(anchor='w', pady=(0, 4))
        self._checklist_outer = tk.Frame(left, bg='#1a1d23')
        self._checklist_outer.pack(fill='both', expand=True)
        self._build_checklist_placeholder()

        btn_row = tk.Frame(left, bg='#1a1d23')
        btn_row.pack(fill='x', pady=4)
        for label, val in [('Select All', True), ('Clear All', False)]:
            tk.Button(btn_row, text=label,
                font=('Segoe UI', 7), relief='flat', cursor='hand2',
                bg='#2d3440', fg='#cdd2dc',
                activebackground='#4fc3f7', activeforeground='#1a1d23',
                command=lambda v=val: [
                    var.set(v) for var in self._check_vars.values()
                ]).pack(side='left', padx=2)

        # Right: file info panel
        tk.Label(right, text='File Information:',
            font=('Segoe UI', 9, 'bold'),
            bg='#1a1d23', fg='#4fc3f7').pack(anchor='w', pady=(0, 4))
        self._info = tk.Text(right,
            font=('Consolas', 8), wrap='word', state='disabled',
            bg='#0d1117', fg='#79c0ff',
            relief='flat', bd=1,
            selectbackground='#1565C0')
        vsb = tk.Scrollbar(right, command=self._info.yview,
            bg='#252b34', troughcolor='#1a1d23')
        self._info.configure(yscrollcommand=vsb.set)
        vsb.pack(side='right', fill='y')
        self._info.pack(fill='both', expand=True)

        # Bottom action bar
        bot = tk.Frame(self.win, bg='#252b34', relief='groove', bd=1)
        bot.pack(fill='x', side='bottom', padx=12, pady=8)

        tk.Button(bot, text='⚙  Import → Data Grid',
            command=lambda: self._import(render=False),
            bg='#1B5E20', fg='white',
            font=('Segoe UI', 9, 'bold'),
            relief='flat', padx=12, pady=6,
            cursor='hand2').pack(side='left', padx=4)

        tk.Button(bot, text='⬡  Import + Render in 3D',
            command=lambda: self._import(render=True),
            bg='#0d47a1', fg='white',
            font=('Segoe UI', 9, 'bold'),
            relief='flat', padx=12, pady=6,
            cursor='hand2').pack(side='left', padx=4)

        tk.Button(bot, text='Cancel', command=self.win.destroy,
            font=('Segoe UI', 9), relief='flat',
            bg='#37474F', fg='white',
            padx=12, pady=6, cursor='hand2').pack(side='right', padx=4)

        self._status = tk.Label(bot, text='Open a file to begin.',
            font=('Segoe UI', 8), fg='#8892a4',
            bg='#252b34', anchor='w')
        self._status.pack(side='left', padx=10, fill='x', expand=True)

    # ── Checklist ─────────────────────────────────────────────────────────────

    def _build_checklist_placeholder(self) -> None:
        for w in self._checklist_outer.winfo_children():
            w.destroy()
        tk.Label(self._checklist_outer,
            text='Probe a file to see available data.',
            font=('Segoe UI', 9), fg='#555',
            bg='#1a1d23').pack(pady=20)

    def _build_checklist(self, ext: str) -> None:
        for w in self._checklist_outer.winfo_children():
            w.destroy()
        self._check_vars.clear()

        sections = _ANSYS_SELECTIONS.get(ext, {})
        if not sections:
            tk.Label(self._checklist_outer,
                text=f"No import config for '{ext}'.",
                fg='#666', bg='#1a1d23').pack(pady=10)
            return

        cv  = tk.Canvas(self._checklist_outer,
                        bg='#1a1d23', highlightthickness=0)
        vsb = tk.Scrollbar(self._checklist_outer, command=cv.yview,
                           bg='#252b34', troughcolor='#1a1d23')
        cv.configure(yscrollcommand=vsb.set)
        vsb.pack(side='right', fill='y')
        cv.pack(side='left', fill='both', expand=True)

        inner = tk.Frame(cv, bg='#1a1d23')
        cv.create_window((0, 0), window=inner, anchor='nw')
        inner.bind('<Configure>',
            lambda e: cv.configure(scrollregion=cv.bbox('all')))

        for sec_name, items in sections.items():
            if not items:
                continue
            bg, fg = _SECTION_COLORS.get(sec_name, ('#555', 'white'))
            tk.Label(inner, text=sec_name,
                font=('Segoe UI', 8, 'bold'),
                bg=bg, fg=fg, padx=6, pady=3,
                anchor='w').pack(fill='x', pady=(6, 1))
            for item in items:
                var = tk.BooleanVar(value=(item in _ANSYS_DEFAULT_ON))
                self._check_vars[item] = var
                tk.Checkbutton(inner, text=item, variable=var,
                    font=('Segoe UI', 8),
                    bg='#1a1d23', fg='#cdd2dc',
                    selectcolor='#2d3440',
                    activebackground='#1a1d23',
                    activeforeground='#4fc3f7').pack(
                    anchor='w', padx=12, pady=1)

    # ── Browse / Probe ────────────────────────────────────────────────────────

    def _browse(self) -> None:
        path = filedialog.askopenfilename(
            title='Open ANSYS File',
            filetypes=[
                ('All ANSYS files',
                 '*.rst *.RST *.rth *.RTH *.full *.FULL '
                 '*.emat *.EMAT *.cdb *.CDB *.dat *.DAT'),
                ('RST/RTH Result',    '*.rst *.rth'),
                ('FULL Matrix',       '*.full'),
                ('EMAT Element Mat.', '*.emat'),
                ('CDB/DAT Archive',   '*.cdb *.dat'),
                ('All Files',         '*.*'),
            ],
            parent=self.win)
        if path:
            self._path_var.set(path)
            self._probe()

    def _probe(self) -> None:
        if not HAS_ANSYS_READER:
            messagebox.showerror('Missing library',
                'pip install ansys-mapdl-reader', parent=self.win)
            return
        path = self._path_var.get().strip()
        if not path or not os.path.isfile(path):
            messagebox.showerror('File not found',
                f'Could not find:\n{path}', parent=self.win)
            return

        ext = Path(path).suffix.lower()
        self._set_status('Reading file…')
        self.win.update_idletasks()

        try:
            obj = (_archive_mod.Archive(path, read_parameters=True)
                   if ext in ('.cdb', '.dat')
                   else read_binary(path))
        except Exception as exc:
            messagebox.showerror('Read error', str(exc), parent=self.win)
            self._set_status('Failed.')
            return

        self._obj  = obj
        self._path = path
        self._ext  = ext

        self._build_checklist(ext)

        if ext in ('.rst', '.rth'):
            self._ls_frame.pack(fill='x', padx=12, pady=(0, 4))
            n = getattr(obj, 'n_results', 1)
            self._rnum_cb.configure(values=[str(i) for i in range(n)])
            self._rnum_var.set('0')
        else:
            self._ls_frame.pack_forget()

        self._populate_info(obj, path, ext)
        self._set_status('Ready — select data and click Import.')

    # ── File info panel ───────────────────────────────────────────────────────

    def _populate_info(self, obj, path: str, ext: str) -> None:
        lines = [
            f'Path:   {path}',
            f'Size:   {os.path.getsize(path) / 1e6:.2f} MB',
            f'Format: {ext.upper()}',
        ]
        if ext in ('.rst', '.rth'):
            lines += self._info_rst(obj)
        elif ext == '.full':
            lines += self._info_full(obj)
        elif ext == '.emat':
            lines += self._info_emat(obj)
        elif ext in ('.cdb', '.dat'):
            lines += self._info_cdb(obj)
        self._set_info('\n'.join(lines))

    def _info_rst(self, rst) -> list:
        lines = []
        for attr, lbl in [('version', 'ANSYS'), ('n_results', 'Results')]:
            v = _safe(getattr, rst, attr)
            if v is not None:
                lines.append(f'{lbl}: {v}')
        try:
            m = rst.mesh
            lines += [f'Nodes:  {m.n_node:,}', f'Elems:  {m.n_elem:,}']
        except Exception:
            pass
        tv = _safe(getattr, rst, 'time_values')
        if tv is not None:
            shown = tv[:6].tolist()
            lines.append(
                f'Time:   {[round(t,4) for t in shown]}'
                + (f' …[{len(tv)}]' if len(tv) > 6 else ''))
        ar = _safe(str, _safe(getattr, rst, 'available_results'))
        if ar:
            lines.append(f'\nAvailable:\n{ar}')
        try:
            nc = list(rst.mesh.node_components.keys())
            if nc:
                lines.append(f'\nNode comps: {nc}')
            ec = list(rst.mesh.element_components.keys())
            if ec:
                lines.append(f'Elem comps: {ec}')
        except Exception:
            pass
        try:
            si = rst.solution_info(0)
            lines.append('\nSolution info (set 0):')
            for k, v in list(si.items())[:10]:
                lines.append(f'  {k}: {v}')
        except Exception:
            pass
        return lines

    def _info_full(self, fl) -> list:
        return [
            f'Equations: {fl.neqn}',
            f'K shape:   {fl.k.shape}  nnz={fl.k.nnz:,}',
            f'M shape:   {fl.m.shape}  nnz={fl.m.nnz:,}',
            f'Load vec:  {fl.load_vector.shape[0]} DOFs',
            f'Const DOFs:{fl.const.shape[0]}',
            f'\nDOF ref (first 8):\n{fl.dof_ref[:8]}',
        ]

    def _info_emat(self, em) -> list:
        lines = [f'Elements: {em.n_elements:,}',
                 f'Nodes:    {em.n_nodes:,}',
                 f'DOF/node: {em.n_dof}']
        hdr = _safe(em.read_header)
        if hdr:
            lines.append('\nHeader:')
            for k, v in list(hdr.items())[:12]:
                lines.append(f'  {k}: {v}')
        return lines

    def _info_cdb(self, ar) -> list:
        lines = [f'Nodes:    {ar.n_node:,}', f'Elements: {ar.n_elem:,}']
        nc = ar.node_components
        if nc:
            lines.append(f'Node comps: {list(nc.keys())}')
        ec = ar.element_components
        if ec:
            lines.append(f'Elem comps: {list(ec.keys())}')
        lines.append(f'Elem types: {[int(e[1]) for e in ar.ekey]}')
        qual = _safe(lambda: ar.quality)
        if qual is not None:
            lines.append(
                f'Mesh quality  min={qual.min():.3f}'
                f'  mean={qual.mean():.3f}')
        return lines

    # ── Import ────────────────────────────────────────────────────────────────

    def _import(self, render: bool = False) -> None:
        if self._obj is None:
            messagebox.showinfo('No file', 'Probe a file first.',
                                parent=self.win)
            return
        selected = {k for k, v in self._check_vars.items() if v.get()}
        if not selected:
            messagebox.showinfo('Nothing selected',
                'Tick at least one item.', parent=self.win)
            return

        ext = self._ext
        obj = self._obj

        if ext in ('.rst', '.rth'):
            n     = getattr(obj, 'n_results', 1)
            rnums = (list(range(n)) if self._all_rnums_var.get()
                     else [int(self._rnum_var.get())])
        else:
            rnums = [0]

        self._set_status('Extracting…')
        self.win.update_idletasks()

        # ── Raw tabular extraction ────────────────────────────────────────────
        try:
            if ext in ('.rst', '.rth'):
                pairs = extract_rst(obj, selected, rnums)
            elif ext == '.full':
                pairs = extract_full(obj, selected)
            elif ext == '.emat':
                pairs = extract_emat(obj, selected)
            elif ext in ('.cdb', '.dat'):
                pairs = extract_cdb(obj, selected)
            else:
                pairs = []
        except Exception as exc:
            messagebox.showerror('Extract error', str(exc), parent=self.win)
            self._set_status('Failed.')
            return

        # ── Build viewer DataFrames for 3D rendering (RST/RTH only) ──────────
        nodes_df = elements_df = results_df = None
        scalar_cols: list = []

        if render and ext in ('.rst', '.rth'):
            ri = int(self._rnum_var.get())
            try:
                nodes_df, elements_df, results_df, scalar_cols = \
                    rst_to_viewer_dataframes(obj, result_index=ri)
            except Exception as exc:
                messagebox.showwarning(
                    '3D render warning',
                    f'Could not extract 3D geometry:\n{exc}\n\n'
                    'Tabular import will still proceed.',
                    parent=self.win)
                render = False

        # ── Notify the main application ───────────────────────────────────────
        self.app._on_ansys_import_complete(
            pairs       = pairs,
            base_name   = Path(self._path).stem,
            nodes_df    = nodes_df    if render else None,
            elements_df = elements_df if render else None,
            results_df  = results_df  if render else None,
            scalar_cols = scalar_cols if render else [],
            source_path = self._path,
            ext         = ext,
        )

        n_tabs = len(pairs)
        self._set_status(
            f'✓ {n_tabs} tab(s) extracted'
            + ('  +  rendered in 3D' if render else ''))
        if n_tabs > 0:
            self.win.after(700, self.win.destroy)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _set_info(self, text: str) -> None:
        self._info.configure(state='normal')
        self._info.delete('1.0', 'end')
        self._info.insert('end', text)
        self._info.configure(state='disabled')

    def _set_status(self, msg: str) -> None:
        self._status.configure(text=msg)


# ═══════════════════════════════════════════════════════════════════════════════
# § 7  MAIN APPLICATION CLASS
# ═══════════════════════════════════════════════════════════════════════════════

class FEAPostProcessor:
    """
    Top-level application controller.

    Data-flow summary
    -----------------
    Startup:
        generate_synthetic_fea_data()  →  nodes_df / elements_df / results_df
        build_vtk_unstructured_grid()  →  self.grid  →  _render_mesh()

    ANSYS import (File → Import ANSYS File):
        ANSYSImportDialog
            → extract_rst / extract_full / extract_emat / extract_cdb
                → _on_ansys_import_complete()
                    → update nodes_df / elements_df / results_df / scalar_cols
                    → rebuild VTK grid
                    → refresh sidebar dropdowns + colorbar range
                    → _render_mesh()
            → _add_imported_tab()  (raw extracted DataFrames → data grid)
    """

    # ── Colour palette ────────────────────────────────────────────────────────
    BG_DARK  = '#1e2228'
    BG_MED   = '#252b34'
    BG_LIGHT = '#2d3440'
    ACCENT   = '#4fc3f7'
    FG_TEXT  = '#e8eaf0'
    FG_MUTED = '#8892a4'
    BORDER   = '#3d4554'
    GREEN    = '#69f0ae'
    ORANGE   = '#ffb74d'
    RED      = '#ef5350'

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self._configure_root()
        self._bind_shortcuts()

        # ── Data layer ────────────────────────────────────────────────────────
        self.nodes_df, self.elements_df, self.results_df = \
            generate_synthetic_fea_data()
        self._scalar_cols: list  = ['Von_Mises_Stress', 'Temperature']
        self._source_label: str  = 'Synthetic (demo)'

        # ── VTK grid ──────────────────────────────────────────────────────────
        if VTK_AVAILABLE:
            self.grid = build_vtk_unstructured_grid(
                self.nodes_df, self.elements_df,
                self.results_df, self._scalar_cols)

        # ── Tk state vars ─────────────────────────────────────────────────────
        self.active_result   = tk.StringVar(value=self._scalar_cols[0])
        self.colorbar_min    = tk.DoubleVar()
        self.colorbar_max    = tk.DoubleVar()
        self._show_edges_var = tk.BooleanVar(value=False)
        self._show_axes_var  = tk.BooleanVar(value=True)
        self._picking_var    = tk.BooleanVar(value=False)

        # ── VTK actor handles ─────────────────────────────────────────────────
        self._actor           = None
        self._scalar_bar      = None
        self._highlight_actor = None

        # ── Extra ANSYS-imported tabs (name → DataFrame) ──────────────────────
        self._imported_tabs: dict[str, pd.DataFrame] = {}

        # ── Build UI ──────────────────────────────────────────────────────────
        self._build_menu()
        self._build_layout()

        if VTK_AVAILABLE:
            self._init_vtk_renderer()
            self._render_mesh()
        else:
            self._show_vtk_fallback()

    # ══════════════════════════════════════════════════════════════════════════
    # Root configuration & keyboard shortcuts
    # ══════════════════════════════════════════════════════════════════════════

    def _configure_root(self) -> None:
        self.root.title('FEA Post-Processor  ·  3D Results Viewer')
        self.root.geometry('1700x960')
        self.root.minsize(1200, 700)
        self.root.configure(bg=self.BG_DARK)

        s = ttk.Style()
        s.theme_use('clam')
        s.configure('.', background=self.BG_DARK, foreground=self.FG_TEXT,
            fieldbackground=self.BG_LIGHT, troughcolor=self.BG_MED,
            bordercolor=self.BORDER, darkcolor=self.BG_DARK,
            lightcolor=self.BG_LIGHT, font=('Segoe UI', 10))
        s.configure('TPanedwindow', background=self.BG_DARK)
        s.configure('Sash', sashrelief='flat', sashpad=2,
                    background=self.BORDER)
        s.configure('TLabel', background=self.BG_DARK,
            foreground=self.FG_TEXT, font=('Segoe UI', 10))
        s.configure('Muted.TLabel', background=self.BG_DARK,
            foreground=self.FG_MUTED, font=('Segoe UI', 9))
        s.configure('TButton', background=self.BG_LIGHT,
            foreground=self.FG_TEXT, bordercolor=self.BORDER,
            relief='flat', font=('Segoe UI', 10), padding=(10, 6))
        s.map('TButton',
            background=[('active', self.ACCENT)],
            foreground=[('active', self.BG_DARK)])
        s.configure('Accent.TButton', background=self.ACCENT,
            foreground=self.BG_DARK,
            font=('Segoe UI', 10, 'bold'), padding=(10, 6))
        s.map('Accent.TButton',
            background=[('active', '#81d4fa')])
        s.configure('TCombobox', background=self.BG_LIGHT,
            foreground=self.FG_TEXT, fieldbackground=self.BG_LIGHT,
            selectbackground=self.ACCENT, selectforeground=self.BG_DARK,
            arrowcolor=self.ACCENT)
        s.map('TCombobox',
            fieldbackground=[('readonly', self.BG_LIGHT)],
            foreground=[('readonly', self.FG_TEXT)])
        s.configure('TSpinbox', background=self.BG_LIGHT,
            foreground=self.FG_TEXT, fieldbackground=self.BG_LIGHT,
            arrowcolor=self.ACCENT, insertcolor=self.FG_TEXT)
        s.configure('TFrame', background=self.BG_DARK)
        s.configure('TScrollbar', background=self.BG_MED,
            troughcolor=self.BG_DARK, arrowcolor=self.FG_MUTED,
            bordercolor=self.BORDER)

    def _bind_shortcuts(self) -> None:
        self.root.bind('<Control-o>', lambda _e: self._cmd_import_ansys())
        self.root.bind('<Control-r>', lambda _e: self._cmd_reset_camera())
        self.root.bind('<Control-e>', lambda _e: self._cmd_toggle_edges())
        self.root.bind('<F5>',        lambda _e: self._cmd_auto_scale())

    # ══════════════════════════════════════════════════════════════════════════
    # Menu bar
    # ══════════════════════════════════════════════════════════════════════════

    def _build_menu(self) -> None:
        kw = dict(bg=self.BG_MED, fg=self.FG_TEXT,
                  activebackground=self.ACCENT, activeforeground=self.BG_DARK)
        mb = tk.Menu(self.root, **kw, borderwidth=0, relief='flat')

        fm = tk.Menu(mb, tearoff=0, **kw)
        fm.add_command(
            label='⚙  Import ANSYS File…  (Ctrl+O)',
            command=self._cmd_import_ansys)
        fm.add_separator()
        fm.add_command(label='Load Synthetic Demo Data',
                       command=self._cmd_load_demo)
        fm.add_separator()
        fm.add_command(label='Exit', command=self.root.quit)
        mb.add_cascade(label='File', menu=fm)

        vm = tk.Menu(mb, tearoff=0, **kw)
        vm.add_command(label='Reset Camera  (Ctrl+R)',
                       command=self._cmd_reset_camera)
        vm.add_command(label='Toggle Edges  (Ctrl+E)',
                       command=self._cmd_toggle_edges)
        vm.add_command(label='Toggle Axes Widget',
                       command=self._cmd_toggle_axes)
        vm.add_separator()
        vm.add_command(label='Auto-Scale Colorbar  (F5)',
                       command=self._cmd_auto_scale)
        mb.add_cascade(label='View', menu=vm)

        hm = tk.Menu(mb, tearoff=0, **kw)
        hm.add_command(label='About', command=self._cmd_about)
        mb.add_cascade(label='Help', menu=hm)

        self.root.config(menu=mb)

    # ══════════════════════════════════════════════════════════════════════════
    # Layout
    # ══════════════════════════════════════════════════════════════════════════

    def _build_layout(self) -> None:
        self._build_status_bar()

        # Title bar
        tbar = tk.Frame(self.root, bg=self.BG_MED, height=44)
        tbar.pack(side='top', fill='x')
        tbar.pack_propagate(False)
        tk.Label(tbar, text='⬡  FEA Post-Processor',
            bg=self.BG_MED, fg=self.ACCENT,
            font=('Segoe UI', 13, 'bold')).pack(
            side='left', padx=16, pady=8)
        self._source_lbl_var = tk.StringVar(value=self._source_label)
        tk.Label(tbar, textvariable=self._source_lbl_var,
            bg=self.BG_MED, fg=self.FG_MUTED,
            font=('Segoe UI', 9)).pack(side='right', padx=16)

        self.paned = ttk.PanedWindow(self.root, orient='horizontal')
        self.paned.pack(fill='both', expand=True)

        self._build_left_panel()
        self._build_center_panel()
        self._build_right_panel()

    def _build_status_bar(self) -> None:
        bar = tk.Frame(self.root, bg=self.BG_MED, height=24)
        bar.pack(side='bottom', fill='x')
        bar.pack_propagate(False)
        self._status_var = tk.StringVar(value='Ready')
        tk.Label(bar, textvariable=self._status_var,
            bg=self.BG_MED, fg=self.FG_MUTED,
            font=('Segoe UI', 8), anchor='w').pack(
            side='left', padx=12, fill='y')
        self._coord_var = tk.StringVar(value='')
        tk.Label(bar, textvariable=self._coord_var,
            bg=self.BG_MED, fg=self.FG_MUTED,
            font=('Segoe UI', 8), anchor='e').pack(
            side='right', padx=12, fill='y')

    # ── Left sidebar ──────────────────────────────────────────────────────────

    def _build_left_panel(self) -> None:
        frame = tk.Frame(self.paned, bg=self.BG_DARK, width=275)
        frame.pack_propagate(False)
        self.paned.add(frame, weight=0)

        cv = tk.Canvas(frame, bg=self.BG_DARK, highlightthickness=0, bd=0)
        sb = ttk.Scrollbar(frame, orient='vertical', command=cv.yview)
        self._sidebar_frame = tk.Frame(cv, bg=self.BG_DARK)
        self._sidebar_frame.bind('<Configure>',
            lambda e: cv.configure(scrollregion=cv.bbox('all')))
        cv.create_window((0, 0), window=self._sidebar_frame, anchor='nw')
        cv.configure(yscrollcommand=sb.set)
        cv.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')

        self._build_sidebar_content(self._sidebar_frame)

    def _build_sidebar_content(self, parent: tk.Frame) -> None:
        p = {'padx': 12, 'pady': 0}
        self._update_scalar_range_vars()

        # § ANSYS Import ───────────────────────────────────────────────────────
        self._sidebar_section(parent, '  ANSYS IMPORT')
        card = self._card(parent)
        lib_txt   = ('ansys-mapdl-reader  ✔' if HAS_ANSYS_READER
                     else 'ansys-mapdl-reader  ✘')
        lib_color = self.GREEN if HAS_ANSYS_READER else self.RED
        tk.Label(card, text=lib_txt,
            bg=self.BG_MED, fg=lib_color,
            font=('Consolas', 8)).pack(anchor='w', **p, pady=(6, 4))
        ttk.Button(card, text='⚙  Import ANSYS File…',
            style='Accent.TButton',
            command=self._cmd_import_ansys).pack(fill='x', **p, pady=(0, 10))

        # § Result field ───────────────────────────────────────────────────────
        self._sidebar_section(parent, '  RESULT FIELD')
        card = self._card(parent)
        ttk.Label(card, text='Active Result',
            style='Muted.TLabel').pack(anchor='w', **p, pady=(8, 2))
        self._result_cb = ttk.Combobox(card,
            textvariable=self.active_result,
            values=self._scalar_cols,
            state='readonly', width=24)
        self._result_cb.pack(fill='x', **p, pady=(0, 10))
        self._result_cb.bind('<<ComboboxSelected>>', self._on_result_changed)

        # § Colorbar ───────────────────────────────────────────────────────────
        self._sidebar_section(parent, '  COLORBAR LIMITS')
        card = self._card(parent)
        ttk.Label(card, text='Minimum',
            style='Muted.TLabel').pack(anchor='w', **p, pady=(8, 2))
        self._cmin_entry = ttk.Spinbox(card,
            from_=-1e12, to=1e12,
            textvariable=self.colorbar_min,
            increment=10, width=22,
            command=self._on_colorbar_changed)
        self._cmin_entry.pack(fill='x', **p, pady=(0, 6))
        self._cmin_entry.bind('<Return>',   self._on_colorbar_changed)
        self._cmin_entry.bind('<FocusOut>', self._on_colorbar_changed)

        ttk.Label(card, text='Maximum',
            style='Muted.TLabel').pack(anchor='w', **p, pady=(0, 2))
        self._cmax_entry = ttk.Spinbox(card,
            from_=-1e12, to=1e12,
            textvariable=self.colorbar_max,
            increment=10, width=22,
            command=self._on_colorbar_changed)
        self._cmax_entry.pack(fill='x', **p, pady=(0, 6))
        self._cmax_entry.bind('<Return>',   self._on_colorbar_changed)
        self._cmax_entry.bind('<FocusOut>', self._on_colorbar_changed)
        ttk.Button(card, text='Auto-Scale  (F5)',
            command=self._cmd_auto_scale).pack(fill='x', **p, pady=(2, 10))

        # § Rendering ──────────────────────────────────────────────────────────
        self._sidebar_section(parent, '  RENDERING')
        card = self._card(parent)
        ttk.Checkbutton(card, text='Show mesh edges',
            variable=self._show_edges_var,
            command=self._on_edges_toggled).pack(
            anchor='w', **p, pady=(8, 4))
        ttk.Checkbutton(card, text='Show orientation axes',
            variable=self._show_axes_var,
            command=self._on_axes_toggled).pack(
            anchor='w', **p, pady=(0, 4))
        ttk.Button(card, text='Reset Camera  (Ctrl+R)',
            command=self._cmd_reset_camera).pack(
            fill='x', **p, pady=(4, 10))

        # § Node probe ─────────────────────────────────────────────────────────
        self._sidebar_section(parent, '  NODE PROBE')
        card = self._card(parent)
        ttk.Label(card,
            text='Click a node in the 3D viewport\nto inspect its properties.',
            style='Muted.TLabel',
            justify='left').pack(anchor='w', **p, pady=(8, 4))
        ttk.Checkbutton(card, text='Enable Node Picking',
            variable=self._picking_var,
            command=self._on_picking_toggled).pack(
            anchor='w', **p, pady=(0, 6))
        ttk.Label(card, text='Inspection Result',
            style='Muted.TLabel').pack(anchor='w', **p, pady=(4, 2))
        self._probe_text = tk.Text(card,
            height=11, width=26,
            bg=self.BG_DARK, fg=self.GREEN,
            font=('Consolas', 9),
            relief='flat', bd=0, state='disabled',
            insertbackground=self.GREEN,
            selectbackground=self.ACCENT)
        self._probe_text.pack(fill='x', **p, pady=(0, 10))
        self._write_probe('Awaiting selection…')

        # § Mesh statistics ────────────────────────────────────────────────────
        self._sidebar_section(parent, '  MESH STATISTICS')
        self._stats_card = self._card(parent)
        self._refresh_stats_card()

        tk.Frame(parent, bg=self.BG_DARK, height=30).pack()

    def _refresh_stats_card(self) -> None:
        """Rebuild stats after a new file is loaded."""
        card = self._stats_card
        for w in card.winfo_children():
            w.destroy()
        node_cols = [c for c in self.elements_df.columns
                     if c.startswith('N') and c[1:].isdigit()]
        et = {4: 'Tet4', 6: 'Wedge6', 8: 'Hex8',
              10: 'Tet10', 20: 'Hex20'}.get(
            len(node_cols), f'{len(node_cols)}-node')
        stats = [
            ('Source',   self._source_label[:22]),
            ('Nodes',    f'{len(self.nodes_df):,}'),
            ('Elements', f'{len(self.elements_df):,}'),
            ('Type',     et),
            ('DOF',      f'{len(self.nodes_df) * 3:,}'),
            ('Scalars',  str(len(self._scalar_cols))),
        ]
        for lbl, val in stats:
            row = tk.Frame(card, bg=self.BG_MED)
            row.pack(fill='x', padx=12, pady=2)
            tk.Label(row, text=lbl, bg=self.BG_MED,
                fg=self.FG_MUTED, font=('Segoe UI', 9)).pack(side='left')
            tk.Label(row, text=val, bg=self.BG_MED,
                fg=self.FG_TEXT, font=('Segoe UI', 9, 'bold')).pack(
                side='right')
        tk.Frame(card, bg=self.BG_MED, height=8).pack()

    def _sidebar_section(self, parent: tk.Frame, title: str) -> None:
        tk.Frame(parent, bg=self.BORDER, height=1).pack(
            fill='x', pady=(12, 0))
        tk.Label(parent, text=title,
            bg=self.BG_MED, fg=self.ACCENT,
            font=('Segoe UI', 8, 'bold'), anchor='w').pack(
            fill='x', ipady=4)

    def _card(self, parent: tk.Frame) -> tk.Frame:
        card = tk.Frame(parent, bg=self.BG_MED)
        card.pack(fill='x')
        return card

    # ── Centre viewport ───────────────────────────────────────────────────────

    def _build_center_panel(self) -> None:
        frame = tk.Frame(self.paned, bg=self.BG_DARK)
        self.paned.add(frame, weight=3)

        toolbar = tk.Frame(frame, bg=self.BG_MED, height=36)
        toolbar.pack(side='top', fill='x')
        toolbar.pack_propagate(False)

        for text, cmd in [
            ('⟳ Reset',  self._cmd_reset_camera),
            ('⊞ Edges',  self._cmd_toggle_edges),
            ('⊹ Axes',   self._cmd_toggle_axes),
            ('✤ Pick',   self._cmd_toggle_pick),
            ('⚙ Import', self._cmd_import_ansys),
        ]:
            tk.Button(toolbar, text=text, command=cmd,
                bg=self.BG_MED, fg=self.FG_TEXT,
                activebackground=self.ACCENT,
                activeforeground=self.BG_DARK,
                relief='flat', bd=0, padx=12, pady=6,
                font=('Segoe UI', 9)).pack(side='left', padx=2)

        self._toolbar_scalar_var = tk.StringVar(value='')
        tk.Label(toolbar, textvariable=self._toolbar_scalar_var,
            bg=self.BG_MED, fg=self.ORANGE,
            font=('Segoe UI', 9, 'bold')).pack(side='right', padx=16)

        border = tk.Frame(frame, bg=self.BORDER)
        border.pack(fill='both', expand=True, padx=2, pady=2)
        self._viewport_frame = tk.Frame(border, bg='#111418')
        self._viewport_frame.pack(fill='both', expand=True, padx=1, pady=1)

    # ── Right data grid ───────────────────────────────────────────────────────

    def _build_right_panel(self) -> None:
        frame = tk.Frame(self.paned, bg=self.BG_DARK, width=440)
        frame.pack_propagate(False)
        self.paned.add(frame, weight=1)

        # Tab header strip
        self._tab_header = tk.Frame(frame, bg=self.BG_MED, height=36)
        self._tab_header.pack(side='top', fill='x')
        self._tab_header.pack_propagate(False)

        self._grid_tab    = tk.StringVar(value='Results')
        self._tab_buttons: dict[str, tk.Button] = {}

        for tab in ('Nodes', 'Elements', 'Results'):
            self._make_tab_button(tab)

        self._grid_container = tk.Frame(frame, bg=self.BG_DARK)
        self._grid_container.pack(fill='both', expand=True)

        self._sheet_frames: dict[str, tk.Frame] = {}
        self._build_builtin_sheets()
        self._switch_grid_tab('Results')

    def _make_tab_button(self, name: str) -> None:
        btn = tk.Button(self._tab_header, text=name,
            command=lambda n=name: self._switch_grid_tab(n),
            bg=self.BG_MED, fg=self.FG_MUTED,
            activebackground=self.BG_LIGHT,
            activeforeground=self.FG_TEXT,
            relief='flat', bd=0, padx=10, pady=6,
            font=('Segoe UI', 9))
        btn.pack(side='left', padx=1)
        self._tab_buttons[name] = btn

    def _build_builtin_sheets(self) -> None:
        for name, df in [('Nodes',    self.nodes_df),
                          ('Elements', self.elements_df),
                          ('Results',  self.results_df)]:
            frm = tk.Frame(self._grid_container, bg=self.BG_DARK)
            self._sheet_frames[name] = frm
            self._populate_sheet_frame(frm, df)

    def _populate_sheet_frame(
        self, frm: tk.Frame, df: pd.DataFrame
    ) -> None:
        """Render df into frm using tksheet or Treeview fallback."""
        for w in frm.winfo_children():
            w.destroy()
        if TKSHEET_AVAILABLE:
            sheet = tksheet.Sheet(frm,
                data=df.values.tolist(),
                headers=df.columns.tolist(),
                theme='dark blue',
                show_row_index=True,
                row_index_width=40,
                header_height=30,
                row_height=24,
                font=('Consolas', 9, 'normal'),
                header_font=('Segoe UI', 9, 'bold'))
            sheet.pack(fill='both', expand=True)
            sheet.enable_bindings((
                'single_select', 'column_select',
                'column_width_resize', 'row_height_resize',
                'right_click_popup_menu', 'rc_select',
                'copy', 'ctrl_click_select'))
        else:
            self._build_treeview(frm, df)

    def _build_treeview(self, parent: tk.Frame, df: pd.DataFrame) -> None:
        cols = df.columns.tolist()
        tv   = ttk.Treeview(parent, columns=cols,
                            show='headings', selectmode='extended')
        for col in cols:
            tv.heading(col, text=col)
            tv.column(col, width=82, anchor='center')
        for _, row in df.head(2000).iterrows():
            tv.insert('', 'end', values=[
                f'{v:.4f}' if isinstance(v, float) else str(v)
                for v in row])
        vsb = ttk.Scrollbar(parent, orient='vertical',   command=tv.yview)
        hsb = ttk.Scrollbar(parent, orient='horizontal', command=tv.xview)
        tv.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side='right',  fill='y')
        hsb.pack(side='bottom', fill='x')
        tv.pack(fill='both', expand=True)

    def _add_imported_tab(self, name: str, df: pd.DataFrame) -> None:
        """Add or replace a dynamic tab for an ANSYS-imported DataFrame."""
        self._imported_tabs[name] = df
        if name not in self._tab_buttons:
            self._make_tab_button(name)
        if name in self._sheet_frames:
            self._sheet_frames[name].destroy()
        frm = tk.Frame(self._grid_container, bg=self.BG_DARK)
        self._sheet_frames[name] = frm
        self._populate_sheet_frame(frm, df)
        self._switch_grid_tab(name)

    def _switch_grid_tab(self, name: str) -> None:
        self._grid_tab.set(name)
        for frm in self._sheet_frames.values():
            frm.pack_forget()
        if name in self._sheet_frames:
            self._sheet_frames[name].pack(fill='both', expand=True)
        for n, btn in self._tab_buttons.items():
            btn.config(
                bg=(self.BG_LIGHT if n == name else self.BG_MED),
                fg=(self.ACCENT   if n == name else self.FG_MUTED))
        df = self._get_tab_df(name)
        if df is not None:
            self._set_status(f'Viewing: {name}  ({len(df):,} rows)')

    def _get_tab_df(self, name: str):
        if name == 'Nodes':    return self.nodes_df
        if name == 'Elements': return self.elements_df
        if name == 'Results':  return self.results_df
        return self._imported_tabs.get(name)

    # ══════════════════════════════════════════════════════════════════════════
    # VTK / PyVista renderer
    # ══════════════════════════════════════════════════════════════════════════

    def _init_vtk_renderer(self) -> None:
        self._vtk_widget = vtkTkRenderWindowInteractor(
            self._viewport_frame,
            rw=vtk.vtkRenderWindow(),
            width=800, height=600)
        self._vtk_widget.pack(fill='both', expand=True)

        self._render_window = self._vtk_widget.GetRenderWindow()
        self._render_window.SetMultiSamples(4)   # MSAA anti-aliasing

        self._plotter  = pv.Plotter(off_screen=False)
        self._plotter.ren_win = self._render_window
        self._renderer = self._plotter.renderer
        self._render_window.AddRenderer(self._renderer)

        # TrackballCamera: left-drag=rotate, right-drag=zoom, middle=pan
        self._vtk_widget.SetInteractorStyle(
            vtk.vtkInteractorStyleTrackballCamera())

        # Orientation axes widget (bottom-left)
        self._axes_widget = vtk.vtkOrientationMarkerWidget()
        self._axes_widget.SetOrientationMarker(vtk.vtkAxesActor())
        self._axes_widget.SetInteractor(self._vtk_widget)
        self._axes_widget.SetViewport(0.0, 0.0, 0.18, 0.18)
        self._axes_widget.SetEnabled(1)
        self._axes_widget.InteractiveOff()

        # Dark gradient background
        self._renderer.SetBackground(0.11, 0.13, 0.17)
        self._renderer.SetBackground2(0.07, 0.08, 0.11)
        self._renderer.GradientBackgroundOn()

        self._vtk_widget.Initialize()
        self._vtk_widget.Start()
        self._set_status('3D renderer ready (VTK + PyVista).')

    def _show_vtk_fallback(self) -> None:
        tk.Label(self._viewport_frame,
            text='PyVista / VTK not installed.\n\n'
                 'pip install pyvista vtk\n\n'
                 'Data grid is fully functional.',
            bg='#111418', fg=self.FG_MUTED,
            font=('Consolas', 11),
            justify='center').place(relx=0.5, rely=0.5, anchor='center')

    # ── Render loop ───────────────────────────────────────────────────────────

    def _render_mesh(self) -> None:
        """
        Re-render the current mesh with the active scalar and colormap bounds.
        Saves and restores the camera position so the user's view is preserved
        across colorbar updates, result-field switches, and file reloads.
        """
        if not VTK_AVAILABLE:
            return

        camera_state = (self._capture_camera()
                        if self._actor is not None else None)

        # Remove stale actors
        for attr in ('_actor', '_scalar_bar'):
            a = getattr(self, attr, None)
            if a is not None:
                self._renderer.RemoveActor(a)
                setattr(self, attr, None)

        # Resolve active scalar — fall back if it was removed
        scalar_name = self.active_result.get()
        available   = list(self.grid.point_data.keys())
        if scalar_name not in available:
            if not available:
                self._set_status('No scalar data to render.')
                return
            scalar_name = available[0]
            self.active_result.set(scalar_name)

        self.grid.set_active_scalars(scalar_name)

        # Validate colormap limits
        try:
            cmin = float(self._cmin_entry.get())
            cmax = float(self._cmax_entry.get())
        except (ValueError, tk.TclError):
            self._update_scalar_range_vars()
            cmin = self.colorbar_min.get()
            cmax = self.colorbar_max.get()

        if cmin >= cmax:
            self._set_status('⚠  Min ≥ Max — colorbar auto-reset.',
                             error=True)
            self._update_scalar_range_vars()
            cmin = self.colorbar_min.get()
            cmax = self.colorbar_max.get()

        # Build VTK mapper
        mapper = vtk.vtkDataSetMapper()
        mapper.SetInputData(self.grid)
        mapper.SetScalarModeToUsePointData()
        mapper.SetColorModeToMapScalars()
        mapper.SelectColorArray(scalar_name)
        mapper.SetScalarRange(cmin, cmax)
        mapper.SetLookupTable(self._build_lut(cmin, cmax))
        mapper.Update()

        # Actor with Gouraud shading (smooth nodal interpolation)
        self._actor = vtk.vtkActor()
        self._actor.SetMapper(mapper)
        prop = self._actor.GetProperty()
        prop.SetInterpolationToGouraud()
        prop.SetOpacity(1.0)
        if self._show_edges_var.get():
            prop.EdgeVisibilityOn()
            prop.SetEdgeColor(0.14, 0.17, 0.21)
            prop.SetLineWidth(0.5)
        else:
            prop.EdgeVisibilityOff()
        self._renderer.AddActor(self._actor)

        # Scalar bar (colorbar)
        self._scalar_bar = vtk.vtkScalarBarActor()
        self._scalar_bar.SetLookupTable(mapper.GetLookupTable())
        self._scalar_bar.SetTitle(scalar_name.replace('_', ' '))
        self._scalar_bar.SetNumberOfLabels(6)
        self._scalar_bar.SetWidth(0.07)
        self._scalar_bar.SetHeight(0.55)
        self._scalar_bar.SetPosition(0.91, 0.22)
        for tp in (self._scalar_bar.GetTitleTextProperty(),
                   self._scalar_bar.GetLabelTextProperty()):
            tp.SetColor(0.88, 0.91, 0.95)
        self._renderer.AddActor(self._scalar_bar)

        if camera_state:
            self._restore_camera(camera_state)
        else:
            self._renderer.ResetCamera()

        self._render_window.Render()
        self._toolbar_scalar_var.set(f'⬡  {scalar_name}')
        self._set_status(
            f'Rendering: {scalar_name}  |  '
            f'[{cmin:.4g} … {cmax:.4g}]  |  '
            f'{len(self.nodes_df):,} nodes  '
            f'{len(self.elements_df):,} elements')

    def _build_lut(self, cmin: float, cmax: float) -> 'vtk.vtkLookupTable':
        """Classic blue → red (Jet) lookup table used in commercial FEA tools."""
        lut = vtk.vtkLookupTable()
        lut.SetNumberOfTableValues(256)
        lut.SetHueRange(0.667, 0.0)     # blue → red
        lut.SetSaturationRange(1.0, 1.0)
        lut.SetValueRange(1.0, 1.0)
        lut.SetTableRange(cmin, cmax)
        lut.Build()
        return lut

    # ── Camera helpers ────────────────────────────────────────────────────────

    def _capture_camera(self) -> dict:
        cam = self._renderer.GetActiveCamera()
        return {'pos':  cam.GetPosition(),
                'fp':   cam.GetFocalPoint(),
                'up':   cam.GetViewUp(),
                'clip': cam.GetClippingRange()}

    def _restore_camera(self, s: dict) -> None:
        cam = self._renderer.GetActiveCamera()
        cam.SetPosition(*s['pos'])
        cam.SetFocalPoint(*s['fp'])
        cam.SetViewUp(*s['up'])
        cam.SetClippingRange(*s['clip'])
        self._renderer.ResetCameraClippingRange()

    # ── Node picking / probing ────────────────────────────────────────────────

    def _enable_picking(self) -> None:
        """Register a LeftButtonPressEvent observer using vtkPointPicker."""
        if not VTK_AVAILABLE:
            return
        self._picker = vtk.vtkPointPicker()
        self._picker.SetTolerance(0.005)

        def on_click(obj, _event):
            if not self._picking_var.get():
                return
            x, y = obj.GetEventPosition()
            self._picker.Pick(x, y, 0, self._renderer)
            pid  = self._picker.GetPointId()
            if pid < 0:
                self._write_probe('No point picked.\nClick closer to a node.')
                return
            self._report_node(pid)

        self._vtk_widget.AddObserver('LeftButtonPressEvent', on_click)

    def _report_node(self, pid: int) -> None:
        """Extract and display all scalar values for a picked node."""
        if pid >= len(self.nodes_df):
            return
        row      = self.nodes_df.iloc[pid]
        nid      = int(row['Node_ID'])
        x, y, z  = row['X'], row['Y'], row['Z']

        res_row = self.results_df[self.results_df['Node_ID'] == nid]
        scalar_lines = []
        if not res_row.empty:
            rr = res_row.iloc[0]
            for col in self._scalar_cols:
                if col in rr.index:
                    scalar_lines.append(
                        f'{col[:16]:<16}\n  {float(rr[col]):>12.4f}')

        active = self.active_result.get()
        active_val = (float(res_row.iloc[0][active])
                      if (not res_row.empty and active in res_row.columns)
                      else float('nan'))

        text = (f'Node ID : {nid}\n'
                + '─' * 22 + '\n'
                f'X  : {x:>10.4f}\n'
                f'Y  : {y:>10.4f}\n'
                f'Z  : {z:>10.4f}\n'
                + '─' * 22 + '\n'
                + '\n'.join(scalar_lines)
                + '\n' + '─' * 22 + '\n'
                f'Active:\n  {active_val:>10.4f}')
        self._write_probe(text)
        self._coord_var.set(
            f'Node {nid}  |  '
            f'X={x:.2f}  Y={y:.2f}  Z={z:.2f}  |  '
            f'{active}={active_val:.3f}')
        self._highlight_node(pid)

    def _highlight_node(self, pid: int) -> None:
        """Render a yellow sphere at the picked node location."""
        if self._highlight_actor:
            self._renderer.RemoveActor(self._highlight_actor)
        coords = self.nodes_df.iloc[pid][['X', 'Y', 'Z']].values
        b      = self.grid.GetBounds()
        diag   = np.sqrt((b[1]-b[0])**2 + (b[3]-b[2])**2 + (b[5]-b[4])**2)
        sphere = vtk.vtkSphereSource()
        sphere.SetCenter(*coords)
        sphere.SetRadius(diag * 0.012)
        sphere.Update()
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(sphere.GetOutputPort())
        self._highlight_actor = vtk.vtkActor()
        self._highlight_actor.SetMapper(mapper)
        self._highlight_actor.GetProperty().SetColor(1.0, 0.92, 0.23)
        self._highlight_actor.GetProperty().SetOpacity(0.9)
        self._renderer.AddActor(self._highlight_actor)
        self._render_window.Render()

    def _write_probe(self, text: str) -> None:
        self._probe_text.config(state='normal')
        self._probe_text.delete('1.0', 'end')
        self._probe_text.insert('end', text)
        self._probe_text.config(state='disabled')

    # ══════════════════════════════════════════════════════════════════════════
    # ANSYS import callback  (called by ANSYSImportDialog)
    # ══════════════════════════════════════════════════════════════════════════

    def _on_ansys_import_complete(
        self,
        pairs:       list,
        base_name:   str,
        nodes_df,
        elements_df,
        results_df,
        scalar_cols: list,
        source_path: str,
        ext:         str,
    ) -> None:
        """
        Receive extracted data from ANSYSImportDialog and update the
        entire application state.

        Parameters
        ----------
        pairs       : list[(tab_suffix, DataFrame)] — raw extracted tables
        base_name   : file stem used as tab-name prefix
        nodes_df    : viewer-ready node table (None = grid-only import)
        elements_df : viewer-ready element table
        results_df  : viewer-ready results table
        scalar_cols : renderable scalar column names
        source_path : full path to the ANSYS file
        ext         : file extension (.rst / .cdb / etc.)
        """
        n_loaded = 0

        # ── Push each raw extracted table into the data-grid panel ────────────
        for suffix, df in pairs:
            # Truncate prefix to keep tab labels readable
            tab_name = f'{base_name[:12]}_{suffix}'
            self._add_imported_tab(tab_name, df)
            n_loaded += 1

        # ── Update the 3D viewer if geometry was extracted ────────────────────
        if (nodes_df is not None
                and elements_df is not None
                and results_df  is not None
                and scalar_cols):

            self.nodes_df     = nodes_df
            self.elements_df  = elements_df
            self.results_df   = results_df
            self._scalar_cols = scalar_cols
            self._source_label = Path(source_path).name

            # Refresh the three built-in sheet frames
            for name, df in [('Nodes',    self.nodes_df),
                              ('Elements', self.elements_df),
                              ('Results',  self.results_df)]:
                self._populate_sheet_frame(self._sheet_frames[name], df)

            # Rebuild the VTK UnstructuredGrid
            if VTK_AVAILABLE:
                try:
                    self.grid = build_vtk_unstructured_grid(
                        self.nodes_df, self.elements_df,
                        self.results_df, self._scalar_cols)
                except Exception as exc:
                    messagebox.showerror('VTK Grid Error',
                        f'Failed to build VTK mesh:\n{exc}')
                    return

            # Update sidebar controls
            self.active_result.set(scalar_cols[0])
            self._result_cb.configure(values=scalar_cols)
            self._update_scalar_range_vars()
            self._refresh_stats_card()
            self._source_lbl_var.set(
                f'{Path(source_path).name}  ·  '
                f'{len(nodes_df):,} nodes  '
                f'{len(elements_df):,} elements  '
                f'{len(scalar_cols)} scalars')

            # Force a fresh render (camera reset on first ANSYS load)
            if VTK_AVAILABLE:
                self._actor = None   # triggers ResetCamera in _render_mesh
                self._render_mesh()

        self._set_status(
            f'⚙ ANSYS import complete — '
            f'{n_loaded} tab(s) from {Path(source_path).name}')

    # ══════════════════════════════════════════════════════════════════════════
    # Event callbacks
    # ══════════════════════════════════════════════════════════════════════════

    def _on_result_changed(self, _event=None) -> None:
        self._update_scalar_range_vars()
        self._render_mesh()

    def _on_colorbar_changed(self, _event=None) -> None:
        """Validate and apply new colorbar limits without resetting the camera."""
        try:
            cmin = float(self._cmin_entry.get())
            cmax = float(self._cmax_entry.get())
        except (ValueError, tk.TclError):
            self._set_status('⚠  Invalid colorbar value.', error=True)
            return
        if cmin >= cmax:
            self._set_status('⚠  Min must be strictly less than Max.',
                             error=True)
            return
        self.colorbar_min.set(cmin)
        self.colorbar_max.set(cmax)
        self._render_mesh()

    def _on_edges_toggled(self) -> None:
        self._render_mesh()

    def _on_axes_toggled(self) -> None:
        if VTK_AVAILABLE and hasattr(self, '_axes_widget'):
            self._axes_widget.SetEnabled(
                1 if self._show_axes_var.get() else 0)
            self._render_window.Render()

    def _on_picking_toggled(self) -> None:
        if not VTK_AVAILABLE:
            return
        if self._picking_var.get():
            self._enable_picking()
            self._set_status('Node picking ACTIVE — click on the mesh.')
            self._write_probe('Picking enabled.\nClick a node.')
        else:
            self._set_status('Node picking disabled.')
            self._write_probe('Picking disabled.')

    # ══════════════════════════════════════════════════════════════════════════
    # Toolbar / menu commands
    # ══════════════════════════════════════════════════════════════════════════

    def _cmd_import_ansys(self) -> None:
        if not HAS_ANSYS_READER:
            messagebox.showerror('Library required',
                'Install the ANSYS reader:\n\n'
                '  pip install ansys-mapdl-reader\n\n'
                'No ANSYS installation or licence needed.',
                parent=self.root)
            return
        ANSYSImportDialog(self.root, self)

    def _cmd_reset_camera(self) -> None:
        if VTK_AVAILABLE and self._actor:
            self._renderer.ResetCamera()
            self._render_window.Render()
            self._set_status('Camera reset to fit mesh.')

    def _cmd_toggle_edges(self) -> None:
        self._show_edges_var.set(not self._show_edges_var.get())
        self._render_mesh()

    def _cmd_toggle_axes(self) -> None:
        self._show_axes_var.set(not self._show_axes_var.get())
        self._on_axes_toggled()

    def _cmd_toggle_pick(self) -> None:
        self._picking_var.set(not self._picking_var.get())
        self._on_picking_toggled()

    def _cmd_auto_scale(self) -> None:
        self._update_scalar_range_vars()
        self._render_mesh()
        self._set_status('Colorbar auto-scaled to full data range.')

    def _cmd_load_demo(self) -> None:
        if not messagebox.askyesno('Load Demo',
                                   'Reload the synthetic demo dataset?\n'
                                   'All imported ANSYS data will be replaced.'):
            return
        self.nodes_df, self.elements_df, self.results_df = \
            generate_synthetic_fea_data()
        self._scalar_cols  = ['Von_Mises_Stress', 'Temperature']
        self._source_label = 'Synthetic (demo)'

        if VTK_AVAILABLE:
            self.grid = build_vtk_unstructured_grid(
                self.nodes_df, self.elements_df,
                self.results_df, self._scalar_cols)

        self.active_result.set(self._scalar_cols[0])
        self._result_cb.configure(values=self._scalar_cols)
        self._update_scalar_range_vars()

        for name, df in [('Nodes',    self.nodes_df),
                          ('Elements', self.elements_df),
                          ('Results',  self.results_df)]:
            self._populate_sheet_frame(self._sheet_frames[name], df)

        self._refresh_stats_card()
        self._source_lbl_var.set('Synthetic (demo)')

        if VTK_AVAILABLE:
            self._actor = None   # force camera reset
            self._render_mesh()
        self._set_status('Synthetic demo dataset loaded.')

    def _cmd_about(self) -> None:
        ansys_ok = ('✔  ansys-mapdl-reader installed'
                    if HAS_ANSYS_READER
                    else '✘  pip install ansys-mapdl-reader')
        messagebox.showinfo('About FEA Post-Processor',
            'FEA Post-Processor  v2.0\n\n'
            'Python · Tkinter · VTK · PyVista\n'
            'pandas · NumPy · tksheet\n\n'
            f'ANSYS import:  {ansys_ok}\n\n'
            'Supported formats:\n'
            '  .rst / .rth  Structural / Thermal results\n'
            '  .full        Stiffness / mass matrices\n'
            '  .emat        Element matrices\n'
            '  .cdb / .dat  MAPDL archive\n\n'
            'Mouse controls (3D viewport):\n'
            '  Left-drag   Rotate\n'
            '  Right-drag  Zoom\n'
            '  Middle-drag Pan\n'
            '  Scroll      Zoom\n\n'
            'Keyboard shortcuts:\n'
            '  Ctrl+O  Import ANSYS file\n'
            '  Ctrl+R  Reset camera\n'
            '  Ctrl+E  Toggle edges\n'
            '  F5      Auto-scale colorbar')

    # ── Utilities ─────────────────────────────────────────────────────────────

    def _update_scalar_range_vars(self) -> None:
        """Synchronise colorbar spinboxes with the active scalar's data range."""
        scalar = self.active_result.get()
        if scalar in self.results_df.columns:
            col = self.results_df[scalar].dropna()
            if len(col) > 0:
                self.colorbar_min.set(round(float(col.min()), 6))
                self.colorbar_max.set(round(float(col.max()), 6))

    def _set_status(self, msg: str, error: bool = False) -> None:
        self._status_var.set(msg)


# ═══════════════════════════════════════════════════════════════════════════════
# § 8  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    if not VTK_AVAILABLE:
        print('\n[WARNING] PyVista/VTK not found — 3D viewport disabled.'
              '\n  pip install pyvista vtk\n')
    if not TKSHEET_AVAILABLE:
        print('\n[WARNING] tksheet not found — falling back to ttk.Treeview.'
              '\n  pip install tksheet\n')
    if not HAS_ANSYS_READER:
        print('\n[INFO] ansys-mapdl-reader not installed.'
              '\n  ANSYS file import disabled.'
              '\n  pip install ansys-mapdl-reader\n')

    root = tk.Tk()
    app  = FEAPostProcessor(root)   # noqa: F841

    if VTK_AVAILABLE:
        root.after(200, app._render_mesh)

    root.mainloop()


if __name__ == '__main__':
    main()
