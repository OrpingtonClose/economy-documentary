# Cython build configuration for the enforcement layer.
#
# These files are compiled to .so shared libraries so they cannot be
# edited at runtime. The pipeline's integrity depends on them:
#   - timeline_guardian: validates OTIO timeline per phase
#   - otio_contracts: OTIO-based contract enforcer
#   - otio_manager: OTIO timeline manager
#   - contracts: contract definitions and validation
#
# Usage:
#   python setup_enforcement.py build_ext --inplace
#
# After building, the .py source files are removed. Only the compiled
# .so files remain. The pipeline runs from bytecode that cannot be
# tampered with via the Edit tool.

from setuptools import setup, Extension
from Cython.Build import cythonize
import os

# The enforcement layer — files that must not be tamperable at runtime
ENFORCEMENT_FILES = [
    "callbacks/timeline_guardian.py",
    "strands_agents/hooks/otio_contracts.py",
    "strands_agents/otio_manager.py",
    "contracts.py",
]

extensions = []
for filepath in ENFORCEMENT_FILES:
    # Compute the module name from the file path
    parts = filepath.replace("/", os.sep).replace(".py", "").split(os.sep)
    module_name = ".".join(parts)
    extensions.append(
        Extension(module_name, [filepath])
    )

setup(
    name="documentary_enforcement",
    ext_modules=cythonize(
        extensions,
        compiler_directives={
            "language_level": "3",
            "boundscheck": False,
            "wraparound": False,
        },
    ),
)
