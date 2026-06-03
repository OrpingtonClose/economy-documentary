Feature: Pipeline Faults and State Consistency Guardrails

  Scenario: Verify VM URL mapping uniqueness, ghost VM handling, and job dirty state consistency
    Given a clean local pipeline database
    When the Provisioner allocates multiple VMs with separate worker URLs
    Then all active VMs must have unique non-empty worker URLs in GSA
    When a TTS job is queued, completed, and then a new job is queued for the same block
    Then GSA must correctly mark the block as dirty while the new job is pending
