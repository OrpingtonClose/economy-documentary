Feature: Real Vast.ai Provisioning and Job Execution Lifecycle

  Scenario: Full Real Provisioning, Health Check, Job Dispatch, and Teardown (UA-10-Real)
    Given the GSA event store contains a queued "tts" job
    And the system budget has remaining funds
    And the Provisioner Agent is configured for real Vast.ai cloud provisioning
    When the Provisioner Agent is woken up
    Then it should query available GPU offers on Vast.ai
    And it should select the cheapest suitable offer under $1.50 per hour
    And it should allocate a real instance on Vast.ai using the selected offer
    And the GSA event store should contain a "vm_allocated" effect with the new instance ID
    And we wait for the instance to transition to the running state
    And we verify the worker agent becomes healthy and responsive
    And the Provisioner Agent dispatches the "tts" job to the worker
    And the worker should process the job and produce a narration audio artifact
    And the GSA event store should contain a "job_completed" effect
    And the generated audio file must be downloadable from the worker and have a non-zero size
    Then the Provisioner Agent should deallocate the active VM after job completion
    And the GSA event store should contain a "vm_deallocated" effect with reason "job_done"
