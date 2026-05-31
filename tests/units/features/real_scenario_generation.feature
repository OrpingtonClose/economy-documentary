Feature: Scenario Agent Script Writing and Timeline Generation

  Scenario: Script Generation and OTIO Update for Unfilled Slots (UA-2-Real)
    Given the GSA event store is clean
    And the Scenario Agent is running on the host
    When the Scenario Agent receives an instruction to "Write a 3-scene documentary about economic growth."
    Then the GSA event store should contain an "update_script" effect with the generated text
    And the scenario script must contain valid dialogue and visual prompts for all 3 slots
    And the OTIO timeline in the GSA should be updated with the script blocks
