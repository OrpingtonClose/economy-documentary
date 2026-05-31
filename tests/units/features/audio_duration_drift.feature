Feature: Audio Agent Duration Drift and Speed Tuning

  Scenario: TTS Duration passes within tolerance (UA-4)
    Given the GSA event store contains a queued "tts" job
    And the target speech duration is 6.5 seconds
    When the TTS job is completed with an actual duration of 6.8 seconds
    And the Audio Agent is woken up
    Then the GSA event store should contain a "duration_adjusted" effect

  Scenario: TTS Duration fails and triggers speed-ratio tuning (UA-5)
    Given the GSA event store contains a queued "tts" job
    And the target speech duration is 6.0 seconds
    When the TTS job is completed with an actual duration of 3.0 seconds
    And the Audio Agent is woken up
    Then the GSA event store should contain a "job_requeued" effect
    And the requeued job should have adjusted speed parameters

  Scenario: TTS fails repeatedly and escalates to scenario rewrite (UA-6)
    Given the GSA event store contains a block that has failed TTS reconciliation 5 times
    When the Audio Agent is woken up
    Then the GSA event store should contain a "reconciliation_failed" effect
