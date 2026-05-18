"""
rst_reader.py — Lightweight ANSYS RST binary reader
=====================================================
Verified against a real CMS superelement RST file.

Ground-truth binary format findings
-------------------------------------
1.  File starts with a standard Fortran record (100B in this file).
2.  Immediately follows a 16384B PAGE BLOCK (record 1).
    The 20-item result header lives at i32 offset 76 within the page payload.
    This is why pymapdl-reader uses read_record(103): byte 412 = word 103
    lands exactly on the payload of that page block.
3.  KEY FIELDS in the result header (i32 offset within page payload from offset 76):
        [0]  fun12      (-2147483648 sentinel)
        [1]  maxn       (max master node count in CMS, or max node num)
        [2]  numdof     (total interface DOF count for CMS, or dof/node)
        [3]  maxe       (number of elements)
        [4]  nsets      (pre-allocated result slots = resmax, NOT actual count)
        [9]  ptrDSI
        [10] ptrTIM     (may be stale/zero — do not rely on it)
        [11] ptrLSP     → points to the DATASET INDEX BLOCK
        [13] ptrGEO     → geometry block
        [15] CMSflg     (nonzero = CMS superelement file)
4.  ptrLSP points to: [fun12:i32] [ptr0:i32] [ptr1:i32] ... [0:i32 ...]
    Each ptrN is an ABSOLUTE WORD offset to a result-set PAGE BLOCK.
    Actual result count = number of nonzero entries after fun12.
5.  Each result-set page block (200B = 50 i32) layout:
        [2]  ptrHED     (= 40, NOT numdof)
        [11] ptrNSL     (relative to block base — nodal solution offset)
        [12] ptrESL     (relative — element solution index)
        [20] ptrOST     (relative = 3, always)
    Sub-records at (base + relative_ptr) are accessed directly.
6.  OST sub-record at (base+3): Fortran record, 10 i32 = 40B
        [0]  nDOF_total (total interface DOFs = 321 in this model)
        [2]  loadstep
        [3]  substep
        [4]  cumit
        [8]  ptrNSL_rel (relative to base)  ← same as result page header [11]
        [9]  ptrESL_rel (relative to base)
7.  NSL data block at (base + ptrNSL_rel):
        Fortran record: length = N_bytes (N = nDOF_total / numdof_per_node × 3 × 8 + 4)
        Payload: [4B zero-pad] [f64 × n_nodes × 3]
        Actual data starts at payload byte 4.
        n_nodes × 3 = (N - 4) // 8
        Node ordering = first group of node IDs from the main page block (offset 166).
8.  ESL at (base + ptrESL_rel): [fun12:i32] [(lo,hi) pairs of element result ptrs...]
    lo+hi×2^32 = absolute word offset to per-element result block.
9.  Node equivalence table: stored inside the main page block (record 1).
    At i32 offset 165 from page payload: fun12 sentinel, then 80 node IDs
    (in result order, matching the rows of the NSL displacement array).
10. CMS files (CMSflg≠0) do NOT store physical node XYZ coordinates.
    The displacement values are interface-DOF amplitudes.

Dependencies: numpy only.
"""

import struct
import numpy as np
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _frec(data: bytes, byte_off: int) -> Tuple[int, bytes]:
    """Read Fortran record at byte_off. Returns (n_bytes, payload)."""
    if byte_off + 4 > len(data):
        return 0, b""
    n = struct.unpack("<I", data[byte_off:byte_off+4])[0]
    if n == 0 or n > 20_000_000:
        return 0, b""
    end = byte_off + 4 + n
    if end > len(data):
        return 0, b""
    return n, data[byte_off+4:end]


def _i32(pay: bytes) -> np.ndarray:
    n = (len(pay) // 4) * 4
    return np.frombuffer(pay[:n], dtype=np.int32)


def _f64(pay: bytes) -> np.ndarray:
    n = (len(pay) // 8) * 8
    return np.frombuffer(pay[:n], dtype=np.float64)


# ---------------------------------------------------------------------------
# RST Reader
# ---------------------------------------------------------------------------

RESULT_HEADER_KEYS = [
    "fun12", "maxn", "numdof", "maxe", "nsets", "ptrEND", "ptrHED", "ptrNOD",
    "ptrELM", "ptrDSI", "ptrTIM", "ptrLSP", "ptrELM2", "ptrGEO", "ptrCYC",
    "CMSflg", "csEls", "units", "nSector", "csCord",
]


class RSTReader:
    """
    Lightweight ANSYS RST file reader.

    Parameters
    ----------
    filename : str | Path

    Attributes
    ----------
    n_nodes      : int   – nodes with displacement results
    n_elements   : int   – element count
    n_results    : int   – actual result sets stored
    is_cms       : bool  – True for CMS superelement files
    node_nums    : ndarray[int32] – ANSYS node numbers (result order)
    ls_table     : list[dict]  – [{loadstep, substep, cumit}, ...]
    time_values  : list[float] – time/load value per result set
    """

    def __init__(self, filename):
        self._path = str(filename)
        with open(self._path, "rb") as f:
            self._data: bytes = f.read()

        self._parse()

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse(self):
        data = self._data

        # ── 1. Standard header ──────────────────────────────────────────
        n0, _p0 = _frec(data, 0)
        std_end_byte = 4 + n0 + 4   # byte position after std header record

        # ── 2. Page block (record 1) ─────────────────────────────────────
        n1, p1 = _frec(data, std_end_byte)
        if n1 == 0:
            raise ValueError("Cannot read page block after standard header")
        page_i32 = _i32(p1)

        # Result header at i32 offset 76 within the page payload
        rh_raw = page_i32[76:76+20]
        if rh_raw.size < 20:
            raise ValueError("Page block too small — unexpected RST layout")
        self._rh: Dict[str, int] = {
            k: int(rh_raw[i]) for i, k in enumerate(RESULT_HEADER_KEYS)
        }

        # ── 3. CMS flag ──────────────────────────────────────────────────
        self.is_cms: bool = bool(self._rh.get("CMSflg", 0))

        # ── 4. Dataset index → result-set base pointers ──────────────────
        ptrLSP = self._rh["ptrLSP"]
        n_lsp, p_lsp = _frec(data, ptrLSP * 4)
        al = _i32(p_lsp)
        # Layout: [fun12, ptr0, ptr1, ..., 0, 0, ...]
        # Count non-zero entries after the fun12 sentinel
        raw_ptrs = al[1:]
        n_actual = int(np.argmax(raw_ptrs == 0))
        if n_actual == 0 and raw_ptrs.size > 0 and raw_ptrs[0] != 0:
            n_actual = int(np.sum(raw_ptrs != 0))
        self._result_ptrs: np.ndarray = raw_ptrs[:n_actual].astype(np.int64)

        # ── 5. Node equivalence table (from main page block) ─────────────
        # At page payload i32 offset 165: fun12 sentinel, then node IDs in
        # result order (matching NSL displacement rows).
        self._node_nums: np.ndarray = self._extract_node_nums(page_i32)

        # ── 6. Load-step / time table (from OST sub-records) ─────────────
        self.ls_table:    List[Dict]  = []
        self.time_values: List[float] = []
        for base in self._result_ptrs:
            ost = self._read_ost(int(base))
            self.ls_table.append({
                "loadstep": ost["loadstep"],
                "substep":  ost["substep"],
                "cumit":    ost["cumit"],
            })
            self.time_values.append(ost["time"])

    def _extract_node_nums(self, page_i32: np.ndarray) -> np.ndarray:
        """
        Extract node numbers from the main page block.

        Structure (discovered from binary analysis):
          page_i32[157] = 80   (number of nodes with results)
          page_i32[158] = 3    (number of DOF groups)
          page_i32[159] = fun12  ← small group (3 entries: 1,2,3)
          page_i32[163] = 321  (nDOF_total)
          page_i32[164] = 3    (group count)
          page_i32[165] = fun12  ← main node list follows
          page_i32[166:166+n_nodes] = node IDs in result order
        """
        sentinel = np.int32(-2147483648)

        # Read n_nodes from page_i32[157] — verified from binary
        n_nodes_candidate = int(page_i32[157]) if page_i32.size > 157 else 0

        # Find the main node list: second fun12 at offset 165
        main_list_start = 166
        if page_i32.size > main_list_start + n_nodes_candidate:
            ids = page_i32[main_list_start : main_list_start + n_nodes_candidate]
            # Validate: all should be positive integers (node IDs)
            if np.all(ids > 0) and np.all(ids < 1_000_000):
                return ids.astype(np.int32)

        # Fallback: scan for fun12 followed by a clean run of valid node IDs
        for i in range(100, min(300, page_i32.size)):
            if page_i32[i] == sentinel:
                run = []
                for j in range(i+1, min(i+400, page_i32.size)):
                    v = int(page_i32[j])
                    if 1 <= v < 1_000_000:
                        run.append(v)
                    else:
                        break
                if len(run) >= 10:
                    return np.array(run, dtype=np.int32)

        return np.arange(1, n_nodes_candidate+1, dtype=np.int32)

    def _read_ost(self, base: int) -> dict:
        """
        Read OST sub-record at (base+3).
        Returns dict: numnod, loadstep, substep, cumit, time, ptrNSL, ptrESL.
        """
        n, p = _frec(self._data, (base + 3) * 4)
        ao = _i32(p)
        if ao.size < 10:
            return dict(numnod=0, loadstep=0, substep=0, cumit=0,
                        time=0.0, ptrNSL=0, ptrESL=0)
        loadstep = int(ao[2])
        substep  = int(ao[3])
        cumit    = int(ao[4])
        ptrNSL   = int(ao[8])
        ptrESL   = int(ao[9])
        # Time value: fallback to substep; a real time f64 is not reliably stored
        time_val = float(substep)
        return dict(numnod=int(ao[0]), loadstep=loadstep, substep=substep,
                    cumit=cumit, time=time_val, ptrNSL=ptrNSL, ptrESL=ptrESL)

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def n_nodes(self) -> int:
        return len(self._node_nums)

    @property
    def n_elements(self) -> int:
        return self._rh.get("maxe", 0)

    @property
    def n_results(self) -> int:
        return len(self._result_ptrs)

    @property
    def node_nums(self) -> np.ndarray:
        return self._node_nums

    # ------------------------------------------------------------------
    # Result reading
    # ------------------------------------------------------------------

    def nodal_solution(self, rnum: int) -> Tuple[np.ndarray, np.ndarray]:
        """
        Read nodal DOF solution for result index rnum (0-based).

        Returns
        -------
        node_nums : ndarray[int32], shape (n_nodes,)
        result    : ndarray[float64], shape (n_nodes, 3)
            Columns: UX, UY, UZ  (or ROTX/ROTY/ROTZ for rotational DOF,
            depending on model configuration).
        """
        if rnum < 0 or rnum >= self.n_results:
            raise IndexError(
                f"Result index {rnum} out of range [0, {self.n_results})")

        base   = int(self._result_ptrs[rnum])
        ost    = self._read_ost(base)
        ptrNSL = ost["ptrNSL"]

        if ptrNSL == 0:
            empty = np.zeros((0, 3), dtype=np.float64)
            return self._node_nums[:0], empty

        # NSL Fortran record: [4B N_bytes] [4B zero-pad] [f64 × n_nodes × 3]
        n_nsl, p_nsl = _frec(self._data, (base + ptrNSL) * 4)
        if n_nsl == 0 or len(p_nsl) < 12:
            empty = np.zeros((0, 3), dtype=np.float64)
            return self._node_nums[:0], empty

        # Skip 4-byte zero pad at start of payload
        raw = p_nsl[4:]
        n_f64 = len(raw) // 8
        disp_flat = np.frombuffer(raw[:n_f64*8], dtype=np.float64).copy()
        n_nodes_in_block = n_f64 // 3

        disp = disp_flat[:n_nodes_in_block * 3].reshape(n_nodes_in_block, 3)
        nnum = self._node_nums[:n_nodes_in_block]
        return nnum, disp

    def nodal_solution_all(self) -> Tuple[np.ndarray, np.ndarray]:
        """
        Read all result sets' nodal DOF solutions.

        Returns
        -------
        node_nums : ndarray[int32], shape (n_nodes,)
        results   : ndarray[float64], shape (n_results, n_nodes, 3)
        """
        all_res = []
        nnum = None
        for i in range(self.n_results):
            nn, res = self.nodal_solution(i)
            if nnum is None:
                nnum = nn
            all_res.append(res)
        if not all_res:
            return np.array([], np.int32), np.zeros((0, 0, 3))
        return nnum, np.stack(all_res, axis=0)

    def displacement_magnitude(self, rnum: int) -> Tuple[np.ndarray, np.ndarray]:
        """
        Compute displacement magnitude (√(UX²+UY²+UZ²)) for each node.

        Returns
        -------
        node_nums : ndarray[int32]
        magnitude : ndarray[float64]
        """
        nnum, disp = self.nodal_solution(rnum)
        mag = np.sqrt((disp**2).sum(axis=1))
        return nnum, mag

    def principal_stress(self, stress: np.ndarray) -> np.ndarray:
        """
        Compute principal stresses from (N,6) [SX SY SZ SXY SYZ SXZ].
        Returns (N,5) [S1 S2 S3 SINT SEQV].
        """
        n = stress.shape[0]
        out = np.zeros((n, 5), dtype=np.float64)
        for i in range(n):
            sx, sy, sz, sxy, syz, sxz = stress[i]
            m = np.array([[sx, sxy, sxz],
                          [sxy, sy, syz],
                          [sxz, syz, sz]])
            eigs = np.sort(np.linalg.eigvalsh(m))[::-1]
            s1, s2, s3 = eigs
            out[i] = [s1, s2, s3, s1 - s3,
                      np.sqrt(0.5 * ((s1-s2)**2 + (s2-s3)**2 + (s3-s1)**2))]
        return out

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def summary(self) -> str:
        lines = [
            f"RST File    : {self._path}",
            f"File size   : {len(self._data):,} bytes",
            f"CMS file    : {self.is_cms}",
            f"Nodes       : {self.n_nodes}",
            f"Elements    : {self.n_elements}",
            f"Results     : {self.n_results}",
            f"CMSflg      : {self._rh.get('CMSflg', 0)}",
            f"ptrLSP      : {self._rh.get('ptrLSP', 0)}",
            f"ptrGEO      : {self._rh.get('ptrGEO', 0)}",
            "",
            "  # |  LS |  SS | cumit | time ",
            "----+-----+-----+-------+------",
        ]
        for i, ls in enumerate(self.ls_table):
            t = self.time_values[i]
            lines.append(
                f"{i:3d} | {ls['loadstep']:3d} | {ls['substep']:3d} "
                f"| {ls['cumit']:5d} | {t:.4g}"
            )
        return "\n".join(lines)

    def close(self):
        # Data is held in memory; nothing to close.
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def __repr__(self):
        return (
            f"RSTReader('{Path(self._path).name}', "
            f"nodes={self.n_nodes}, elements={self.n_elements}, "
            f"results={self.n_results}, cms={self.is_cms})"
        )


# ---------------------------------------------------------------------------
# Top-level convenience
# ---------------------------------------------------------------------------

def read_rst(filename) -> RSTReader:
    """Open an ANSYS RST file and return an RSTReader."""
    return RSTReader(filename)


# ---------------------------------------------------------------------------
# CLI smoke-test:  python rst_reader.py  file.rst
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "file.rst"
    with read_rst(path) as rst:
        print(rst)
        print()
        print(rst.summary())
        print()

        for rnum in range(rst.n_results):
            nnum, disp = rst.nodal_solution(rnum)
            _, mag = rst.displacement_magnitude(rnum)
            ls = rst.ls_table[rnum]
            print(f"Result {rnum:2d}  LS={ls['loadstep']} SS={ls['substep']:2d}  "
                  f"nodes={len(nnum)}  max|U|={mag.max():.4f}")

        print()
        nnum_all, all_disp = rst.nodal_solution_all()
        print(f"All results array: {all_disp.shape}  "
              f"(n_results × n_nodes × 3_dof)")
        print()
        print("Node mapping (first 10 nodes, result 0):")
        nnum0, d0 = rst.nodal_solution(0)
        for i in range(min(10, len(nnum0))):
            print(f"  Node {nnum0[i]:4d}:  "
                  f"UX={d0[i,0]:10.4f}  UY={d0[i,1]:10.4f}  UZ={d0[i,2]:10.4f}")
