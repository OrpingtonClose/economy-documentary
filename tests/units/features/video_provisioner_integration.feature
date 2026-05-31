Feature: Video Agent and Provisioner Integration

  Scenario: Video Agent queues and Provisioner delivers LTX video generation
    Given the GSA event store is clean
    And the Scenario Agent has delivered a script with one scene
    And the Audio Agent has completed reconciliation for the scene
    And the Video Agent and Provisioner Agent are running in the VM
    And the VM Agent worker is active on port 8880
    When the Video Agent is woken up
    Then the GSA event store should contain a queued "ltx" video job
    When the Provisioner Agent is woken up
    Then the GSA event store should contain a "vm_allocated" effect
    And the GSA event store should contain a "job_started" effect
    When the VM Agent worker completes the "ltx" job
    And the Video Agent is woken up
    Then the GSA event store should contain a "merge_into_otio" effect
