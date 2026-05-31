Feature: Real Vast.ai Video VM Lifecycle and Artifact Production

  Scenario: Video VM Provisioning, LTX Job Execution, and Teardown (UA-11-Real)
    Given the GSA event store contains a queued "video" job for "ltx"
    And the system budget has remaining funds
    And the Provisioner Agent is configured for real Vast.ai cloud provisioning
    When the Provisioner Agent is woken up
    Then it should query available GPU offers suitable for LTX-2.3 (VRAM >= 24GB)
    And it should select a suitable offer under $2.00 per hour
    And it should allocate a real instance on Vast.ai using the selected offer
    And the GSA event store should contain a "vm_allocated" effect for the video worker
    And we wait for the instance to transition to the running state
    And the Provisioner Agent dispatches the "video" job to the worker
    And the worker should generate the documentary video clip
    And the GSA event store should contain a "job_completed" effect
    And the generated video clip file must be downloadable from the worker and have a non-zero size
    Then the Provisioner Agent should deallocate the video VM
    And the GSA event store should contain a "vm_deallocated" effect with reason "job_done"
