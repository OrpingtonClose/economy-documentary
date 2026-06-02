Feature: Provisioner Happy Path Orchestration & Exponential Escalation

  Scenario: Flawless execution scaling via doubling rollout up to a soft limit of 4 VMs
    Given a pipeline queue with multiple pending rendering and tts jobs
    When the Provisioner initiates provisioning with exactly 1 VM
    Then the initial jobs are executed sequentially on the single VM
    When queue demand escalates and the Provisioner doubles the VM count to 2 VMs
    Then jobs are routed and executed in parallel across both VMs
    When queue demand continues to grow and the Provisioner doubles the VM count to the soft limit of 4 VMs
    Then jobs are successfully completed in parallel across the 4 active VM instances
