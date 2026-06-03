Feature: Concurrency, Busy State, and Endpoint Intervention
  As an operator of the documentary pipeline
  I want to ensure that agents serialize or reject concurrent wakeups cleanly
  And that PUT requests successfully cancel active turns and terminate all subprocesses without leaving orphans
  And that GET health queries can run concurrently without blocking.

  Scenario: POST requests return 409 Conflict when the agent is busy
    Given an agent application "test_agent" is running
    When a long-running turn is triggered on "test_agent" via POST
    And a concurrent POST request is sent to "test_agent"
    Then the second POST request must receive a 409 Conflict response
    And the first POST request should eventually complete successfully

  Scenario: GET health queries run concurrently and are not blocked by active turns
    Given an agent application "test_agent" is running
    When a long-running turn is triggered on "test_agent" via POST
    And a GET health query is sent to "test_agent"
    Then the GET health query must return immediately with status 200
    And the first POST request should eventually complete successfully

  Scenario: PUT requests cancel the active turn and terminate all subprocesses
    Given an agent application "test_agent" is running
    When a turn running a long bash subprocess is triggered on "test_agent" via POST
    And a PUT request is sent to "test_agent"
    Then the active turn must be cancelled immediately
    And the running bash subprocess group must be terminated instantly
    And no orphan processes from that subprocess group must remain on the system
    And a new turn must start on "test_agent"
