Feature: Concurrency, Busy State, and Endpoint Intervention
  As an operator of the documentary pipeline
  I want to ensure that agents serialize or reject concurrent wakeups cleanly
  And that PUT requests successfully cancel active turns and terminate all subprocesses without leaving orphans
  And that GET and POST queries block to wait for active turns to finish.

  Scenario: POST requests block to wait for active turns to finish
    Given an agent application "test_agent" is running
    When a heavy turn is running in the background on "test_agent"
    And a concurrent POST request is sent to "test_agent" in a separate task
    Then the active background turn is allowed to finish
    And the concurrent POST request should then complete successfully

  Scenario: GET health queries block to wait for active turns to finish
    Given an agent application "test_agent" is running
    When a heavy turn is running in the background on "test_agent"
    And a concurrent GET health query is sent to "test_agent" in a separate task
    Then the active background turn is allowed to finish
    And the GET health query should then complete successfully

  Scenario: PUT requests cancel the active turn and terminate all subprocesses
    Given an agent application "test_agent" is running
    When a turn running a long bash subprocess is triggered on "test_agent" via PUT
    And a concurrent PUT request is sent to "test_agent"
    Then the active turn must be cancelled immediately
    And the running bash subprocess group must be terminated instantly
    And no orphan processes from that subprocess group must remain on the system
    And a new turn must start on "test_agent"
