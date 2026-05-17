"""
rst_reader.py
=============
Lightweight, standalone ANSYS RST binary file reader.
Reverse-engineered from pymapdl-reader (ansys/pymapdl-reader on GitHub).

Dependencies: numpy only (no pymapdl, no pyvista, no Cython).

What it reads
-------------
- Standard file header   (Fortran record layout, item 0-11)
- Result header          (record at pointer 103 → ~80 items)
- Nodal equivalence table  (ptrNOD)
- Element equivalence table (ptrELM)
- Dataset index table      (ptrDSI) — 64-bit combined pointers
- Time / load-step values
- Geometry header          (ptrGEO)
- Node coordinates         (ptrGEO → ptrGEOM → node XYZ)
- Element connectivity     (ptrGEO → ptrGEOM → element table)
- Nodal DOF solution       (displacement / temperature per result set)
- Nodal stress             (ENS record per result set)
- Nodal elastic strain     (EEL record per result set)

RST binary format
-----------------
ANSYS RST files are written in Fortran unformatted sequential binary.
Every logical record is bracketed by a 4-byte integer giving the
byte-count of the payload (same value appears before and after).
All numerical data is little-endian (Intel / Windows default).

Pointers stored in the file are 1-based **record** offsets (×4 bytes).

Usage
-----
>>> from rst_reader import RSTReader
>>> rst = RSTReader("file.rst")
>>> print(rst.n_nodes, rst.n_elements, rst.n_results)
>>> nodes = rst.nodes           # dict  node_id → (x, y, z)
>>> nnum, disp = rst.nodal_solution(0)   # result index 0
>>> nnum, stress = rst.nodal_stress(0)   # SX SY SZ SXY SYZ SXZ
"""

import struct
import numpy as np
from pathlib import Path
from typing import Dict, Tuple, Optional

# ---------------------------------------------------------------------------
# Low-level binary helpers
# ---------------------------------------------------------------------------

HEADER_KEYS = [
    "fun12", "maxn", "numdof", "maxe", "nsets", "ptrEND", "ptrHED", "ptrNOD",
    "ptrELM", "ptrDSI", "ptrTIM", "ptrLSP", "ptrELM2", "ptrGEO", "ptrCYC",
    "CMSflg", "csEls", "units", "nSector", "csCord", "ptrEnd8", "ptrEnd81",
    "Spare1", "Spare2", "Spare3", "Spare4", "Spare5", "Spare6", "Spare7",
    "Spare8", "Spare9", "Spare10", "Spare11", "Spare12", "ptrDSIl", "ptrTIMl",
    "ptrLSPl", "ptrCYCl", "CMSkey", "ecpflg", "sizeDSI", "ptrEND2",
    "resmax", "AvailData", "ptrGEO2", "ptrMast", "ptrBC", "ptrFixDOF",
    "Spare13", "Spare14", "Spare15", "Spare16", "Spare17", "Spare18",
    "Spare19", "Spare20", "ptrOST", "Spare21", "Spare22", "Spare23",
    "Spare24", "Spare25", "Spare26", "Spare27", "Spare28", "Spare29",
    "Spare30", "Spare31", "Spare32", "Spare33", "Spare34", "Spare35",
    "Spare36", "Spare37", "Spare38", "Spare39", "Spare40",
]

GEOMETRY_HEADER_KEYS = [
    "maxn", "maxe", "mxnd", "mxel", "maxcsy", "ptrSYM", "numcsy", "ptrCSY",
    "maxsec", "secsiz", "nummat", "ptrMAT", "maxreal", "realsiz", "ptrREAL",
    "maxety", "etysiz", "numety", "ptrETY", "maxrl", "rlodsiz", "numrload",
    "ptrRLB", "SolPert", "ptrGEOM", "nMatProp",
]

# Element index table keys (position in per-element index record)
ELEMENT_INDEX_TABLE_KEYS = [
    "EMS", "ENF", "ENS", "ENG", "EGR", "EEL", "EPL", "ECR", "ETH",
    "EUL", "EFX", "ELF", "EMN", "ECD", "ENL", "EHC", "EPT", "ESF",
    "EDI", "ETB", "ECT", "EXY", "EBA", "ESV", "MNL",
]

ELEMENT_RESULT_NCOMP = {
    "ENS": 6,   # SX SY SZ SXY SYZ SXZ
    "EEL": 7,   # EPELX EPELY EPELZ EPELXY EPELYZ EPELXZ EQV
    "EPL": 7,
    "ECR": 7,
    "ETH": 8,
    "ENL": 10,
    "EDI": 7,
}

DOF_REF = {
    1: "UX", 2: "UY", 3: "UZ", 4: "ROTX", 5: "ROTY", 6: "ROTZ",
    7: "AX",  8: "AY",  9: "AZ", 10: "VX", 11: "VY", 12: "VZ",
    16: "TEMP", 17: "PRES", 18: "VOLT", 19: "MAG", 20: "ENKE",
    21: "ENDS", 30: "EMF", 31: "CURR",
}


class _FileReader:
    """
    Minimal ANSYS binary file reader.

    Supports two formats:
      - Fortran sequential: every record is [4-byte len][payload][4-byte len]
      - Blocked (raw):      the file is one flat int32 array; pointers are
                            absolute word offsets; no per-record framing.
    """

    def __init__(self, path: str):
        self._f = open(path, "rb")
        self._fsize = self._f.seek(0, 2)   # file size in bytes
        self._f.seek(0)
        # Load entire file as flat int32 for raw access (max ~2 GB files)
        self._f.seek(0)
        self._raw = np.fromfile(self._f, dtype=np.int32)
        self._f.seek(0)

    def close(self):
        self._f.close()

    def seek(self, record_ptr: int):
        """Seek to byte position record_ptr * 4."""
        self._f.seek(record_ptr * 4)

    # ------------------------------------------------------------------
    # Fortran-framed record access (traditional sequential format)
    # ------------------------------------------------------------------

    def read_record(self, ptr: int) -> np.ndarray:
        """
        Read a Fortran-wrapped record at byte offset ptr*4.
        Returns payload as int32 array.
        """
        self._f.seek(ptr * 4)
        raw_len = self._f.read(4)
        if not raw_len or len(raw_len) < 4:
            return np.array([], dtype=np.int32)
        n_bytes = struct.unpack("<I", raw_len)[0]
        if n_bytes == 0 or n_bytes > self._fsize:
            return np.array([], dtype=np.int32)
        payload = self._f.read(n_bytes)
        self._f.read(4)  # trailing length word
        return np.frombuffer(payload, dtype=np.int32).copy()

    def read_record_dp(self, ptr: int) -> np.ndarray:
        """Read a Fortran record, return as float64."""
        arr = self.read_record(ptr)
        # Ensure even number of int32s so view to float64 works
        if arr.size % 2:
            arr = arr[:-1]
        return arr.view(np.float64)

    def read_record_at_current(self) -> np.ndarray:
        """Read Fortran record from current file position."""
        raw_len = self._f.read(4)
        if not raw_len:
            return np.array([], dtype=np.int32)
        n_bytes = struct.unpack("<I", raw_len)[0]
        if n_bytes == 0 or n_bytes > self._fsize:
            return np.array([], dtype=np.int32)
        payload = self._f.read(n_bytes)
        self._f.read(4)
        return np.frombuffer(payload, dtype=np.int32).copy()

    def read_record_at_ptr(self, ptr: int) -> np.ndarray:
        """
        Read the Fortran-wrapped record whose DATA starts at word `ptr`.

        In blocked ANSYS files the result-header pointers (ptrNOD, ptrTIM,
        etc.) point directly at the first data word, so the Fortran length
        prefix sits at word ptr-1.  This method handles that convention.

        Falls back to read_record(ptr) if the prefix at ptr-1 is implausible.
        """
        if ptr <= 0:
            return np.array([], dtype=np.int32)
        # Try reading the Fortran record that starts at word (ptr - 1)
        # i.e. [len_bytes @ ptr-1] [payload starting at ptr] [len_bytes]
        self._f.seek((ptr - 1) * 4)
        raw_len_bytes = self._f.read(4)
        if not raw_len_bytes or len(raw_len_bytes) < 4:
            return self.read_record(ptr)
        n_bytes = struct.unpack("<I", raw_len_bytes)[0]
        if n_bytes == 0 or n_bytes > self._fsize:
            # fallback: ptr itself might be the Fortran length
            return self.read_record(ptr)
        payload = self._f.read(n_bytes)
        self._f.read(4)  # trailing length word
        return np.frombuffer(payload, dtype=np.int32).copy()

    def read_record_dp_at_ptr(self, ptr: int) -> np.ndarray:
        """Like read_record_at_ptr but returns float64."""
        arr = self.read_record_at_ptr(ptr)
        if arr.size % 2:
            arr = arr[:-1]
        return arr.view(np.float64) if arr.size else np.array([], dtype=np.float64)

    # ------------------------------------------------------------------
    # Raw (blocked) record access — no Fortran framing
    # ------------------------------------------------------------------

    def read_raw(self, ptr: int, count: int) -> np.ndarray:
        """
        Read `count` int32 words starting at absolute word offset `ptr`.
        No Fortran framing expected.
        """
        end = min(ptr + count, self._raw.size)
        if ptr >= self._raw.size:
            return np.array([], dtype=np.int32)
        return self._raw[ptr:end].copy()

    def read_raw_dp(self, ptr: int, count: int) -> np.ndarray:
        """
        Read `count` float64 values starting at absolute word offset `ptr`.
        Each float64 occupies 2 int32 words.
        """
        n_words = count * 2
        arr = self.read_raw(ptr, n_words)
        if arr.size % 2:
            arr = arr[:-1]
        return arr.view(np.float64) if arr.size else np.array([], dtype=np.float64)


def _parse_header(record: np.ndarray, keys: list) -> dict:
    """Map a flat int32 record to a named dict using a key list."""
    d = {}
    for i, k in enumerate(keys):
        d[k] = int(record[i]) if i < len(record) else 0
    return d


def _read_standard_header(reader: _FileReader):
    """
    Read the standard Ansys file header (always the first Fortran record).

    Returns
    -------
    header : dict
    end_word : int
        Word-pointer to the record immediately following the standard header.
    blocked : bool
        True when the file uses blocked (raw) format — all subsequent data
        is accessed as a flat int32 array with no per-record Fortran framing.
    """
    reader._f.seek(0)
    raw_len = reader._f.read(4)
    if not raw_len or len(raw_len) < 4:
        raise IOError("Cannot read file header — file may be empty or corrupt")
    n_bytes = struct.unpack("<I", raw_len)[0]
    payload = reader._f.read(n_bytes)
    reader._f.read(4)   # trailing length word
    end_byte = reader._f.tell()
    end_word = end_byte // 4

    arr = np.frombuffer(payload, dtype=np.int32).copy()
    header = {
        "file_type":   int(arr[0])  if len(arr) > 0  else 0,
        "units":       int(arr[4])  if len(arr) > 4  else 0,
        "result_type": int(arr[5])  if len(arr) > 5  else 0,
        "version":     int(arr[6])  if len(arr) > 6  else 0,
        "record_size": int(arr[11]) if len(arr) > 11 else 4096,
        "_raw":        arr,
    }

    # Detect blocked format:
    # Blocked files have a small first record (<=25 words) followed by a
    # single large block record (typically 4096 words).  The real result
    # header is embedded as raw words inside that block.
    blocked = False
    if arr.size <= 25:
        # Peek at the next record length
        next_rec_raw = reader._f.read(4)
        if next_rec_raw and len(next_rec_raw) == 4:
            next_len = struct.unpack("<I", next_rec_raw)[0]
            if next_len == 16384:   # 4096 words × 4 bytes = classic ANSYS block
                blocked = True
        reader._f.seek(end_byte)  # restore position

    return header, end_word, blocked


# ---------------------------------------------------------------------------
# Main reader class
# ---------------------------------------------------------------------------

class RSTReader:
    """
    Lightweight ANSYS RST binary reader.

    Parameters
    ----------
    filename : str or Path
        Path to the .rst file.

    Attributes
    ----------
    n_nodes     : int
    n_elements  : int
    n_results   : int
    time_values : np.ndarray  shape (n_results,)
    ls_table    : np.ndarray  shape (n_results, 3)  [loadstep, substep, cumulative]
    nodes       : dict  node_id (1-based) → np.ndarray([x, y, z])
    node_nums   : np.ndarray  sorted node numbers
    node_coords : np.ndarray  shape (n_nodes, 3) XYZ, aligned to node_nums
    """

    def __init__(self, filename):
        self._path = str(filename)
        self._fr = _FileReader(self._path)

        # 1. Standard header -- first Fortran record.
        self._std_header, std_end_word, self._blocked = _read_standard_header(self._fr)

        # 2. Result header -- location depends on format.
        self._rh = self._find_result_header(std_end_word)

        nsets  = self._rh.get("nsets",  0)
        resmax = self._rh.get("resmax", nsets) or nsets

        # 3. Nodal equivalence table  (ANSYS node numbers, 1-based)
        try:
            self._neqv = self._read_int_table(
                self._rh.get("ptrNOD", 0), self._rh.get("maxn", 0)
            ).copy()
            # Filter out garbage (only keep positive node numbers)
            self._neqv = self._neqv[self._neqv > 0]
        except Exception:
            self._neqv = np.array([], dtype=np.int32)

        # 4. Element equivalence table
        try:
            self._eeqv = self._read_int_table(
                self._rh.get("ptrELM", 0), self._rh.get("maxe", 0)
            ).copy()
            self._eeqv = self._eeqv[self._eeqv > 0]
        except Exception:
            self._eeqv = np.array([], dtype=np.int32)

        # 5. Dataset index table
        try:
            self._rpointers = self._read_dataset_index()
        except Exception:
            self._rpointers = np.zeros(nsets, dtype=np.int64)

        # 6. Time values
        try:
            tv = self._read_dp_table(self._rh.get("ptrTIM", 0), resmax)
            # Sanity check: time values should be positive finite numbers < 1e12
            if tv.size >= nsets:
                tv_slice = tv[:nsets]
                if np.isfinite(tv_slice).all() and (tv_slice >= 0).all() and (tv_slice < 1e12).all():
                    self.time_values = tv_slice.copy()
                else:
                    self.time_values = np.arange(nsets, dtype=np.float64)
            else:
                self.time_values = np.arange(nsets, dtype=np.float64)
        except Exception:
            self.time_values = np.arange(nsets, dtype=np.float64)

        # 7. Load-step table  [nsets x 3]
        try:
            rec = self._read_int_table(self._rh.get("ptrLSP", 0), resmax * 3)
            if rec.size >= 3 * resmax and resmax > 0:
                ls  = rec[0          : resmax      ][:nsets]
                ss  = rec[resmax     : 2 * resmax  ][:nsets]
                cum = rec[2 * resmax : 3 * resmax  ][:nsets]
                self.ls_table = np.column_stack([ls, ss, cum]).astype(np.int32)
            elif rec.size >= nsets * 3 and nsets > 0:
                self.ls_table = rec[: nsets * 3].reshape(-1, 3).copy()
            else:
                self.ls_table = np.zeros((nsets, 3), dtype=np.int32)
        except Exception:
            self.ls_table = np.zeros((nsets, 3), dtype=np.int32)

        # 8. Geometry header
        try:
            geo_rec = self._read_int_table(
                self._rh.get("ptrGEO", 0), len(GEOMETRY_HEADER_KEYS)
            )
            self._gh = _parse_header(geo_rec, GEOMETRY_HEADER_KEYS)
        except Exception:
            self._gh = {k: 0 for k in GEOMETRY_HEADER_KEYS}

        # 9. Node coordinates & element connectivity (lazy)
        self._nodes: Optional[Dict] = None
        self._elements: Optional[Dict] = None
        self._node_coords: Optional[np.ndarray] = None
        self._node_nums: Optional[np.ndarray] = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def n_nodes(self) -> int:
        return int(self._neqv.size)

    @property
    def n_elements(self) -> int:
        return int(self._eeqv.size)

    @property
    def n_results(self) -> int:
        """Actual number of result sets stored in the file."""
        # Use rpointers length (already sliced to nsets) as ground truth
        if hasattr(self, "_rpointers") and self._rpointers is not None:
            return int(self._rpointers.size)
        return int(self._rh["nsets"])

    @property
    def node_nums(self) -> np.ndarray:
        if self._node_nums is None:
            self._load_geometry()
        return self._node_nums

    @property
    def node_coords(self) -> np.ndarray:
        """XYZ coordinates aligned to node_nums, shape (n_nodes, 3)."""
        if self._node_coords is None:
            self._load_geometry()
        return self._node_coords

    @property
    def nodes(self) -> Dict[int, np.ndarray]:
        """Dict mapping ANSYS node number → np.array([x, y, z])."""
        if self._nodes is None:
            self._load_geometry()
        return self._nodes

    @property
    def elements(self) -> Dict[int, np.ndarray]:
        """Dict mapping ANSYS element number → np.array of node numbers."""
        if self._elements is None:
            self._load_geometry()
        return self._elements

    # ------------------------------------------------------------------
    # Geometry loading
    # ------------------------------------------------------------------

    def _load_geometry(self):
        """
        Read nodes and elements from the geometry block.

        Layout at ptrGEOM (from fdresu.inc / ANSYS programmer's manual):
          Item 0 : maxn   (max node number)
          Item 1 : numn   (number of nodes with coords)
          Item 2 : maxe   (max element number)
          Item 3 : nume   (number of elements)
          Item 4 : ptrNodl (pointer to nodal coords, relative to ptrGEOM)
          Item 5 : ptrEltl (pointer to element table, relative to ptrGEOM)
          ...
        Then at ptrNodl: records of (nodenum, x, y, z, thxy, thyz, thzx)
                         each packed as [1×int32, 6×float64] = 52 bytes
        """
        base = self._gh["ptrGEOM"]

        # Read geometry sub-header (first record at ptrGEOM)
        sub = self._fr.read_record(base)
        # sub[0]=maxn, sub[1]=numn, sub[2]=maxe, sub[3]=nume
        # sub[4]=ptrNodl (relative), sub[5]=ptrEltl (relative)
        numn = int(sub[1]) if len(sub) > 1 else 0
        nume = int(sub[3]) if len(sub) > 3 else 0
        ptr_nodl = int(sub[4]) if len(sub) > 4 else 0
        ptr_eltl = int(sub[5]) if len(sub) > 5 else 0

        # ---- Nodes ----
        self._nodes = {}
        node_list = []
        coord_list = []

        if ptr_nodl:
            abs_ptr = base + ptr_nodl
            for _ in range(numn):
                # Each node record: [int32 nodenum] then [6×float64]
                # = 4 + 48 = 52 bytes payload, wrapped in Fortran record
                rec = self._fr.read_record(abs_ptr)
                if rec.size == 0:
                    break
                node_id = int(rec[0])
                coords = rec[1:].view(np.float64)[:3]
                self._nodes[node_id] = coords.copy()
                node_list.append(node_id)
                coord_list.append(coords.copy())
                # Advance pointer: 4 (leading len) + 4 (nodenum) + 48 (6×f64) + 4 (trailing len) = 60 bytes → 15 records
                abs_ptr += 15  # 60 / 4

        if node_list:
            self._node_nums = np.array(node_list, dtype=np.int32)
            self._node_coords = np.array(coord_list, dtype=np.float64)
        else:
            # Fallback: use the nodal equivalence table
            self._node_nums = self._neqv.copy()
            self._node_coords = np.zeros((len(self._neqv), 3), dtype=np.float64)

        # ---- Elements ----
        self._elements = {}
        if ptr_eltl:
            abs_ptr = base + ptr_eltl
            for i in range(nume):
                rec = self._fr.read_record(abs_ptr)
                if rec.size == 0:
                    break
                # Element record layout (varies by element type):
                # Items 0-9  = element header (mat, type, real, etc.)
                # Items 10+  = node list (up to 20 nodes for solid elements)
                elem_id = int(self._eeqv[i]) if i < len(self._eeqv) else i + 1
                n_nodes_rec = max(0, rec.size - 10)
                self._elements[elem_id] = rec[10: 10 + n_nodes_rec].copy()
                # approximate pointer advance: (4 + rec.nbytes + 4) / 4
                abs_ptr += (8 + rec.size * 4) // 4

    # ------------------------------------------------------------------
    # Result header finding — sequential, blocked-raw, or brute-force scan
    # ------------------------------------------------------------------

    def _find_result_header(self, std_end_word: int) -> dict:
        """
        Locate the result header using multiple strategies in priority order.

        Strategy A -- sequential Fortran record (traditional format)
        Strategy B -- fixed word 103 (pymapdl-reader legacy default)
        Strategy C -- raw word scan (blocked/paged format)
        """
        def _looks_valid(rh):
            """Return True only if all critical fields look physically reasonable."""
            nsets  = rh.get("nsets",  0)
            ptrnod = rh.get("ptrNOD", 0)
            ptrgeo = rh.get("ptrGEO", 0)
            numdof = rh.get("numdof", 0)
            maxn   = rh.get("maxn",   0)
            return (
                1 <= nsets  <= 100_000
                and 2 <= numdof <= 32
                and 100 < ptrnod < 50_000_000
                and 0  < maxn   < 50_000_000
                # reject ASCII-space fill (0x20202020 = 538976288)
                and ptrnod != 538976288
                and ptrgeo != 538976288
            )

        # For blocked format jump straight to raw scan (Strategy C)
        if not self._blocked:
            # Strategy A: sequential
            rec_a = self._fr.read_record(std_end_word)
            rh_a  = _parse_header(rec_a, HEADER_KEYS)
            if _looks_valid(rh_a):
                return rh_a

            # Strategy B: fixed word 103
            rec_b = self._fr.read_record(103)
            rh_b  = _parse_header(rec_b, HEADER_KEYS)
            if _looks_valid(rh_b):
                return rh_b

        # Strategy C: raw word scan (blocked/paged format)
        # The result header is embedded as a flat array inside a big block.
        # We search the first 4096 words of the raw file for a window that
        # looks like a valid result header.
        raw = self._fr._raw
        search_limit = min(raw.size - len(HEADER_KEYS), 4096)
        for i in range(search_limit):
            chunk = raw[i: i + len(HEADER_KEYS)]
            if chunk.size < 15:
                break
            rh = _parse_header(chunk, HEADER_KEYS)
            if _looks_valid(rh):
                self._blocked = True
                if rh.get("resmax", 0) == 0:
                    rh["resmax"] = rh["nsets"]
                return rh

        # Absolute fallback
        return {k: 0 for k in HEADER_KEYS}

    # ------------------------------------------------------------------
    # Unified data read helpers (Fortran or raw depending on format)
    # ------------------------------------------------------------------

    def _read_int_table(self, ptr: int, hint_count: int = 0) -> np.ndarray:
        """
        Read an int32 table at word-pointer `ptr`.
        - Fortran mode: read_record(ptr) -- ptr points to Fortran length prefix.
        - Blocked mode: read_record_at_ptr(ptr) -- ptr points to data[0],
          length prefix is at ptr-1.
        `hint_count` is used only as a fallback if the Fortran length is missing.
        """
        if not ptr:
            return np.array([], dtype=np.int32)
        if self._blocked:
            arr = self._fr.read_record_at_ptr(ptr)
            if arr.size == 0 and hint_count > 0:
                return self._fr.read_raw(ptr, hint_count)
            return arr
        return self._fr.read_record(ptr)

    def _read_dp_table(self, ptr: int, hint_count: int = 0) -> np.ndarray:
        """
        Read a float64 table at word-pointer `ptr`.
        - Fortran mode: read_record_dp(ptr).
        - Blocked mode: read_record_dp_at_ptr(ptr) -- ptr points to data[0].
        """
        if not ptr:
            return np.array([], dtype=np.float64)
        if self._blocked:
            arr = self._fr.read_record_dp_at_ptr(ptr)
            if arr.size == 0 and hint_count > 0:
                return self._fr.read_raw_dp(ptr, hint_count)
            return arr
        return self._fr.read_record_dp(ptr)

    # ------------------------------------------------------------------
    # Dataset index
    # ------------------------------------------------------------------

    def _read_dataset_index(self) -> np.ndarray:
        """
        Read the 64-bit dataset pointer table.

        Layout at ptrDSI:
          [resmax × int32  lo-32-bits]
          [resmax × int32  hi-32-bits]
        Total = 2 × resmax int32 values.
        """
        nsets  = self._rh["nsets"]
        resmax = self._rh.get("resmax", nsets) or nsets
        ptr_dsi = self._rh["ptrDSI"]

        if not ptr_dsi:
            return np.zeros(nsets, dtype=np.int64)

        rec = self._read_int_table(ptr_dsi, resmax * 2)

        if rec.size < 2 * resmax:
            if rec.nbytes >= nsets * 8:
                arr = rec.view(np.int64)[:nsets]
                return arr.copy()
            padded = np.zeros(2 * resmax, dtype=np.int32)
            padded[:rec.size] = rec
            rec = padded

        raw0 = rec[:resmax].tobytes()
        raw1 = rec[resmax: 2 * resmax].tobytes()
        sub0 = [raw0[i*4:(i+1)*4] for i in range(nsets)]
        sub1 = [raw1[i*4:(i+1)*4] for i in range(nsets)]
        combined = b"".join(sub0[i] + sub1[i] for i in range(nsets))
        ptrs = np.frombuffer(combined, dtype=np.int64)
        return ptrs.copy()

    # ------------------------------------------------------------------
    # Per-result set reading
    # ------------------------------------------------------------------

    def _result_header(self, rnum: int) -> dict:
        """
        Read the solution data header for result set rnum (0-based).

        Located at rpointer[rnum].  The first record contains ~20 items.
        Key items:
          0  = numdof  (DOFs per node)
          1  = numnod  (nodes with results)
          4  = ptrNSL  (pointer to nodal solution)
          5  = ptrESL  (element solution table pointer)
        """
        base = int(self._rpointers[rnum])
        keys = [
            "numdof", "numnod", "nElm", "nElmVal", "ptrNSL", "ptrESL",
            "ptrNSPH", "rxtrap", "ptrEXT", "ptrEXY", "ptrEXZ",
            "ptrNSLH", "ptrEXTH", "ptrEXYH", "ptrEXZH",
            "ptrDOF", "Spare1", "Spare2", "Spare3", "Spare4",
        ]
        rec = self._read_int_table(base, len(keys))
        return {"_base": base, **_parse_header(rec, keys)}

    def _dof_ref(self, rnum: int) -> list:
        """Return list of DOF labels for result set rnum."""
        sh = self._result_header(rnum)
        base = sh["_base"]
        numdof = sh["numdof"]
        ptr_dof = sh.get("ptrDOF", 0)
        if ptr_dof:
            dof_rec = self._read_int_table(base + ptr_dof, numdof)
            return [DOF_REF.get(int(d), f"DOF{d}") for d in dof_rec[:numdof]]
        return ["UX", "UY", "UZ"][:numdof]

    def nodal_solution(self, rnum: int) -> Tuple[np.ndarray, np.ndarray]:
        """
        Read the nodal DOF solution for result set rnum.

        Returns
        -------
        node_nums : np.ndarray  int32, shape (n,)
            ANSYS node numbers with results.
        result : np.ndarray  float64, shape (n, numdof)
            One row per node, one column per DOF.
        """
        sh = self._result_header(rnum)
        base = sh["_base"]
        numdof = sh["numdof"]
        numnod = sh["numnod"]
        ptr_nsl = sh["ptrNSL"]

        if not ptr_nsl or numnod == 0 or numdof == 0:
            return np.array([], np.int32), np.zeros((0, numdof or 1))

        data = self._read_dp_table(base + ptr_nsl, numnod * numdof)

        n_vals = numnod * numdof
        if data.size < n_vals:
            n_vals = data.size
            numnod = n_vals // numdof

        result = data[:n_vals].reshape(numdof, numnod).T
        node_nums = self._neqv[:numnod].copy()
        return node_nums, result

    def nodal_stress(self, rnum: int) -> Tuple[np.ndarray, np.ndarray]:
        """
        Read nodal averaged stress for result set rnum.

        Returns
        -------
        node_nums : np.ndarray  int32
        stress    : np.ndarray  float64, shape (n, 6)
                    columns: SX  SY  SZ  SXY  SYZ  SXZ
        """
        return self._read_nodal_result(rnum, "ENS")

    def nodal_elastic_strain(self, rnum: int) -> Tuple[np.ndarray, np.ndarray]:
        """
        Read nodal elastic strain for result set rnum.

        Returns
        -------
        node_nums : np.ndarray  int32
        strain    : np.ndarray  float64, shape (n, 7)
                    columns: EPELX EPELY EPELZ EPELXY EPELYZ EPELXZ EQV
        """
        return self._read_nodal_result(rnum, "EEL")

    def _read_nodal_result(
        self, rnum: int, result_type: str
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Generic element-nodal-averaged result reader.

        The per-element index table at base + ptrESL contains one record per
        element; each record lists pointers for all result types
        (ENS, EEL, etc.).  This method collects values, sums them per node,
        and divides by the count to get nodal averages.
        """
        ncomp = ELEMENT_RESULT_NCOMP.get(result_type, 6)
        etype_idx = ELEMENT_INDEX_TABLE_KEYS.index(result_type)

        sh = self._result_header(rnum)
        base = sh["_base"]
        ptr_esl = sh["ptrESL"]
        n_elems = int(self._rh["maxe"])

        if not ptr_esl:
            nn = self.n_nodes
            return self._neqv.copy(), np.full((nn, ncomp), np.nan)

        # Accumulate nodal results by summing element contributions
        node_data: Dict[int, list] = {}

        for i, elem_id in enumerate(self._eeqv):
            # Per-element index table record at ptrESL + i
            eidx_rec = self._fr.read_record(base + ptr_esl + i)
            if eidx_rec.size <= etype_idx:
                continue
            ptr_res = int(eidx_rec[etype_idx])
            if not ptr_res:
                continue

            # Result record for this element
            res_rec = self._fr.read_record(base + ptr_esl + ptr_res)
            if res_rec.size == 0:
                continue

            res_dp = res_rec.view(np.float64)
            # Layout: ncomp values per node corner
            n_corners = res_dp.size // ncomp
            res_mat = res_dp[: n_corners * ncomp].reshape(n_corners, ncomp)

            # Get node list for this element
            elem_nodes = self.elements.get(int(elem_id), np.array([]))
            for j, nid in enumerate(elem_nodes[:n_corners]):
                nid = int(nid)
                if nid not in node_data:
                    node_data[nid] = []
                node_data[nid].append(res_mat[j])

        if not node_data:
            nn = self.n_nodes
            return self._neqv.copy(), np.full((nn, ncomp), np.nan)

        node_nums = np.array(sorted(node_data.keys()), dtype=np.int32)
        result = np.array(
            [np.mean(node_data[n], axis=0) for n in node_nums], dtype=np.float64
        )
        return node_nums, result

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def result_dof(self, rnum: int) -> list:
        """Return list of DOF labels (e.g. ['UX','UY','UZ']) for result rnum."""
        return self._dof_ref(rnum)

    def principal_stress(
        self, stress: np.ndarray
    ) -> np.ndarray:
        """
        Compute principal stresses from a (N, 6) stress array.

        Input columns : SX SY SZ SXY SYZ SXZ
        Output columns: S1 S2 S3 SINT SEQV

        Rows that contain NaN or Inf are returned as all-NaN.
        """
        n = stress.shape[0]
        out = np.full((n, 5), np.nan, dtype=np.float64)
        for i in range(n):
            row = stress[i]
            if not np.isfinite(row).all():
                continue
            sx, sy, sz, sxy, syz, sxz = row
            m = np.array([
                [sx,  sxy, sxz],
                [sxy, sy,  syz],
                [sxz, syz, sz ],
            ])
            try:
                eigs = np.sort(np.linalg.eigvalsh(m))[::-1]  # S1 >= S2 >= S3
            except np.linalg.LinAlgError:
                continue
            s1, s2, s3 = eigs
            sint = s1 - s3
            seqv = np.sqrt(0.5 * ((s1-s2)**2 + (s2-s3)**2 + (s3-s1)**2))
            out[i] = [s1, s2, s3, sint, seqv]
        return out

    def summary(self) -> str:
        """Return a human-readable summary of the RST file."""
        lines = [
            f"RST File   : {self._path}",
            f"Nodes      : {self.n_nodes}",
            f"Elements   : {self.n_elements}",
            f"Results    : {self.n_results}",
            f"nsets(hdr) : {self._rh['nsets']}",
            f"resmax(hdr): {self._rh['resmax']}",
            f"ptrNOD     : {self._rh['ptrNOD']}",
            f"ptrGEO     : {self._rh['ptrGEO']}",
            "",
            "  # | Load Step | Sub Step | Time",
            "----+-----------+----------+----------",
        ]
        nr = min(self.n_results, len(self.time_values), len(self.ls_table))
        for i in range(nr):
            ls, ss, _ = self.ls_table[i]
            t = self.time_values[i]
            lines.append(f"{i:3d} |{ls:10d} |{ss:9d} | {t:.6g}")
        return "\n".join(lines)

    def close(self):
        self._fr.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def __repr__(self):
        return (
            f"RSTReader('{Path(self._path).name}', "
            f"nodes={self.n_nodes}, elements={self.n_elements}, "
            f"results={self.n_results})"
        )


# ---------------------------------------------------------------------------
# Convenience top-level function
# ---------------------------------------------------------------------------

def read_rst(filename: str) -> RSTReader:
    """Open an ANSYS RST file and return an RSTReader instance."""
    return RSTReader(filename)


# ---------------------------------------------------------------------------
# Quick smoke-test  (python rst_reader.py  file.rst)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python rst_reader.py  <file.rst>")
        sys.exit(1)

    with read_rst(sys.argv[1]) as rst:
        print(rst.summary())
        print()

        # First result set
        rnum = 0
        dofs = rst.result_dof(rnum)
        nnum, sol = rst.nodal_solution(rnum)
        print(f"Result 0 DOFs : {dofs}")
        print(f"Nodal solution shape: {sol.shape}")
        if sol.size:
            print(f"  Node {nnum[0]}: {sol[0]}")
            print(f"  Node {nnum[-1]}: {sol[-1]}")

        nnum_s, stress = rst.nodal_stress(rnum)
        print(f"Nodal stress shape  : {stress.shape}")
        if stress.size and not np.all(np.isnan(stress)):
            ps = rst.principal_stress(stress)
            print(f"  S_EQV max = {np.nanmax(ps[:, 4]):.4g}")
