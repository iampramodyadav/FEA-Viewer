"""
launch.py — FEA Post-Processor Guided Launcher
================================================
Performs a dependency pre-flight check before starting the application.
Provides clear install instructions for any missing package.

Run:
    python launch.py
"""

import sys
import importlib
from typing import NamedTuple


# ── Dependency manifest ────────────────────────────────────────────────────

class Dep(NamedTuple):
    import_name: str          # name used in import statement
    pip_name:    str          # name used in pip install
    required:    bool         # if True, abort on missing
    min_version: str | None   # optional minimum version string


DEPENDENCIES: list[Dep] = [
    Dep('numpy',   'numpy',   True,  '1.24'),
    Dep('pandas',  'pandas',  True,  '2.0'),
    Dep('vtk',     'vtk',     False, '9.2'),
    Dep('pyvista', 'pyvista', False, '0.43'),
    Dep('tksheet', 'tksheet', False, '7.0'),
]

# ANSI colour codes (stripped on Windows without colorama)
try:
    import colorama; colorama.init()
    GRN = '\033[92m'; RED = '\033[91m'; YLW = '\033[93m'
    CYN = '\033[96m'; RST = '\033[0m';  BLD = '\033[1m'
except ImportError:
    GRN = RED = YLW = CYN = RST = BLD = ''


# ── Version comparison helper ─────────────────────────────────────────────

def _version_ok(module, min_ver: str) -> bool:
    """Return True if the installed module meets the minimum version."""
    ver = getattr(module, '__version__', None)
    if ver is None:
        return True   # can't tell — assume ok
    try:
        from packaging.version import Version
        return Version(ver) >= Version(min_ver)
    except Exception:
        # packaging not available — do a naive string compare
        return ver >= min_ver


# ── Pre-flight check ──────────────────────────────────────────────────────

def preflight() -> bool:
    """
    Check all dependencies.

    Returns
    -------
    bool
        True if all *required* dependencies are satisfied and the app
        can start; False otherwise.
    """
    print(f'\n{BLD}{CYN}FEA Post-Processor — Dependency Check{RST}')
    print('─' * 44)

    missing_required  = []
    missing_optional  = []
    version_warnings  = []

    for dep in DEPENDENCIES:
        try:
            mod = importlib.import_module(dep.import_name)
            ver = getattr(mod, '__version__', '?')
            ok  = True
            if dep.min_version and ver != '?':
                ok = _version_ok(mod, dep.min_version)

            tag   = f'{GRN}✓{RST}' if ok else f'{YLW}⚠{RST}'
            label = 'REQUIRED' if dep.required else 'optional'
            print(f'  {tag}  {dep.import_name:<12} {ver:<10} [{label}]')

            if not ok:
                version_warnings.append(dep)

        except ImportError:
            tag   = f'{RED}✗{RST}'
            label = 'REQUIRED' if dep.required else 'optional'
            print(f'  {tag}  {dep.import_name:<12} {"NOT FOUND":<10} [{label}]')
            if dep.required:
                missing_required.append(dep)
            else:
                missing_optional.append(dep)

    print('─' * 44)

    # ── Version warnings ──────────────────────────────────────────────────
    if version_warnings:
        print(f'\n{YLW}Version warnings:{RST}')
        for dep in version_warnings:
            print(f'  {dep.import_name} should be ≥ {dep.min_version}')
            print(f'    pip install --upgrade {dep.pip_name}')

    # ── Missing optional ──────────────────────────────────────────────────
    if missing_optional:
        pkgs = ' '.join(d.pip_name for d in missing_optional)
        print(f'\n{YLW}Optional packages missing (degraded mode):{RST}')
        for dep in missing_optional:
            if dep.import_name in ('vtk', 'pyvista'):
                print(f'  • {dep.import_name}: 3D viewport will be disabled.')
            elif dep.import_name == 'tksheet':
                print(f'  • tksheet: data grid falls back to ttk.Treeview.')
        print(f'\n  Install all optional packages:\n'
              f'  {CYN}pip install {pkgs}{RST}')

    # ── Missing required ──────────────────────────────────────────────────
    if missing_required:
        pkgs = ' '.join(d.pip_name for d in missing_required)
        print(f'\n{RED}{BLD}Required packages missing — cannot start:{RST}')
        for dep in missing_required:
            print(f'  • {dep.import_name}')
        print(f'\n  Install with:\n  {CYN}pip install {pkgs}{RST}\n')
        return False

    print(f'\n{GRN}{BLD}All required dependencies satisfied.{RST}')
    return True


# ── Python version guard ──────────────────────────────────────────────────

def check_python_version() -> bool:
    major, minor = sys.version_info[:2]
    ver_str = f'{major}.{minor}.{sys.version_info[2]}'
    if (major, minor) < (3, 10):
        print(f'{RED}Python {ver_str} detected.  '
              f'Python ≥ 3.10 is required.{RST}')
        return False
    print(f'  {GRN}✓{RST}  Python {ver_str}')
    return True


# ── Main ──────────────────────────────────────────────────────────────────

def main() -> None:
    print(f'\n{BLD}{'═' * 44}{RST}')
    print(f'{BLD}  FEA Post-Processor  v1.0{RST}')
    print(f'{BLD}{'═' * 44}{RST}')

    py_ok  = check_python_version()
    dep_ok = preflight()

    if not py_ok or not dep_ok:
        sys.exit(1)

    print(f'\n{CYN}Starting application…{RST}\n')

    # ── Launch ────────────────────────────────────────────────────────────
    try:
        import tkinter as tk
        from fea_postprocessor import FEAPostProcessor

        root = tk.Tk()
        app  = FEAPostProcessor(root)   # noqa: F841

        # Give Tk time to map the window before the first render
        try:
            import vtk  # noqa: F401 — only if available
            root.after(250, app._render_mesh)
        except ImportError:
            pass

        root.mainloop()

    except Exception as exc:
        print(f'\n{RED}Fatal error during startup:{RST}')
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
