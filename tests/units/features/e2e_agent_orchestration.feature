Feature: End-to-End Multi-Agent Orchestration Happy Path

  Scenario: Successfully run a screenplay through the entire multi-agent pipeline
    Given a screenplay raw dialogue script is loaded in GSA
    And all pipeline agents (Scenario, Audio, Video, Provisioner, Assembly) are running
    When the pipeline is initiated and wakes up all agents sequentially
    Then the Scenario Agent generates structured script blocks
    And the Audio and Video Agents queue media production tasks
    And the Provisioner executes jobs on worker VM environments
    And the Assembly Agent compiles the final validated MP4 movie
