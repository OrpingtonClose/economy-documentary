Feature: Audio Agent and Provisioner Integration

  Scenario: Audio Agent queues and Provisioner delivers TTS audio generation
    Given the GSA event store is clean
    And the Scenario Agent has delivered a script with one scene
    And the Audio Agent and Provisioner Agent are running in the VM
    And the VM Agent worker is active on port 8880
    When the Audio Agent is woken up
    Then the GSA event store should contain a queued "tts" audio job
    When the Provisioner Agent is woken up
    Then the GSA event store should contain a "vm_allocated" effect
    And the GSA event store should contain a "job_started" effect
    When the VM Agent worker completes the "tts" job with duration 6.8 seconds
    And the Audio Agent is woken up
    Then the GSA event store should contain a "duration_adjusted" effect
