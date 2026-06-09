from pathlib import Path

target_file = Path('/Users/orpington/Documents/economy-documentary-work/tests/conftest.py')

content = """import os
import pytest

def pytest_configure(config):
    # Register custom markers to avoid warnings
    config.addinivalue_line("markers", "architecture: mark a test as an architecture compliance test")
    config.addinivalue_line("markers", "covering: mark a test as a covering test (simulation cover)")
    config.addinivalue_line("markers", "simulation: mark a test as a simulation test")

def pytest_collection_modifyitems(config, items):
    arch_items = []
    cov_items = []
    sim_items = []
    other_items = []
    for item in items:
        filename = os.path.basename(item.fspath or "")
        if filename.startswith("test_architecture_"):
            item.add_marker(pytest.mark.architecture)
            arch_items.append(item)
        elif filename.startswith("test_covering_"):
            item.add_marker(pytest.mark.covering)
            cov_items.append(item)
        elif filename.startswith("test_simulation_"):
            item.add_marker(pytest.mark.simulation)
            sim_items.append(item)
        else:
            other_items.append(item)
            
    # Reorder items: architecture first, then covering, then simulation
    items[:] = arch_items + cov_items + sim_items + other_items

def pytest_runtest_makereport(item, call):
    if call.excinfo is not None:
        if item.get_closest_marker("architecture"):
            pytest.exit("Architecture test failed! Aborting the entire test suite immediately.", returncode=1)
"""

target_file.write_text(content, encoding='utf-8')
print("Successfully wrote updated conftest.py")
