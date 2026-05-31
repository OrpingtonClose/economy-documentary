Feature: Scenario Agent behavior

  Scenario: First Draft Script (UA-1)
    Given the GSA event store is clean
    And the Scenario Agent is running in the VM
    When the Scenario Agent receives an instruction to "Write a short documentary about Lacan's objet petit a."
    Then the GSA event store should contain an "update_script" effect
    And the new script block text must be semantically coherent with the original topic
    And the semantic evaluation metric "script_coherence" must score above 0.85

  Scenario: Rewrite Response on Reconciliation Failed (UA-2)
    Given the GSA event store has a first draft script
    And a downstream reconciliation failure occurred for the block "intro" with delta -3.0 seconds
    And the Scenario Agent is running in the VM
    When the Scenario Agent is woken up
    Then the GSA event store should contain at least two "update_script" effects
    And the rewritten script block text must be adjusted semantically to slow down speech
    And the semantic evaluation metric "duration_alignment" must score above 0.80
