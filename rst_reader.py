"""
rst_reader.py  —  Lightweight ANSYS RST binary reader
======================================================
Requires: numpy  +  ansys-mapdl-reader (pip install ansys-mapdl-reader)

The heavy Cython extensions in ansys-mapdl-reader are small (~2 MB) and
pip-installable without ANSYS; they handle the low-level binary I/O.
This module wraps them in a clean, minimal API.

Ground-truth format notes
--------------------------
* Standard header  : 100 raw i32 at byte 0 (no Fortran wrapper).
* Result header    : Fortran record at word 103 (byte 412) → 80 i32.
                     Key fields (0-based, fun12 is index 0):
                       [2] nnod, [6] nelm, [8] nsets, [9] numdof
                       [10] ptrDSIl, [11] ptrTIMl, [12] ptrLSPl
                       [13] ptrELMl, [14] ptrNODl, [15] ptrGEOl
* Geometry header  : Fortran record at ptrGEOl → geometry_header_keys.
                     ptrLOCl → per-node coord records (via load_nodes).
                     ptrEIDl → element connectivity (via load_elements).
* Dataset index    : Fortran record at ptrDSIl → [resmax lo-words][resmax hi-words]
                     → combine into nsets × int64 result-set base pointers.
* Time values      : Fortran record at ptrTIMl → f64 array, first nsets entries.
* Solution header  : Fortran record at rpointer[i] → solution_data_header_keys.
                     Key fields: ptrNSL (disp), ptrESL (element results).
* Nodal solution   : c_read_record(rp + ptrNSL) → int32 reinterpreted as f64.
                     Shape: (nnod × numdof) column-major → reshape to (nnod, numdof).
* Element results  : ESL record → 40 int64 ptrs (one per element).
                     Each ptr → element index table (25 int32, one per result type).
                     ENS_ptr[2], EEL_ptr[5], ETH_ptr[8] → Fortran record of float32
                     corner values:  n_corners × ncomp  stored as f32.
* Nodal averaging  : element corner f32 values averaged per node (sum / count).
* Principal stress : computed from 3×3 symmetric tensor eigenvalues.
"""

import numpy as np
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ansys.mapdl.reader._binary_reader import (
    c_read_record, load_nodes, load_elements,
)
from ansys.mapdl.reader._rst_keys import (
    result_header_keys, geometry_header_keys, solution_data_header_keys,
)
from ansys.mapdl.reader.common import parse_header

# Element index table key positions (0-based)
_EITK_ENS = 2   # nodal stress         6 f32/corner
_EITK_EEL = 5   # elastic strain       7 f32/corner
_EITK_EPL = 6   # plastic strain       7 f32/corner
_EITK_ETH = 8   # thermal strain       8 f32/corner

_STRESS_COLS       = ["SX",  "SY",  "SZ",  "SXY", "SYZ", "SXZ"]
_STRAIN_COLS       = ["EPELX","EPELY","EPELZ","EPELXY","EPELYZ","EPELXZ","EQV"]
_PSTRAIN_COLS      = ["EPPLX","EPPLY","EPPLZ","EPPLXY","EPPLYZ","EPPLXZ","EQV"]
_THSTRAIN_COLS     = ["EPTHX","EPTHY","EPTHZ","EPTHXY","EPTHYZ","EPTHXZ","EQV","ESWELL"]
_PRINCIPAL_COLS    = ["S1",   "S2",   "S3",   "SINT", "SEQV"]

DOF_LABELS = {1:"UX",2:"UY",3:"UZ",4:"ROTX",5:"ROTY",6:"ROTZ",
              7:"AX",8:"AY",9:"AZ",16:"TEMP",17:"PRES",18:"VOLT"}


class RSTReader:
    """
    Lightweight ANSYS RST result file reader.

    Parameters
    ----------
    filename : str | Path

    Attributes (populated at construction)
    ----------------------------------------
    n_nodes      : int
    n_elements   : int
    n_results    : int
    numdof       : int   DOF per node
    node_nums    : int32 array (nnod,)   — ANSYS node numbers in result order
    nnum_sorted  : int32 array (nnod,)   — ANSYS node numbers sorted ascending
    nodes        : float64 (nnod,3)      — XYZ sorted by nnum_sorted
    elem         : list[int32]           — element connectivity (one array per elem)
    enum         : int32 (nelm,)         — ANSYS element numbers
    time_values  : float64 (nsets,)
    ls_table     : int32  (nsets,3)      — [loadstep, substep, cumit]
    """

    def __init__(self, filename):
        self._path = str(filename)
        self._parse()

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse(self):
        p = self._path

        # ── Result header ────────────────────────────────────────────
        rh_raw = c_read_record(p, 103, False)
        rh     = parse_header(rh_raw, result_header_keys)
        self._rh = rh

        self.n_nodes   = int(rh['nnod'])
        self.n_elements= int(rh['nelm'])
        self.numdof    = int(rh['numdof'])
        self.n_results = int(rh['nsets'])
        resmax         = int(rh['resmax'])

        # ── Geometry header ──────────────────────────────────────────
        gh_raw = c_read_record(p, rh['ptrGEOl'], False)
        gh     = parse_header(gh_raw, geometry_header_keys)
        self._gh = gh

        # ── Node coordinates (load_nodes fills nnum + xyz in one pass) ─
        _nnum  = np.empty(self.n_nodes, np.int32)
        _nodes = np.empty((self.n_nodes, 6), np.float64)
        load_nodes(p, gh['ptrLOCl'], self.n_nodes, _nodes, _nnum)
        sidx = np.argsort(_nnum)
        self.nnum_sorted = _nnum[sidx].copy()
        self.nodes       = _nodes[sidx, :3].copy()   # XYZ only

        # node_nums: result-order node IDs (for NSL/ESL alignment)
        self.node_nums = c_read_record(p, rh['ptrNODl'], False).copy()
        # map node_id → index in node_nums (result order)
        self._nidx = {int(n): i for i, n in enumerate(self.node_nums)}

        # ── Element connectivity ─────────────────────────────────────
        ptr_eid   = gh['ptrEIDl']
        e_disp    = c_read_record(p, ptr_eid, False).view(np.int64).copy()
        flat, off = load_elements(p, ptr_eid, self.n_elements, e_disp)
        self._flat_cells = flat
        self._offsets    = off
        self.elem  = [flat[off[i]: off[i+1] if i+1 < len(off) else len(flat)]
                      for i in range(self.n_elements)]
        self.enum  = np.array([e[8] for e in self.elem], np.int32)

        # ── Dataset index → result-set base word pointers (int64) ───
        dsi_raw = c_read_record(p, rh['ptrDSIl'], False)
        lo  = dsi_raw[:resmax].tobytes()
        hi  = dsi_raw[resmax:].tobytes()
        combined = b"".join(lo[i*4:(i+1)*4] + hi[i*4:(i+1)*4]
                            for i in range(self.n_results))
        self._rpointers = np.frombuffer(combined, np.int64).copy()

        # ── Time values ──────────────────────────────────────────────
        tv_raw      = c_read_record(p, rh['ptrTIMl'], False)
        self.time_values = tv_raw[:self.n_results].view(np.float64).copy()

        # ── Load-step table [nsets × 3] ──────────────────────────────
        ls_raw = c_read_record(p, rh['ptrLSPl'], False)
        self.ls_table = ls_raw[:self.n_results * 3].reshape(-1, 3).copy()

    # ------------------------------------------------------------------
    # Solution header (cached per result)
    # ------------------------------------------------------------------

    def _sh(self, rnum: int) -> dict:
        rp  = int(self._rpointers[rnum])
        raw = c_read_record(self._path, rp, False)
        return parse_header(raw, solution_data_header_keys)

    def _esl_ptrs(self, sh: dict, rp: int) -> Tuple[int, np.ndarray]:
        """Return (esl_base_word, int64 array of per-element ptrs)."""
        esl_base = rp + sh['ptrESL']
        raw      = c_read_record(self._path, esl_base, False)
        return esl_base, raw.view(np.int64).copy()

    # ------------------------------------------------------------------
    # Nodal displacement
    # ------------------------------------------------------------------

    def nodal_solution(self, rnum: int) -> Tuple[np.ndarray, np.ndarray]:
        """
        Read nodal DOF solution.

        Returns
        -------
        node_nums : int32  (n_nodes,)   ANSYS node numbers in result order
        disp      : float64 (n_nodes, numdof)
        """
        self._check(rnum)
        rp  = int(self._rpointers[rnum])
        sh  = self._sh(rnum)
        raw = c_read_record(self._path, rp + sh['ptrNSL'], False)
        n   = self.n_nodes * self.numdof
        disp = np.frombuffer(raw.tobytes()[:n * 8], np.float64).reshape(
            self.n_nodes, self.numdof).copy()
        return self.node_nums, disp

    def nodal_displacement(self, rnum: int) -> Tuple[np.ndarray, np.ndarray]:
        """Alias for nodal_solution (matches mapdl-reader API)."""
        return self.nodal_solution(rnum)

    # ------------------------------------------------------------------
    # Element-nodal results (stress, strain, …)
    # ------------------------------------------------------------------

    def _read_elem_nodal(self, rnum: int, key_idx: int,
                         ncomp: int) -> Tuple[np.ndarray, np.ndarray]:
        """
        Average element corner float32 results to nodes.

        Parameters
        ----------
        key_idx : int  position in element index table (e.g. 2=ENS, 5=EEL)
        ncomp   : int  components per corner (6 for stress, 7 for strain, …)

        Returns
        -------
        node_nums : int32  (n_nodes,)   ANSYS node numbers (sorted)
        result    : float64 (n_nodes, ncomp)  NaN where element results absent
        """
        self._check(rnum)
        rp  = int(self._rpointers[rnum])
        sh  = self._sh(rnum)
        esl_base, esl_ptrs = self._esl_ptrs(sh, rp)

        s   = np.zeros((self.n_nodes, ncomp), np.float64)
        cnt = np.zeros(self.n_nodes, np.int32)

        for e in range(self.n_elements):
            eb  = esl_base + int(esl_ptrs[e])
            eit = c_read_record(self._path, eb, False)
            if eit.size <= key_idx or eit[key_idx] == 0:
                continue
            raw  = c_read_record(self._path, eb + int(eit[key_idx]), False)
            f32  = raw.view(np.float32)
            ncor = f32.size // ncomp
            if ncor == 0:
                continue
            mat  = f32[:ncor * ncomp].reshape(ncor, ncomp).astype(np.float64)
            nids = self.elem[e][10: 10 + ncor]
            for j, nid in enumerate(nids):
                nid = int(nid)
                if nid in self._nidx:
                    i = self._nidx[nid]
                    s[i]   += mat[j]
                    cnt[i] += 1

        arr          = np.full((self.n_nodes, ncomp), np.nan, np.float64)
        mask         = cnt > 0
        arr[mask]    = s[mask] / cnt[mask, None]

        # Return sorted by node number (matches mapdl-reader output)
        sidx = np.argsort(self.node_nums)
        return self.node_nums[sidx], arr[sidx]

    def nodal_stress(self, rnum: int) -> Tuple[np.ndarray, np.ndarray]:
        """Nodal averaged stress. Returns (nnum, (n,6) [SX SY SZ SXY SYZ SXZ])."""
        return self._read_elem_nodal(rnum, _EITK_ENS, 6)

    def nodal_elastic_strain(self, rnum: int) -> Tuple[np.ndarray, np.ndarray]:
        """Nodal averaged elastic strain. Returns (nnum, (n,7))."""
        return self._read_elem_nodal(rnum, _EITK_EEL, 7)

    def nodal_plastic_strain(self, rnum: int) -> Tuple[np.ndarray, np.ndarray]:
        """Nodal averaged plastic strain (7 components)."""
        return self._read_elem_nodal(rnum, _EITK_EPL, 7)

    def nodal_thermal_strain(self, rnum: int) -> Tuple[np.ndarray, np.ndarray]:
        """Nodal averaged thermal strain (8 components)."""
        return self._read_elem_nodal(rnum, _EITK_ETH, 8)

    # ------------------------------------------------------------------
    # Principal stress
    # ------------------------------------------------------------------

    def principal_nodal_stress(self, rnum: int) -> Tuple[np.ndarray, np.ndarray]:
        """
        Principal stresses computed from nodal_stress.

        Returns
        -------
        nnum   : int32  (n_nodes,)
        result : float64 (n_nodes, 5)  [S1 S2 S3 SINT SEQV]
        """
        nnum, stress = self.nodal_stress(rnum)
        n   = stress.shape[0]
        out = np.full((n, 5), np.nan, np.float64)
        for i in range(n):
            if np.any(np.isnan(stress[i])):
                continue
            sx, sy, sz, sxy, syz, sxz = stress[i]
            m = np.array([[sx, sxy, sxz],
                          [sxy, sy,  syz],
                          [sxz, syz, sz ]])
            e1, e2, e3 = np.sort(np.linalg.eigvalsh(m))[::-1]
            out[i] = [e1, e2, e3, e1 - e3,
                      np.sqrt(0.5 * ((e1-e2)**2 + (e2-e3)**2 + (e3-e1)**2))]
        return nnum, out

    # ------------------------------------------------------------------
    # Solution info / metadata
    # ------------------------------------------------------------------

    def solution_info(self, rnum: int) -> dict:
        """Return metadata dict for result set rnum."""
        self._check(rnum)
        ls = self.ls_table[rnum]
        sh = self._sh(rnum)
        return {
            "loadstep":  int(ls[0]),
            "substep":   int(ls[1]),
            "cumit":     int(ls[2]),
            "time":      float(self.time_values[rnum]),
            "numdof":    self.numdof,
            "n_nodes":   self.n_nodes,
            "n_elements":self.n_elements,
        }

    def dof_labels(self, rnum: int = 0) -> List[str]:
        """Return DOF label list for result rnum (e.g. ['UX','UY','UZ'])."""
        sh = self._sh(rnum)
        dofs = sh.get('DOFS', [])
        if dofs:
            return [DOF_LABELS.get(int(d), f"DOF{d}") for d in dofs]
        return [DOF_LABELS.get(i + 1, f"DOF{i+1}") for i in range(self.numdof)]

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def summary(self) -> str:
        lines = [
            f"RST File   : {self._path}",
            f"Nodes      : {self.n_nodes}",
            f"Elements   : {self.n_elements}",
            f"DOF/node   : {self.numdof}  ({', '.join(self.dof_labels())})",
            f"Results    : {self.n_results}",
            "",
            "  # | LS | SS | cumit |      time",
            "----+----+----+-------+----------",
        ]
        for i, (ls, t) in enumerate(zip(self.ls_table, self.time_values)):
            lines.append(f"{i:3d} |{int(ls[0]):3d} |{int(ls[1]):3d} "
                         f"|{int(ls[2]):6d} | {float(t):.6g}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Context manager / helpers
    # ------------------------------------------------------------------

    def _check(self, rnum: int):
        if not (0 <= rnum < self.n_results):
            raise IndexError(
                f"Result index {rnum} out of range [0, {self.n_results})")

    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *_): self.close()
    def __repr__(self):
        return (f"RSTReader('{Path(self._path).name}', "
                f"nodes={self.n_nodes}, elements={self.n_elements}, "
                f"results={self.n_results})")


def read_rst(filename) -> RSTReader:
    """Open an ANSYS RST file and return an RSTReader."""
    return RSTReader(filename)


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "file.rst"
    with read_rst(path) as rst:
        print(rst)
        print()
        print(rst.summary())
        print()
        for rn in range(rst.n_results):
            nn, d = rst.nodal_solution(rn)
            ls    = rst.ls_table[rn]
            print(f"  Result {rn:2d}  LS={ls[0]} SS={ls[1]:2d}  "
                  f"max|U|={np.sqrt((d**2).sum(1)).max():.4f}")
        print()
        nn, s = rst.nodal_stress(1)
        ps    = rst.principal_nodal_stress(1)
        print(f"Stress result 1:")
        print(f"  Node {nn[0]}: SX={s[0,0]:.4g}  SY={s[0,1]:.4g}  SZ={s[0,2]:.4g}")
        print(f"  Node {nn[0]}: S1={ps[1][0,0]:.4g}  SEQV={ps[1][0,4]:.4g}")
