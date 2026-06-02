Feature: Muxing and Timeline Composition Happy Path

  Scenario: Mux completed audio and video segments into final validated MP4
    Given the GSA contains completed rendering jobs for both audio and video blocks
    And the Assembly Agent is active on the host
    When the Assembly Agent receives the wake-up triggers
    Then it executes ffmpeg commands to mux and merge the tracks into a final output MP4
    And the output file is validated uncorrupted and matches target limits
