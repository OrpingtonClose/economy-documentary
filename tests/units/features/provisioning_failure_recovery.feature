Feature: Automated Infrastructure Failure Recovery

  Scenario: Provisioner handles cold-start timeouts, preemption, and restarts
    Given a pipeline queue with pending jobs
    When a VM worker fails to boot within its timeout window
    Then the Provisioner condemns that VM and provisions a replacement VM
    When an active VM worker is preempted mid-job
    Then the Provisioner detects the preemption, reschedules the interrupted job, and allocates a replacement VM
    When the Provisioner process is terminated and restarted mid-run
    Then it replays the event log to discover active VMs and resume job routing without double-provisioning
