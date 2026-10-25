# Model usage for the current full-dataset run

The full 518-request run of `python3 code/main.py run` completed locally without a model call. The optional Gemini model is configured in source as `gemini-2.5-flash`, but it was **not invoked** because `GEMINI_API_KEY` was absent. No provider/model should be reported as used for this run.

| Metric | Actual value |
| --- | ---: |
| Model providers used | None |
| Model calls | 0 |
| Cached model responses used | 0 |
| Retries | 0 |
| Model failures | 0 |
| Input tokens | 0 |
| Output tokens | 0 |
| Total tokens | 0 |
| Average tokens per request | 0 |
| Estimated total model cost | $0 |
| Estimated model cost per request | $0 |

This report describes the current provisional `output.csv` run only. Recreate it from actual API response usage and price data if a later run uses Gemini. Do not infer token usage from request count or present a simulated call as real.
