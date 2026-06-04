Feature: Concurrency, Busy State, and Endpoint Intervention
  As an operator of the documentary pipeline
  I want to ensure that agents reject concurrent wakeups cleanly with conflict status
  And that PUT requests successfully cancel active turns and terminate all subprocesses without leaving orphans
  And that GET queries return health status immediately and do not block.

  Scenario: POST requests return 409 Conflict if active turns are running
    Given an agent application "test_agent" is running
    When a heavy turn is running in the background on "test_agent"
    And a concurrent POST request is sent to "test_agent"
    Then the concurrent POST request should fail with 409 Conflict
    And the active background turn is allowed to finish

  Scenario: GET health queries return immediately and do not block
    Given an agent application "test_agent" is running
    When a heavy turn is running in the background on "test_agent"
    And a concurrent GET health query is sent to "test_agent"
    Then the GET health query should complete immediately with busy status
    And the active background turn is allowed to finish

  Scenario: PUT requests cancel the active turn and terminate all subprocesses
    Given an agent application "test_agent" is running
    When a turn running a long bash subprocess is triggered on "test_agent" via PUT
    And a concurrent PUT request is sent to "test_agent"
    Then the active turn must be cancelled immediately
    And the running bash subprocess group must be terminated instantly
    And no orphan processes from that subprocess group must remain on the system
    And a new turn must start on "test_agent"
