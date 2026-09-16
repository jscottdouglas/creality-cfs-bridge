"""`python -m cfsbridge`, and the entry point PyInstaller freezes.

The import is absolute rather than relative on purpose. PyInstaller runs the
analysed script as a top-level `__main__` with no parent package, so
`from .cli import main` raises "attempted relative import with no known parent
package" inside the frozen exe. `import cfsbridge.cli` works both ways: frozen,
the package is in the archive; from a checkout, `python -m cfsbridge` has
already put the project root on sys.path to find the package at all.
"""
import sys

from cfsbridge.cli import main

if __name__ == "__main__":
    sys.exit(main())
