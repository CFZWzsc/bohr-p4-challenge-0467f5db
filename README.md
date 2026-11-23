# Runtime contract

Bohrium Playground Period 4 submission — challenge `challenge-0467f5db` (track: `literature-search`).

This repository holds the complete skill package for the challenge: the skill
definition, its executable entry point and all supporting scripts. No precomputed
answers, private data or credentials are included.

## Skill

- Name: `retrieval-coverage-gap-analysis`
- Summary: Use when a research search supplies a known-relevant reference set plus retrieved records and query logs, and needs recall/coverage computed from the reference denominator, per-reference hit status, actionable miss classification, and supplementary-round marginal gain in a contract-preserving result.json.

## Contents

| File |
|------|
| `SKILL.md` |
| `scripts/run_skill.py` |
| `README.md` |

## How to run

```bash
python scripts/run_skill.py --case /input/case.json --output /app/output/result.json
```

The entry point is `SKILL.md`; input/output contracts, error handling and a minimal
usage example are documented there.

## Notes

- Pure Python standard library; no network access required.
- The result JSON keeps the input case id, the main results, per-item evidence,
  a run status and an uncertainty section.
