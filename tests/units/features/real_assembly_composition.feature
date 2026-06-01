Feature: Real Assembly Agent Multitrack Composition

  Scenario: Assembly Agent Combines Reconciled Audio and Video into Final MP4 (UA-9-Real)
    Given the GSA event store contains completed audio and video jobs for all scenes
    And the Assembly Agent is running on the host
    When the Assembly Agent receives a wakeup instruction
    Then the Assembly Agent should merge the media tracks using the assembly tool
    And it should validate the final output duration against the combined slot targets
    And the GSA event store should contain a "pipeline_complete" effect with the output path and duration
