Feature: Assembly Agent Media Integration

  Scenario: Successful ffmpeg muxing and timeline assembly (UA-14)
    Given the GSA event store has clean audio and video tracks delivered
    And standard video and audio stub files are generated in the VM
    When the Assembly Agent is woken up
    Then the GSA event store should contain a "pipeline_complete" effect
    And the generated video file must be playable and match the timeline duration
