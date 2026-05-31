Feature: Reconciliation Recovery Integration

  Scenario: Scenario Agent heals a downstream audio reconciliation failure
    Given the GSA event store is clean
    And the Scenario Agent, Audio Agent, and Provisioner Agent are running in the VM
    And the GSA event store contains a block that has failed TTS reconciliation 5 times
    When the Audio Agent is woken up
    Then the GSA event store should contain a "reconciliation_failed" effect
    When the Scenario Agent is woken up
    Then the GSA event store should contain a rewritten "update_script" effect
    And the rewritten script block text must be adjusted semantically to slow down speech
    When the Audio Agent is woken up
    Then the GSA event store should contain a new queued "tts" job for the rewritten block
