# Core-engine evaluation (current milestone)

Commands run from the repository root:

```text
python3 code/evaluation/main.py
python3 -m unittest discover -s code/evaluation -p 'test_*.py' -v
python3 code/main.py run
python3 code/main.py validate
```

The 32 public sample requests are separate from the 518 evaluation requests. Expected sample answers were read only by `code/evaluation/main.py` after the engine produced its predictions.

| Measure | Actual result |
| --- | ---: |
| Case-level deletion-status accuracy | 27 / 32 (84.4%) |
| Entire ordered step arrays exactly correct | 5 / 32 (15.6%) |
| Exact predicted step objects found in expected arrays | 93 / 142 predicted (65.5% precision); 93 / 193 expected (48.2% recall) |
| Position-aligned step fields equal to expected fields | 820 / 1,351 (60.7%); this simple measure penalizes shifted arrays |
| Full evaluation-request CSV | 518 rows; zero structural or citation-time validator errors |
| Cases flagged uncertain in full run | 35 |
| Authorized full-run rows with no supported steps | 33 |
| Evidence extraction failures | 0 / 94 files |

Three additional independent checks in `code/evaluation/test_core.py` all passed: a later capture cannot prove an earlier state; a tombstone may keep the stable reference but not a direct email; and an export request with an explicit refusal of deletion does not authorize deletion.

Observed failures include five wrong sample statuses and 27 cases without an exact step sequence. The first two complete sample cases have no direct identifier match in the available parsed evidence, so the current engine conservatively emits incomplete empty results rather than inventing an absence. Other missing steps commonly involve an absent or anonymized resource that cannot yet be linked to its subject from evidence available at `audit_at`. These are investigation gaps, not validator successes; `output.csv` is provisional.

Improvements made during this milestone: parse readback logs and nested structured log messages, distinguish event time from capture time, preserve primary/replica observations, classify explicit flow and restore states, and reject later evidence citations. The next improvement is grounded cross-record linking; sample labels must remain isolated from prediction logic.
