# c01 scenario refiner convergence

Knowledge page. The refiner-not-addressing-evaluator-issues bug
surfaced during slice 1 AG-UI testing. See
<ref_file file="/home/ubuntu/repos/economy-documentary/docs/strands-migration/deploy/slice-1-agui-test-report.md" />
for the 465 s run that hit 3 consecutive `POOR` ratings before converging.

## Symptom

Refiner produces mutations that do not address the specific
`EvaluatorReport` issues raised in the prior iteration.

## Hypotheses

1. Refiner system prompt does not quote the evaluator's specific
   failed checks — it only receives the aggregate `POOR` rating.
2. Refiner is optimising for self-consistency rather than evaluator
   alignment.

## Mitigation in place

- Hard cap `SCENARIO_REFINE_CAP = 3` in `scenario_agent.py` prevents
  infinite loops but does not fix convergence.

## Open questions

- Does quoting the evaluator's concrete failure strings back into
  the refiner prompt close the loop?
- Does switching the refiner to a lower-temperature decoding setup
  reduce divergence?
