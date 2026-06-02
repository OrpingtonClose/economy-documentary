Feature: Localized Segment Recovery

  Scenario: Pipeline recovers from localized job failures without repeating completed tasks
    Given a 100-block documentary run where 98 blocks have completed audio/video jobs but 2 blocks have failed
    When the Provisioner detects the failure logs in the event store
    Then it retries only the 2 failed jobs on a fresh or recycled worker VM
    And the Assembly Agent holds compilation until the retried segments are completed
    And the final movie timeline compiles successfully with all 100 media slots present
