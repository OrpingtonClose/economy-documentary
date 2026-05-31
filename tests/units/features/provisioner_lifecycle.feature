Feature: Provisioner VM Lifecycle Management

  Scenario: Allocation of VM for Pending Jobs (UA-10)
    Given the GSA event store contains a queued "tts" job
    And the system budget has remaining funds
    When the Provisioner Agent is woken up
    Then the GSA event store should contain a "vm_allocated" effect

  Scenario: Reclaiming a stuck or unresponsive VM (UA-12)
    Given the GSA event store has a VM allocated over 900 seconds ago
    When the Provisioner Agent is woken up
    Then the GSA event store should contain a "vm_deallocated" effect with reason "provision_failed"

  Scenario: Deallocating idle VMs to preserve budget (UA-13)
    Given the GSA event store has an active running VM
    And no pending or running jobs exist in the queue
    When the Provisioner Agent is woken up
    Then the GSA event store should contain a "vm_deallocated" effect with reason "job_done"
