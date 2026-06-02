Feature: Coordinate Timeline Projection and Shift Cascades

  Scenario: Range overlap exclusion validation
    Given a clean CoordinateTimeline projection
    And we seed two screenplay blocks of duration 3.0 seconds
    And we merge block 1 at offset 0.0 seconds successfully
    When we attempt to merge an overlapping block 3 at offset 1.5 seconds
    Then it should raise a ValueError with "Collision on track"

  Scenario: Dynamic downstream shift propagation
    Given a clean CoordinateTimeline projection
    And we seed two screenplay blocks of duration 3.0 seconds
    And we merge block 1 at offset 0.0 seconds successfully
    And we merge block 2 at offset 3.0 seconds successfully
    When we adjust block 1 duration to 3.5 seconds
    Then block 1 span should be 0.0 to 3.5 seconds
    And block 2 span should be shifted to 3.5 to 6.5 seconds

  Scenario: High-precision time math using SQLite sqlean
    Given a clean CoordinateTimeline projection
    When we query the duration of a 3.23s span starting at 12.0s using sqlean-time
    Then it should return exactly 3230000000 nanoseconds

  Scenario: Point-in-time event replay
    Given a clean EventStore
    And we append a PipelineStarted event
    And we append an UpdateScript event with block 1 duration 3.0s
    And we append a MergeIntoOTIO event for block 1 at 0.0s
    When we replay events up to sequence 2
    Then the timeline should contain 0 clips
    When we replay events up to sequence 3
    Then the timeline should contain 1 clip for block 1

  Scenario: Overlap checks are track-isolated
    Given a clean CoordinateTimeline projection
    And we seed two screenplay blocks of duration 3.0 seconds
    And we merge block 1 on track "A1_Narration" at offset 0.0 seconds successfully
    When we merge block 2 on track "V1_Video" at offset 1.5 seconds successfully
    Then both track timelines should contain their respective clips

  Scenario: Downstream shift propagates recursively across multiple clips
    Given a clean CoordinateTimeline projection
    And we seed three screenplay blocks of duration 3.0 seconds
    And we merge block 1 at offset 0.0 seconds successfully
    And we merge block 2 at offset 3.0 seconds successfully
    And we merge block 3 at offset 6.0 seconds successfully
    When we adjust block 1 duration to 4.0 seconds
    Then block 1 span should be 0.0 to 4.0 seconds
    And block 2 span should be shifted to 4.0 to 7.0 seconds
    And block 3 span should be shifted to 7.0 to 10.0 seconds

