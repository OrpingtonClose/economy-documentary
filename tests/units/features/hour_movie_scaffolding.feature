Feature: Hour-Long Movie Pipeline Scaffolding

  Scenario: Successfully scaffold and compile an hour-long timeline
    Given the event store contains a script for an hour-long documentary (120 blocks, 3600s target)
    When the Provisioner schedules all parallel rendering jobs (120 tts, 120 video)
    And all rendering jobs are completed with media file metadata
    Then the Assembly Agent compiles the entire 120-slot OpenTimelineIO sequence
    And the compiled sequence has zero gaps or overlaps and matches the 3600s target duration
    And the SQLite database WAL performance is verified stable
