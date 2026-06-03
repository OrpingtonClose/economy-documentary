# Implementation Plan

## Objective
Refactor existing tests to replace "localhost" with "127.0.0.1" in HTTP request destinations (URIs and worker URLs) to avoid macOS connectivity issues with Uvicorn servers bound to 127.0.0.1.

## Proposed Changes
Modify the following test files in `tests/units/`:
1. `test_agent_search_tools.py`: Change `gsa_url="http://localhost:8000/"` to `gsa_url="http://127.0.0.1:8000/"`. Avoid changing the checker line.
2. `test_hour_movie_scaffolding_bdd.py`: Replace "localhost" with "127.0.0.1" in HTTP requests and worker URLs.
3. `test_longform_readiness_bdd.py`: Replace "localhost" with "127.0.0.1" in HTTP requests.
4. `test_pipeline_faults_bdd.py`: Replace "localhost" with "127.0.0.1" in worker URLs.
5. `test_provisioning_happy_path_bdd.py`: Replace "localhost" with "127.0.0.1" in HTTP requests and worker URLs.
6. `test_real_assembly_bdd.py`: Replace "localhost" with "127.0.0.1" in HTTP requests.
7. `test_real_scenario_bdd.py`: Replace "localhost" with "127.0.0.1" in HTTP requests.
8. `test_real_self_correction_bdd.py`: Replace "localhost" with "127.0.0.1" in HTTP requests.
9. `test_real_vast_provisioning_bdd.py`: Replace "localhost" with "127.0.0.1" in HTTP requests.
10. `test_real_video_provisioner_bdd.py`: Replace "localhost" with "127.0.0.1" in HTTP requests.

## Verification Steps
1. Run `.venv/bin/pytest` and ensure all tests pass.
2. Verify that there are no remaining instances of "localhost" in `tests/` except for the allowed regex/check logic in `test_agent_search_tools.py`.
