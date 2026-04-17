"""
ADK Environment Simulation — test scenario framework.

Replaces hand-rolled ``if _TEST_MODE`` checks with ADK-native
``EnvironmentSimulationConfig`` for hermetic, reproducible testing
and targeted failure injection at every escalation site.

Usage::

    from testing.scenarios import get_scenario
    from testing.simulation_bridge import activate_simulation

    config = get_scenario("A1_audio_duration_drift")
    activate_simulation(config)
    # Pipeline now uses simulation engine for all tool calls

    # Or via CLI:
    #   python -m testing.runner --scenario A1
"""
