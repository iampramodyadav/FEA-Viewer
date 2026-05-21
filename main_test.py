"""
main_test.py — Test suite for rst_reader + rst_compat + ansys_importer pipeline
================================================================================
Tests the full stack without requiring tkinter, ANSYS, or ansys-mapdl-reader.

Run:
    python main_test.py                        # uses file.rst in same folder
    python main_test.py path/to/other.rst      # specify a file

What is tested
--------------
  BLOCK 1  rst_reader.py         — low-level binary parsing
  BLOCK 2  rst_compat.py         — mapdl-reader compatibility interface
  BLOCK 3  extract_rst()         — importer extractor function
  BLOCK 4  Data quality checks   — numeric sanity on displacements
  BLOCK 5  All result sets        — loop over every result index
  BLOCK 6  Edge cases            — bad index, context manager, repr
"""

import sys
import os
import traceback
import numpy as np

# ── Locate the RST file ────────────────────────────────────────────────────────
RST_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(__file__), "tests", "file.rst"
)
# Also try same directory as this script
if not os.path.isfile(RST_PATH):
    candidate = os.path.join(os.path.dirname(__file__), "file.rst")
    if os.path.isfile(candidate):
        RST_PATH = candidate

# ── Colour helpers (ANSI, gracefully disabled on Windows without colour support)
try:
    import ctypes
    ctypes.windll.kernel32.SetConsoleMode(
        ctypes.windll.kernel32.GetStdHandle(-11), 7)
except Exception:
    pass

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"


# ── Test runner ────────────────────────────────────────────────────────────────
_results = []

def run(label: str, fn):
    """Run fn(), record pass/fail, print result."""
    try:
        detail = fn()
        status = f"{GREEN}✓ PASS{RESET}"
        _results.append(("PASS", label))
    except AssertionError as e:
        status = f"{RED}✗ FAIL{RESET}"
        detail = f"AssertionError: {e}"
        _results.append(("FAIL", label))
    except Exception as e:
        status = f"{RED}✗ ERROR{RESET}"
        detail = f"{type(e).__name__}: {e}"
        _results.append(("ERROR", label))

    detail_str = f"  {YELLOW}→ {detail}{RESET}" if detail else ""
    print(f"  {status}  {label}")
    if detail_str and _results[-1][0] != "PASS":
        print(detail_str)
        tb = traceback.format_exc()
        if "NoneType" not in tb and "AssertionError" not in tb:
            for line in tb.strip().splitlines()[-4:]:
                print(f"    {line}")


def section(title: str):
    print(f"\n{BOLD}{CYAN}{'─'*60}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'─'*60}{RESET}")


# ══════════════════════════════════════════════════════════════════════════════
# PRE-FLIGHT
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}RST Reader Test Suite{RESET}")
print(f"File : {RST_PATH}")
print(f"Exists: {os.path.isfile(RST_PATH)}")

if not os.path.isfile(RST_PATH):
    print(f"\n{RED}✗  RST file not found.{RESET}")
    print(  "   Place file.rst in the same folder, under tests/file.rst,")
    print(  "   or pass the path as: python main_test.py <path/to/file.rst>")
    sys.exit(1)

# ── Imports ───────────────────────────────────────────────────────────────────
section("Importing modules")
try:
    from rst_reader import RSTReader, read_rst
    print(f"  {GREEN}✓{RESET} rst_reader imported")
except ImportError as e:
    print(f"  {RED}✗  Cannot import rst_reader: {e}{RESET}")
    sys.exit(1)

try:
    from rst_compat import RSTCompat, read_rst_compat
    print(f"  {GREEN}✓{RESET} rst_compat imported")
except ImportError as e:
    print(f"  {RED}✗  Cannot import rst_compat: {e}{RESET}")
    sys.exit(1)

try:
    import pandas as pd
    print(f"  {GREEN}✓{RESET} pandas imported")
except ImportError:
    print(f"  {RED}✗  pandas not installed — pip install pandas{RESET}")
    sys.exit(1)

# extract_rst without tkinter — import only the extractor function
try:
    import importlib, types
    # Stub out tkinter so ansys_importer.py can be partially imported
    _tk_stub = types.ModuleType("tkinter")
    _tk_stub.ttk = types.ModuleType("tkinter.ttk")
    _tk_stub.filedialog = types.ModuleType("tkinter.filedialog")
    _tk_stub.messagebox = types.ModuleType("tkinter.messagebox")
    for _name in ["Tk","Frame","Label","Entry","Button","Checkbutton",
                  "BooleanVar","StringVar","Text","Scrollbar","Canvas",
                  "PanedWindow","Toplevel","END"]:
        setattr(_tk_stub, _name, object)
    sys.modules.setdefault("tkinter", _tk_stub)
    sys.modules.setdefault("tkinter.ttk", _tk_stub.ttk)
    sys.modules.setdefault("tkinter.filedialog", _tk_stub.filedialog)
    sys.modules.setdefault("tkinter.messagebox", _tk_stub.messagebox)
    _tb_stub = types.ModuleType("ttkbootstrap")
    _tb_stub.Toplevel = object
    sys.modules.setdefault("ttkbootstrap", _tb_stub)

    import ansys_importer as _ai
    extract_rst = _ai.extract_rst
    _safe       = _ai._safe
    print(f"  {GREEN}✓{RESET} ansys_importer.extract_rst imported")
except Exception as e:
    print(f"  {YELLOW}⚠  ansys_importer not importable ({e}) — block 3 skipped{RESET}")
    extract_rst = None
    _safe = lambda fn, *a, **k: (fn(*a, **k) if callable(fn) else None)


# ══════════════════════════════════════════════════════════════════════════════
# BLOCK 1 — rst_reader.py
# ══════════════════════════════════════════════════════════════════════════════
section("BLOCK 1 — rst_reader.py  (low-level binary reader)")

_raw = RSTReader(RST_PATH)   # shared across block 1 tests

run("File opens without error",
    lambda: f"RSTReader at {RST_PATH}")

run("n_results > 0",
    lambda: (
        setattr(_raw, "_r", _raw.n_results) or
        None
        if _raw.n_results > 0 else (_ for _ in ()).throw(
            AssertionError(f"n_results={_raw.n_results}"))
    ))

def _t_n_results():
    assert _raw.n_results > 0, f"n_results={_raw.n_results}"
    return f"n_results={_raw.n_results}"
run("n_results > 0", _t_n_results)

def _t_n_nodes():
    assert _raw.n_nodes > 0, f"n_nodes={_raw.n_nodes}"
    return f"n_nodes={_raw.n_nodes}"
run("n_nodes > 0", _t_n_nodes)

def _t_node_nums_type():
    nn = _raw.node_nums
    assert isinstance(nn, np.ndarray), f"got {type(nn)}"
    assert nn.dtype in (np.int32, np.int64), f"dtype={nn.dtype}"
    assert len(nn) == _raw.n_nodes, f"len={len(nn)} vs n_nodes={_raw.n_nodes}"
    return f"node_nums shape={nn.shape} dtype={nn.dtype}"
run("node_nums is int array, len == n_nodes", _t_node_nums_type)

def _t_node_nums_positive():
    nn = _raw.node_nums
    assert np.all(nn > 0), f"non-positive node IDs: {nn[nn<=0]}"
    return f"all {len(nn)} node IDs positive, min={nn.min()} max={nn.max()}"
run("all node IDs are positive", _t_node_nums_positive)

def _t_ls_table():
    assert len(_raw.ls_table) == _raw.n_results, \
        f"ls_table len={len(_raw.ls_table)} != n_results={_raw.n_results}"
    for i, ls in enumerate(_raw.ls_table):
        assert "loadstep" in ls and "substep" in ls and "cumit" in ls, \
            f"result {i} missing keys: {ls}"
    return f"{len(_raw.ls_table)} entries, keys=loadstep/substep/cumit"
run("ls_table has correct length and keys", _t_ls_table)

def _t_nodal_solution_shape():
    nnum, disp = _raw.nodal_solution(0)
    assert isinstance(nnum, np.ndarray), f"nnum type={type(nnum)}"
    assert isinstance(disp, np.ndarray), f"disp type={type(disp)}"
    assert disp.ndim == 2,               f"disp.ndim={disp.ndim}, expected 2"
    assert disp.dtype == np.float64,     f"disp.dtype={disp.dtype}"
    assert len(nnum) == disp.shape[0],   f"nnum len={len(nnum)} vs disp rows={disp.shape[0]}"
    assert disp.shape[1] >= 1,           f"disp has 0 columns"
    return f"nnum={nnum.shape}, disp={disp.shape}"
run("nodal_solution(0) returns correct shapes", _t_nodal_solution_shape)

def _t_nodal_solution_finite():
    _, disp = _raw.nodal_solution(0)
    n_inf = np.sum(~np.isfinite(disp))
    assert n_inf == 0, f"{n_inf} non-finite values in disp"
    return f"all {disp.size} values finite"
run("nodal_solution(0) — all values finite", _t_nodal_solution_finite)

def _t_nodal_solution_nonzero():
    _, disp = _raw.nodal_solution(0)
    n_nz = np.count_nonzero(disp)
    assert n_nz > 0, "all displacement values are zero"
    return f"{n_nz}/{disp.size} non-zero values"
run("nodal_solution(0) — displacement not all zero", _t_nodal_solution_nonzero)

def _t_displacement_magnitude():
    nnum, disp = _raw.nodal_solution(0)
    nnum2, mag = _raw.displacement_magnitude(0)
    assert np.array_equal(nnum, nnum2), "node order mismatch"
    assert mag.shape == (len(nnum),), f"mag shape={mag.shape}"
    expected = np.sqrt((disp**2).sum(axis=1))
    np.testing.assert_allclose(mag, expected, rtol=1e-10)
    return f"max|U|={mag.max():.4f}"
run("displacement_magnitude(0) == sqrt(UX²+UY²+UZ²)", _t_displacement_magnitude)

def _t_context_manager():
    with read_rst(RST_PATH) as r:
        n = r.n_results
        assert n > 0
    return f"context manager opened and closed, n_results={n}"
run("context manager (with read_rst(...))", _t_context_manager)

def _t_repr():
    r = repr(_raw)
    assert "RSTReader" in r, f"repr missing 'RSTReader': {r}"
    assert str(_raw.n_nodes) in r
    return r
run("__repr__ contains key info", _t_repr)

def _t_summary():
    s = _raw.summary()
    assert "RST File" in s
    assert str(_raw.n_results) in s
    lines = s.splitlines()
    assert len(lines) >= _raw.n_results + 5
    return f"{len(lines)} lines"
run("summary() produces multi-line string", _t_summary)

_raw.close()


# ══════════════════════════════════════════════════════════════════════════════
# BLOCK 2 — rst_compat.py
# ══════════════════════════════════════════════════════════════════════════════
section("BLOCK 2 — rst_compat.py  (mapdl-reader compatibility layer)")

_compat = read_rst_compat(RST_PATH)

def _t_compat_repr():
    r = repr(_compat)
    assert "RSTCompat" in r
    return r
run("RSTCompat repr", _t_compat_repr)

def _t_compat_n_results():
    assert _compat.n_results > 0, f"n_results={_compat.n_results}"
    return f"n_results={_compat.n_results}"
run("n_results > 0", _t_compat_n_results)

def _t_compat_version():
    assert isinstance(_compat.version, str) and len(_compat.version) > 0
    return f"version='{_compat.version}'"
run("version is non-empty string", _t_compat_version)

def _t_compat_is_cms():
    assert isinstance(_compat.is_cms, bool)
    return f"is_cms={_compat.is_cms}"
run("is_cms is bool", _t_compat_is_cms)

def _t_compat_time_values():
    tv = _compat.time_values
    assert isinstance(tv, np.ndarray), f"type={type(tv)}"
    assert len(tv) == _compat.n_results, \
        f"len={len(tv)} vs n_results={_compat.n_results}"
    assert np.all(np.isfinite(tv)), "non-finite time values"
    return f"time_values={tv.tolist()}"
run("time_values: ndarray, len==n_results, all finite", _t_compat_time_values)

def _t_compat_available_results():
    av = _compat.available_results
    assert isinstance(av, str) and len(av) > 0
    return f"available_results: '{av[:60]}…'"
run("available_results is non-empty string", _t_compat_available_results)

def _t_compat_materials():
    assert isinstance(_compat.materials, dict)
    return f"materials dict (len={len(_compat.materials)})"
run("materials is dict", _t_compat_materials)

# mesh stub
def _t_mesh_nnum():
    nn = _compat.mesh.nnum
    assert isinstance(nn, np.ndarray)
    assert len(nn) > 0
    assert np.all(nn > 0)
    return f"mesh.nnum shape={nn.shape}, min={nn.min()}, max={nn.max()}"
run("mesh.nnum — positive int array", _t_mesh_nnum)

def _t_mesh_nodes():
    nodes = _compat.mesh.nodes
    assert isinstance(nodes, np.ndarray)
    assert nodes.shape == (_compat.mesh.n_node, 3), \
        f"shape={nodes.shape}, expected=({_compat.mesh.n_node},3)"
    assert nodes.dtype == np.float64
    return f"mesh.nodes shape={nodes.shape}"
run("mesh.nodes — (n_node, 3) float64", _t_mesh_nodes)

def _t_mesh_n_node():
    assert _compat.mesh.n_node == len(_compat.mesh.nnum)
    return f"mesh.n_node={_compat.mesh.n_node}"
run("mesh.n_node == len(mesh.nnum)", _t_mesh_n_node)

def _t_mesh_n_elem():
    assert isinstance(_compat.mesh.n_elem, int)
    assert _compat.mesh.n_elem == len(_compat.mesh.enum)
    return f"mesh.n_elem={_compat.mesh.n_elem}"
run("mesh.n_elem == len(mesh.enum)", _t_mesh_n_elem)

def _t_mesh_components():
    assert isinstance(_compat.mesh.node_components, dict)
    assert isinstance(_compat.mesh.element_components, dict)
    return "node_components and element_components are dicts"
run("mesh.node_components and element_components are dicts", _t_mesh_components)

def _t_solution_info():
    si = _compat.solution_info(0)
    assert isinstance(si, dict), f"type={type(si)}"
    for key in ("loadstep", "substep", "cumit", "time"):
        assert key in si, f"missing key '{key}'"
    return f"keys={list(si.keys())}"
run("solution_info(0) has loadstep/substep/cumit/time", _t_solution_info)

def _t_solution_info_out_of_range():
    si = _compat.solution_info(99999)
    assert si == {}, f"expected empty dict, got {si}"
    return "returns {} for out-of-range rnum"
run("solution_info(99999) returns empty dict", _t_solution_info_out_of_range)

def _t_nodal_displacement():
    nnum, disp = _compat.nodal_displacement(0)
    assert isinstance(nnum, np.ndarray)
    assert isinstance(disp, np.ndarray)
    assert disp.ndim == 2
    assert disp.shape[0] == len(nnum)
    return f"nnum={nnum.shape}, disp={disp.shape}"
run("nodal_displacement(0) shape OK", _t_nodal_displacement)

# All unavailable methods return None
_UNAVAILABLE = [
    "nodal_stress", "principal_nodal_stress", "nodal_elastic_strain",
    "nodal_plastic_strain", "nodal_thermal_strain", "nodal_temperature",
    "nodal_velocity", "nodal_acceleration", "nodal_input_force",
    "nodal_static_forces", "nodal_boundary_conditions", "element_stress",
]
def _t_unavailable():
    failed = []
    for name in _UNAVAILABLE:
        fn = getattr(_compat, name, None)
        if fn is None:
            failed.append(f"{name}: attribute missing")
            continue
        result = fn(0)
        if result is not None:
            failed.append(f"{name}: returned {result!r} instead of None")
    assert not failed, "\n    ".join(failed)
    return f"all {len(_UNAVAILABLE)} unavailable methods return None"
run("all unavailable methods return None (for _safe() compatibility)",
    _t_unavailable)

def _t_compat_context_manager():
    with read_rst_compat(RST_PATH) as r:
        nr = r.n_results
        assert nr > 0
    return f"context manager works, n_results={nr}"
run("context manager (with read_rst_compat(...))", _t_compat_context_manager)


# ══════════════════════════════════════════════════════════════════════════════
# BLOCK 3 — extract_rst()  (importer extractor)
# ══════════════════════════════════════════════════════════════════════════════
section("BLOCK 3 — extract_rst()  (ansys_importer extractor function)")

if extract_rst is None:
    print(f"  {YELLOW}⚠  Skipped — ansys_importer could not be imported{RESET}")
else:
    _rst_for_extract = read_rst_compat(RST_PATH)
    _all_rnums = list(range(_rst_for_extract.n_results))

    def _t_extract_nodes():
        pairs = extract_rst(_rst_for_extract, {"Node Coordinates"}, _all_rnums)
        assert len(pairs) == 1, f"expected 1 tab, got {len(pairs)}"
        name, df = pairs[0]
        assert "NodeID" in df.columns
        assert "X" in df.columns
        assert len(df) == _rst_for_extract.mesh.n_node
        return f"'{name}': {df.shape[0]} rows, cols={list(df.columns)}"
    run("Node Coordinates tab shape & columns", _t_extract_nodes)

    def _t_extract_solution_summary():
        pairs = extract_rst(_rst_for_extract, {"Solution Summary"}, _all_rnums)
        assert len(pairs) == 1
        name, df = pairs[0]
        assert "ResultIndex" in df.columns
        assert len(df) == _rst_for_extract.n_results
        assert "loadstep" in df.columns or "time" in df.columns
        return f"'{name}': {df.shape[0]} rows"
    run("Solution Summary tab — one row per result set", _t_extract_solution_summary)

    def _t_extract_displacement():
        pairs = extract_rst(_rst_for_extract, {"Nodal Displacement"}, _all_rnums)
        assert len(pairs) == 1
        name, df = pairs[0]
        assert "ResultIndex" in df.columns
        assert "NodeID" in df.columns
        assert "UX" in df.columns
        expected_rows = _rst_for_extract.n_results * _rst_for_extract.mesh.n_node
        assert len(df) == expected_rows, \
            f"expected {expected_rows} rows (results×nodes), got {len(df)}"
        assert df["UX"].dtype == np.float64
        return f"'{name}': {df.shape} — {_rst_for_extract.n_results}×{_rst_for_extract.mesh.n_node}"
    run("Nodal Displacement tab — n_results × n_nodes rows", _t_extract_displacement)

    def _t_extract_single_result():
        pairs = extract_rst(_rst_for_extract, {"Nodal Displacement"}, [0])
        name, df = pairs[0]
        assert len(df) == _rst_for_extract.mesh.n_node
        assert all(df["ResultIndex"] == 0)
        return f"single result: {len(df)} rows, all ResultIndex==0"
    run("Single result set extraction (rnum=0 only)", _t_extract_single_result)

    def _t_extract_unavailable_skipped():
        # Stress is unavailable in our reader — should produce 0 tabs
        pairs = extract_rst(_rst_for_extract,
                            {"Nodal Stress", "Element Stress"}, _all_rnums)
        assert len(pairs) == 0, \
            f"expected 0 tabs for unavailable results, got {len(pairs)}"
        return "unavailable results produce 0 tabs (silently skipped via _safe)"
    run("Unavailable results produce 0 tabs (not an error)", _t_extract_unavailable_skipped)

    def _t_extract_all_selected():
        # Note: "Element Connectivity" is excluded because mesh.elem is empty
        # for RST-only files (connectivity lives in CDB/FULL, not RST result files).
        all_sel = {
            "Node Coordinates", "Solution Summary", "Nodal Displacement",
            "Nodal Stress", "Element Stress", "Node Components",
            "Materials", "Nodal Elastic Strain",
        }
        pairs = extract_rst(_rst_for_extract, all_sel, _all_rnums)
        names = [p[0] for p in pairs]
        # At minimum: Nodes, SolnSummary, Nodal_Displacement
        assert len(pairs) >= 2, f"expected ≥2 tabs, got {len(pairs)}: {names}"
        return f"{len(pairs)} tab(s): {names}"
    run("Full selection set (no Element Connectivity) — ≥2 tabs produced",
        _t_extract_all_selected)

    def _t_extract_df_dtypes():
        pairs = extract_rst(_rst_for_extract, {"Nodal Displacement"}, _all_rnums)
        _, df = pairs[0]
        assert df["NodeID"].dtype in (np.int32, np.int64, int, object,
                                       np.dtype("int64"), np.dtype("int32"))
        for col in ["UX","UY","UZ"]:
            if col in df.columns:
                assert df[col].dtype == np.float64, \
                    f"{col}.dtype={df[col].dtype}"
        return f"dtypes OK: NodeID={df['NodeID'].dtype} UX={df['UX'].dtype}"
    run("DataFrame column dtypes correct", _t_extract_df_dtypes)


# ══════════════════════════════════════════════════════════════════════════════
# BLOCK 4 — Data quality
# ══════════════════════════════════════════════════════════════════════════════
section("BLOCK 4 — Data quality checks")

_r = read_rst_compat(RST_PATH)

def _t_displacement_finite():
    for rn in range(_r.n_results):
        _, disp = _r.nodal_displacement(rn)
        n_bad = np.sum(~np.isfinite(disp))
        assert n_bad == 0, f"result {rn}: {n_bad} non-finite displacement values"
    return f"all {_r.n_results} result sets: 100% finite"
run("All result sets — zero non-finite displacement values", _t_displacement_finite)

def _t_displacement_range():
    max_mag = 0.0
    for rn in range(_r.n_results):
        _, disp = _r.nodal_displacement(rn)
        mag = np.sqrt((disp**2).sum(axis=1)).max()
        max_mag = max(max_mag, mag)
    assert max_mag > 0,       "max displacement magnitude is exactly 0"
    assert max_mag < 1e9,     f"max displacement suspiciously large: {max_mag:.3g}"
    return f"max |U| across all results = {max_mag:.4f}"
run("Max displacement magnitude in (0, 1e9)", _t_displacement_range)

def _t_results_differ():
    if _r.n_results < 2:
        return "only 1 result — skipped"
    _, d0 = _r.nodal_displacement(0)
    _, d1 = _r.nodal_displacement(_r.n_results - 1)
    assert not np.allclose(d0, d1, atol=1e-12), \
        "first and last result sets are identical — likely a parsing error"
    delta = np.abs(d0 - d1).max()
    return f"max delta between result 0 and last = {delta:.4f}"
run("First and last result sets differ", _t_results_differ)

def _t_node_nums_consistent():
    # Node order should be consistent across all result sets
    nn0, _ = _r.nodal_displacement(0)
    for rn in range(1, _r.n_results):
        nn, _ = _r.nodal_displacement(rn)
        assert np.array_equal(nn, nn0), \
            f"node order changed between result 0 and {rn}"
    return f"node order consistent across {_r.n_results} result sets"
run("Node order consistent across all result sets", _t_node_nums_consistent)

def _t_ls_table_monotonic():
    # Access ls_table via the underlying RSTReader (RSTCompat exposes it through solution_info)
    ls_data = _r._reader.ls_table
    cumits = [ls["cumit"] for ls in ls_data]
    assert cumits == sorted(cumits), \
        f"cumulative iterations not monotonic: {cumits}"
    return f"cumit values: {cumits}"
run("Cumulative iterations are monotonically non-decreasing", _t_ls_table_monotonic)


# ══════════════════════════════════════════════════════════════════════════════
# BLOCK 5 — All result sets loop
# ══════════════════════════════════════════════════════════════════════════════
section("BLOCK 5 — All result sets")

print(f"\n  {'rnum':>4}  {'LS':>4}  {'SS':>4}  {'cumit':>6}  {'n_nodes':>7}  "
      f"{'max|U|':>10}  {'UX range':>22}  status")
print(f"  {'─'*4}  {'─'*4}  {'─'*4}  {'─'*6}  {'─'*7}  {'─'*10}  {'─'*22}  {'─'*6}")

all_ok = True
for rn in range(_r.n_results):
    try:
        nnum, disp = _r.nodal_displacement(rn)
        ls         = _r._reader.ls_table[rn]
        mag        = np.sqrt((disp**2).sum(axis=1))
        ux_range   = f"[{disp[:,0].min():.2f}, {disp[:,0].max():.2f}]"
        status     = f"{GREEN}OK{RESET}"
    except Exception as e:
        status = f"{RED}ERR: {e}{RESET}"
        all_ok = False
        nnum, disp, mag, ux_range = [], None, [0], "—"
        ls = {"loadstep":0,"substep":0,"cumit":0}

    print(f"  {rn:>4}  {ls['loadstep']:>4}  {ls['substep']:>4}  "
          f"{ls['cumit']:>6}  {len(nnum):>7}  "
          f"{max(mag) if len(mag) else 0:>10.4f}  {ux_range:>22}  {status}")

_results.append(("PASS" if all_ok else "FAIL",
                 "All result set reads succeeded"))


# ══════════════════════════════════════════════════════════════════════════════
# BLOCK 6 — Edge cases
# ══════════════════════════════════════════════════════════════════════════════
section("BLOCK 6 — Edge cases")

def _t_bad_rnum_raises():
    try:
        _r.nodal_displacement(-1)
        raise AssertionError("Expected IndexError for rnum=-1")
    except IndexError:
        pass
    try:
        _r.nodal_displacement(99999)
        raise AssertionError("Expected IndexError for rnum=99999")
    except IndexError:
        pass
    return "IndexError raised for rnum=-1 and rnum=99999"
run("nodal_displacement with out-of-range index raises IndexError", _t_bad_rnum_raises)

def _t_bad_file():
    try:
        _ = RSTReader("/nonexistent/path/fake.rst")
        raise AssertionError("Expected FileNotFoundError or similar")
    except (FileNotFoundError, OSError, IOError):
        pass
    return "raises OSError/FileNotFoundError for nonexistent file"
run("RSTReader with nonexistent file raises OS error", _t_bad_file)

def _t_nodal_solution_all():
    reader = RSTReader(RST_PATH)
    nn, all_disp = reader.nodal_solution_all()
    assert all_disp.ndim == 3, f"expected 3D, got {all_disp.ndim}D"
    assert all_disp.shape[0] == reader.n_results
    assert all_disp.shape[1] == reader.n_nodes
    assert all_disp.shape[2] >= 1
    reader.close()
    return f"shape={all_disp.shape}  (n_results × n_nodes × n_dof)"
run("nodal_solution_all() returns (n_results, n_nodes, n_dof) array",
    _t_nodal_solution_all)

def _t_principal_stress_shape():
    _, disp = _r.nodal_displacement(0)
    # principal_stress expects (N,6) — pass a synthetic stress array
    reader = RSTReader(RST_PATH)
    n = reader.n_nodes
    fake_stress = np.random.randn(n, 6) * 100
    ps = reader.principal_stress(fake_stress)
    assert ps.shape == (n, 5), f"expected ({n},5), got {ps.shape}"
    assert np.all(ps[:, 0] >= ps[:, 1]), "S1 < S2 somewhere"
    assert np.all(ps[:, 1] >= ps[:, 2]), "S2 < S3 somewhere"
    assert np.all(ps[:, 4] >= 0),        "SEQV < 0 somewhere"
    reader.close()
    return f"principal_stress → shape={ps.shape}, SEQV range=[{ps[:,4].min():.2f},{ps[:,4].max():.2f}]"
run("principal_stress(synthetic) → (N,5), S1≥S2≥S3, SEQV≥0",
    _t_principal_stress_shape)

def _t_rst_reader_summary_has_all_sets():
    reader = RSTReader(RST_PATH)
    s = reader.summary()
    for rn in range(reader.n_results):
        assert str(rn) in s, f"result index {rn} missing from summary"
    reader.close()
    return f"all {reader.n_results} result indices present in summary"
run("summary() contains every result index", _t_rst_reader_summary_has_all_sets)


# ══════════════════════════════════════════════════════════════════════════════
# FINAL REPORT
# ══════════════════════════════════════════════════════════════════════════════
section("Summary")

n_pass  = sum(1 for s,_ in _results if s == "PASS")
n_fail  = sum(1 for s,_ in _results if s == "FAIL")
n_error = sum(1 for s,_ in _results if s == "ERROR")
n_total = len(_results)

print(f"\n  Total : {n_total}")
print(f"  {GREEN}Pass  : {n_pass}{RESET}")
if n_fail:
    print(f"  {RED}Fail  : {n_fail}{RESET}")
    for s, label in _results:
        if s == "FAIL":
            print(f"    {RED}✗{RESET} {label}")
if n_error:
    print(f"  {RED}Error : {n_error}{RESET}")
    for s, label in _results:
        if s == "ERROR":
            print(f"    {RED}!{RESET} {label}")

print()
if n_fail == 0 and n_error == 0:
    print(f"  {GREEN}{BOLD}✓  All tests passed.{RESET}")
else:
    print(f"  {RED}{BOLD}✗  {n_fail + n_error} test(s) failed.{RESET}")
print()

sys.exit(0 if (n_fail == 0 and n_error == 0) else 1)



# from rst_compat import read_rst_compat
# rst = read_rst_compat('file.rst')

# from ansys.mapdl.reader import read_binary as _mapdl_read_binary
# rst = _mapdl_read_binary('file.rst')

# from rst_compat import read_rst_compat
# rst = read_rst_compat('file.rst')

# print('-----------available_results---------------')
# print(rst.available_results)
# print('------------n_node--------------')
# print(rst.mesh.n_node)
# print('-----------n_elem---------------')
# print(rst.mesh.n_elem)
# print('-----------nodes---------------')
# print(rst.mesh.nodes[0:10])
# print('-----------elem---------------')
# print(rst.mesh.elem[0:10])
# print('----------enum----------------')
# print(rst.mesh.enum[0:10])
# print('-----------nodal_stress---------------')
# print(rst.nodal_stress(1))
# print('------------principal_nodal_stress--------------')
# print(rst.principal_nodal_stress(1))
# print('-------------nodal_elastic_strain-------------')
# print(rst.nodal_elastic_strain(1))