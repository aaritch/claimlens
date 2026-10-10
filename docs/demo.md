# Offline demo and recording script

```powershell
.\.venv\Scripts\python.exe -m scripts.demo --out results/demo
```

This creates a **new** output directory, a synthetic dataset, evaluation report, Parquet file and metadata-only summary.
It uses mock models and an isolated temporary SQLite database. It does not connect to AWS/Salesforce or touch the
database configured in your shell. Use a fresh output directory for each run.

## Five-minute walkthrough

1. **0:00–0:45, framing:** synthetic-data reference implementation; show `docs/architecture.md`.
2. **0:45–1:30, extraction/evaluation:** run the demo, show the evaluation report and distinguish mock plumbing
   from real Bedrock quality. Explain why plausible wrong amounts require evaluation, not just rules.
3. **1:30–2:15, reliability:** show summary upload 201 / duplicate 200. Explain content+recipe fingerprint,
   leases, unique constraints, retry backoff and the dead-letter queue.
4. **2:15–3:15, human review:** run a localhost mock API, upload an incomplete synthetic appeal,
   open `/review/`, correct fields and approve. Show stale-version/invalid-NPI protections and answer-key export.
5. **3:15–4:15, delivery/security:** show the CI/deploy workflow split, migration checks, opaque names,
   structured logs, CloudTrail and the monitoring dashboard definition.
6. **4:15–5:00, integrations and honesty:** show Parquet allow-listed fields and Salesforce dry run.
   Explain what still needs credentials, networking, service smoke tests, least-privilege DB users and real-PHI review.

This is a recording storyboard and executable offline demo, not a claim that a live-cloud demo or narrated video
has been recorded.
