import pytest
from tests.runner.architecture_checker import run_agentic_architecture_test

def test_architecture_compliance():
    # Run the agentic architecture auditor to enforce paranoid test invariants.
    run_agentic_architecture_test()
