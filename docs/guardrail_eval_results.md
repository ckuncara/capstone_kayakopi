# Guardrail evaluation results

Offline run on 26 November 2026:

```text
.\.venv\Scripts\python.exe -m pytest tests\unit\test_guardrail_eval.py -q --tb=short
..........................................                               [100%]
42 passed in 4.01s
```

The evaluation covers 22 questions, including five phrasings of the individual-pay kill shot
and deterministic refusals, using synthetic evidence and a mocked model. It also checks that
confidential details in structured model answers are filtered before reaching the user, and
that incomplete or misleading example answers fail the strengthened checks. This verifies the
guarded chat path, not the live model's reasoning or factual accuracy. The answer checks are
pattern-based and can reject a correct answer if it is worded differently.
The live evaluation has not been run as part of this recorded result.
