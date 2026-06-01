Feature: Real Audio Agent Narration and Duration Reconciliation

  Scenario: Audio Agent Detects Unprocessed Narration and Reconciles Duration (UA-8-Real)
    Given the GSA event store has a written script block needing audio narration
    And the system budget has remaining funds
    And the Audio Agent is running on the host
    When the Audio Agent receives a wakeup instruction
    Then the Audio Agent should queue a "tts" job for the narration block
    And the GSA event store should contain a "queue_job" effect for the block
    When the Provisioner or test harness marks the job completed with a dummy audio artifact
    And the Audio Agent receives another wakeup instruction
    Then the Audio Agent should evaluate the generated audio duration against target tolerance
    And the GSA event store should contain a "reconciliation_complete" effect for the slot
