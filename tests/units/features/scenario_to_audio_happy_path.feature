Feature: Scenario-to-Audio Production Pipeline Happy Path

  Scenario: Automatically generate and reconcile audio narration from parsed screenplay script
    Given a parsed SD-JSON screenplay structure is loaded in GSA
    And the Scenario and Audio Agents are active on the host
    When the Scenario Agent processes the script and appends update_script blocks
    Then the Audio Agent detects the script blocks and queues TTS generation jobs
    And the jobs are completed and reconciled successfully matching duration targets
