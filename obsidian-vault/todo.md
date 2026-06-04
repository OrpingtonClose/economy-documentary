# Pipeline Implementation TODO

## High-Priority Features

- [ ] **Implement Simulation & Shadow Pipelines**
  - **Description**: Build a generic `SimulationCapability` (subclass of `AbstractCapability`) to enable full end-to-end pipeline dry runs without renting real cloud GPU instances.
  - **Mechanism**:
    - Intercept generated events (e.g. `QueueJob`, `VMAllocated`) at the capability lifecycle/hook level.
    - Bypass actual cloud-worker VM provisioning and subprocess execution.
    - Write matching mock events (e.g., `JobCompleted` with simulated durations, `AudioMeasured` with aligned WhisperX times) directly back into the EventStore.
  - **Value**: Allows validating prompt updates, routing logic, and multi-agent coordination end-to-end in under 10 seconds.
