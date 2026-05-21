"""
rst_compat.py — mapdl-reader compatibility wrapper for rst_reader.py
=====================================================================
Exposes an interface identical to ansys.mapdl.reader.read_binary() so that
ansys_importer.py works without modification.
"""

import numpy as np
from pathlib import Path
from typing import Optional
from rst_reader import RSTReader


class _MeshStub:
    """Mimics the mesh sub-object returned by ansys-mapdl-reader RST objects."""

    def __init__(self, rst: RSTReader):
        # Sorted node data
        self.nnum   = rst.nnum_sorted.copy()       # int32 (nnod,)
        self.nodes  = rst.nodes.copy()             # float64 (nnod, 3)  XYZ
        self.n_node = int(rst.n_nodes)

        # Element data
        self.enum   = rst.enum.copy()              # int32 (nelm,)
        self.elem   = [e.copy() for e in rst.elem] # list[int32]
        self.n_elem = int(rst.n_elements)

        # Element type key (minimal stub)
        self.etype  = np.ones(rst.n_elements, np.int32)

        # Component dicts (not in RST result files)
        self.node_components    : dict = {}
        self.element_components : dict = {}


class RSTCompat:
    """
    Drop-in replacement for the object returned by
    ansys.mapdl.reader.read_binary() for .rst / .rth files.

    All methods return (nnum, data) tuples matching the mapdl-reader convention.
    Methods for result types not stored in this file return None so that
    ansys_importer._safe() skips them silently.
    """

    def __init__(self, path: str):
        self._reader  = RSTReader(path)
        self._path    = path
        self.mesh     = _MeshStub(self._reader)

        # Scalar attributes
        self.n_results   = self._reader.n_results
        self.version     = "rst_reader (lightweight)"
        self.is_cms      = False   # standard RST

        # time_values: float64 array matching mapdl-reader
        self.time_values = self._reader.time_values.copy()

        # Build available_results string
        dofs = self._reader.dof_labels()
        avail = ("Available Results:\n"
                 f"NSL : Nodal displacements ({', '.join(dofs)})\n"
                 "ENS : Nodal stresses\n"
                 "EEL : Nodal elastic strains\n"
                 "EPL : Nodal plastic strains\n"
                 "ETH : Nodal thermal strains")
        self.available_results = avail

        # Materials — not in RST result files
        self.materials: dict = {}

    # ------------------------------------------------------------------
    # solution_info
    # ------------------------------------------------------------------

    def solution_info(self, rnum: int) -> dict:
        if rnum >= self.n_results or rnum < 0:
            return {}
        return self._reader.solution_info(rnum)

    # ------------------------------------------------------------------
    # Nodal displacement / solution
    # ------------------------------------------------------------------

    def nodal_displacement(self, rnum: int):
        """Returns (node_nums, disp_array) — UX UY UZ [ROTX ROTY ROTZ]."""
        return self._reader.nodal_solution(rnum)

    # ------------------------------------------------------------------
    # Stress / strain
    # ------------------------------------------------------------------

    def nodal_stress(self, rnum: int):
        """Returns (node_nums, stress) — SX SY SZ SXY SYZ SXZ."""
        return self._reader.nodal_stress(rnum)

    def principal_nodal_stress(self, rnum: int):
        """Returns (node_nums, principal) — S1 S2 S3 SINT SEQV."""
        return self._reader.principal_nodal_stress(rnum)

    def nodal_elastic_strain(self, rnum: int):
        """Returns (node_nums, strain) — 7 components."""
        return self._reader.nodal_elastic_strain(rnum)

    def nodal_plastic_strain(self, rnum: int):
        """Returns (node_nums, strain) — 7 components."""
        return self._reader.nodal_plastic_strain(rnum)

    def nodal_thermal_strain(self, rnum: int):
        """Returns (node_nums, strain) — 8 components."""
        return self._reader.nodal_thermal_strain(rnum)

    # ------------------------------------------------------------------
    # Not stored in RST result files → return None (importer skips via _safe)
    # ------------------------------------------------------------------

    def nodal_temperature(self, *_):           return None
    def nodal_velocity(self, *_):              return None
    def nodal_acceleration(self, *_):          return None
    def nodal_input_force(self, *_):           return None
    def nodal_static_forces(self, *_):         return None
    def nodal_boundary_conditions(self, *_):   return None
    def element_stress(self, *_):              return None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self._reader.close()

    def __repr__(self):
        return (f"RSTCompat('{Path(self._path).name}', "
                f"nodes={self._reader.n_nodes}, "
                f"elements={self._reader.n_elements}, "
                f"results={self.n_results})")


def read_rst_compat(path: str) -> RSTCompat:
    """Open an RST/RTH file and return an RSTCompat object."""
    return RSTCompat(path)
