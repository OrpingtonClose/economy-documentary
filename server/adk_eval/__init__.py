"""ADK eval harness package.

Agent re-export lives in :mod:`adk_eval.agent`. It is NOT imported eagerly
here so that pytest collection (and ``adk web`` discovery of sibling
modules) is never blocked by transitive failures in the pipeline import
graph -- the ADK loader imports ``adk_eval.agent`` itself when it needs
the ``root_agent`` handle.
"""
