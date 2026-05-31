> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Evaluator-refiner convergence patterns

Knowledge page. General lessons about the
`generate → evaluate → refine` loop shape that applies to all
components with an internal refinement stage (c01 scenario,
c03 scenario refiner, future visual refiner).

## Invariants

- Refiners must receive the full evaluator report, not just a summary
  verdict.
- The orchestrator must not advance to the next stage while a refiner
  is in flight for the previous stage.
- Hard iteration caps are a safety net, not a convergence strategy.

<!-- Fill in as patterns accumulate -->
