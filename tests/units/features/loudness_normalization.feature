Feature: Audio Loudness Normalization at Scale

  Scenario: Mux and normalize narration tracks across a long-form movie
    Given 60 audio segments with varying loudness levels and different voice roles
    When the Assembly Agent processes the final timeline mix using loudness filters
    Then the final output audio is checked using loudness analysis tools
    And the integrated loudness matches -16.0 LUFS +/- 1.0 LUFS
    And true peak does not exceed -1.0 dBTP
