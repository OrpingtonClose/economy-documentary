"""Strands-based reimplementation of the documentary pipeline.

See :doc:`docs/strands-migration/README.md` for the architecture. Scaffolded
under this package so the legacy ADK pipeline in :mod:`server.agents` and
:mod:`server.callbacks` can keep running unchanged while components are
migrated one-by-one behind the ``--pipeline=strands`` flag on
``run_pipeline.py``.
"""

from strands_agents._version import __version__

__all__ = ["__version__"]
