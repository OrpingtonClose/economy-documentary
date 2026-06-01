Feature: Collaborative Cross-Agent Self-Correction Loop

  Scenario: Scenario Agent Automatically Shortens Script in Response to Audio Reconciliation Failure (UA-12-Real)
    Given the GSA event store contains a failed audio reconciliation event for a slot
    And the Scenario Agent is running on the host
    When the Scenario Agent receives a wakeup instruction
    Then the Scenario Agent should read the event log and detect the duration failure
    And it should rewrite the script block text to be shorter
    And it should append a revised "update_script" effect to the GSA
