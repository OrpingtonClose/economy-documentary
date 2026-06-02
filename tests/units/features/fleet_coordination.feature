Feature: Multi-VM Job Dispatch & Fleet Coordination

  Scenario: Provisioner coordinates multiple worker instances and dispatches jobs
    Given the job queue contains 50 pending audio and video rendering tasks
    When the Provisioner registers multiple active worker VM instances
    And initiates parallel job claiming across the active fleet
    Then distinct jobs are routed to distinct worker VMs based on capability matches
    And the event store logs each job's completion with its handling VM instance ID
