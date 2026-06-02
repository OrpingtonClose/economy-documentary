Feature: Multi-Scene Transition and Visual Integrity

  Scenario: Assemble a multi-scene documentary with transitions and ensure clean boundaries
    Given a script with 10 scenes, each scene containing multiple blocks
    When the rendering jobs for all audio and video blocks are completed
    And the Assembly Agent applies a cross-dissolve transition at scene boundaries
    Then the compiled timeline has transition effects at scene changes with zero track misalignment
