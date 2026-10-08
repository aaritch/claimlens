# ClaimLens: HIPAA-Style Document Intake Pipeline on AWS Bedrock

A portfolio project designed to mirror the GDIT "AI/ML Document Processing Developer" (CMS) job description, component by component.

---

## 1. What You're Building (and an honest framing note)

**One-sentence pitch:** A pipeline that ingests messy healthcare-style documents (PDF, DOCX, emails, scans) into S3, classifies them, extracts structured data with AWS Bedrock, validates the result with deterministic rules, stores it, and measures accuracy and cost.

**Analogy: a hospital mail room.** Letters (documents) arrive in all shapes. A clerk sorts them (classification), reads the handwriting on scanned ones with a magnifier (OCR), fills in a standard form (structured extraction), a supervisor checks the form against the rulebook (hybrid validation), and a manager keeps stats on how often clerks make mistakes and how much each letter costs to process (evaluation + cost tracking).

**Framing note:** Present this as a *portfolio / reference implementation built on synthetic data*, not as production CMS experience. Your strength is the DevOps/IaC/AI-agent background; this project shows you can apply it to a document-processing domain. Interviewers respect "I built this to learn the domain, here's what I'd do differently in production" far more than keyword stuffing.

**Hard rule:** Use only **synthetic** documents. Never real PHI/PII, even "just for testing."

---

## 2. JD-to-Component Map

| JD requirement | Where it shows up in ClaimLens |
|---|---|
| AWS Bedrock: prompt engineering, structured output, model selection, token/cost management | Components 4, 5, 9 |
| S3, Lambda, ECS/Fargate or Batch, ECR, IAM, KMS, CloudWatch | Components 1, 2, 3, 8, 10 |
| RESTful services + event-driven backends | Component 2 (event flow) + Component 7 (REST API) |
| Strong Python, concurrent batch processing | Component 3 (worker with thread/async concurrency) |
| PDF, DOCX, email parsing; OCR with Textract/Tesseract | Components 3, 4 |
| Postgres / DynamoDB / Redis | Components 6, 7 (Postgres + DynamoDB + Redis cache) |
| Docker builds + CI/CD (Git, Jenkins, GitHub Actions) | Component 10 |
| pytest + code coverage | Component 9 |
| Measuring model quality (ground truth, precision/recall, A/B, prompt iteration) | Component 9 |
| Hybrid deterministic rules + LLM | Component 5 |
| PII/PHI handling (VPC isolation, encryption, least-privilege IAM, audit logging) | Component 8 |
| Healthcare claims / regulated document workflows | Synthetic IDR + surprise-billing docs (Component 11) |
| Salesforce data integration | Component 12 (stretch) |
| SQL analytics (Snowflake/Redshift) | Component 12 (stretch, Athena as stand-in) |
| IaC (Terraform, CDK, CloudFormation) | Component 10 (Terraform, your strength) |
| Jira / agile / communication | Component 13 (repo process + docs) |
| Active AWS certification | Not code, but mention which cert you're pursuing/hold |

---

## 3. Architecture

```
                         ┌────────────────────────────────────────────┐
  Synthetic docs  ──►    │  S3 "landing" bucket (KMS-encrypted)       │
  (PDF/DOCX/EML/scan)    └──────────────────┬─────────────────────────┘
                                            │ S3 → EventBridge
                                            ▼
                                   EventBridge rule (filter by suffix/prefix)
                                            │
                                            ▼
                                  SQS queue  ──► DLQ (failed after N tries)
                                            │
                                            ▼
                        ┌───────────────────────────────────────┐
                        │ Worker: ECS Fargate task (or Lambda)  │
                        │  1. Detect file type                  │
                        │  2. Native parse (pypdf/python-docx/  │
                        │     email stdlib)                     │
                        │  3. Text too sparse? → Textract OCR   │
                        │  4. Bedrock: classify doc type        │
                        │  5. Bedrock: extract fields (schema)  │
                        │  6. Rules engine validates            │
                        │  7. Confidence/rule fail → review Q   │
                        └──────┬─────────────────┬──────────────┘
                               ▼                 ▼
                        Postgres (RDS)      DynamoDB (status/idempotency)
                        validated records   + Redis (cache/dedupe)
                               │
                               ▼
                    FastAPI (REST) on Lambda/Fargate ──► query claims, review queue
                               │
                    CloudWatch metrics/dashboards (tokens, cost, OCR rate, latency)
                    CloudTrail + audit log (who accessed what)
```

**Why a queue between EventBridge and the worker?**
Analogy: a restaurant ticket rail. If 500 orders arrive at once, the kitchen doesn't panic; tickets wait in order, and a ticket that keeps getting burned goes to a "problem orders" spot (the DLQ). Without SQS, a burst of uploads can overwhelm Bedrock rate limits and you lose failures silently.

---

## 4. Components in Detail

### Component 1: Secure Landing Zone (S3 + KMS)
- **What:** Encrypted bucket with separate prefixes: `incoming/`, `processed/`, `failed/`, `quarantine/`.
- **Build notes:** Block public access, versioning on, bucket policy requiring TLS, SSE-KMS with a customer-managed key, lifecycle rules to expire test data.
- **Analogy:** A locked mailbox where only the mail carrier can drop things in and only the mail room staff can take them out.

### Component 2: Event-Driven Backbone (EventBridge + SQS + DLQ)
- **What:** S3 object-created events go to EventBridge, then SQS, then your worker.
- **Build notes:** Filter on prefix/suffix *inside the EventBridge rule* (the earlier draft only claimed to filter .pdf/.txt but did not). Add a DLQ with a redrive policy, and set a visibility timeout longer than your max processing time.
- **JD hit:** "event-driven backends in AWS."

### Component 3: Multi-Format Parsing Engine (Python)
- **What:** A router that picks a parser by file type.
  - PDF: `pypdf` (or `pdfplumber`)
  - DOCX: `python-docx`
  - Email: Python's built-in `email` module for `.eml` (stretch: `.msg` via `extract-msg`); extract body *and* attachments, then recurse
  - Images / scanned PDFs: go to OCR (Component 4)
- **Concurrency:** Process a batch with `concurrent.futures.ThreadPoolExecutor` (I/O-bound: S3, Bedrock, Textract calls) and explain *why threads fit I/O-bound work*, while `ProcessPoolExecutor` fits CPU-bound work like heavy PDF rendering.
- **Build notes:** Design each parser as a small class with the same interface (`parse(path) -> ParsedDocument`). This is a beginner-friendly way to learn clean Python structure.
- **Analogy:** A mail sorter with different tools for envelopes, packages, and faxes, all handing the same-shaped "document summary card" to the next station.

### Component 4: OCR Fallback (Textract, optional Tesseract)
- **What:** If native text is below a density threshold (chars per page, not just total chars), route to OCR.
- **Fixes to the earlier draft:**
  - `detect_document_text` on an S3 object only handles **single-page** PDFs/images. For multi-page PDFs use the **asynchronous** `start_document_text_detection` + poll/SNS completion.
  - Measure density *per page*, because a 20-page PDF with 1 text page could still exceed 150 total characters.
  - Log how often OCR fires (it's your biggest avoidable cost).
- **Stretch:** Add a Tesseract path in the container as a free/local alternative and compare accuracy and cost against Textract. That comparison alone is an excellent interview story.
- **Analogy:** Reading printed text with your eyes vs. using a magnifier and a translator for blurry faxes. The magnifier works but costs money per page.

### Component 5: Bedrock Classification + Structured Extraction
- **Two calls, two jobs:**
  1. **Classify** (cheap/fast model): Surprise Billing Complaint, Provider Appeal, IDR Registration, Other.
  2. **Extract** (stronger model if needed): only the fields for that doc type.
- **Structured output:** Use the Bedrock **Converse API with tool use** and a forced `toolChoice`, with JSON schema generated from **Pydantic** models. Validate the result with Pydantic anyway; forced tool use makes the shape *very likely* correct, not guaranteed.
- **Model selection:** Make `MODEL_ID` an environment variable. The earlier draft hardcoded Claude 3.5 Sonnet (June 2024), which is an older model; check the Bedrock console for currently available models and inference profiles in your region and enable model access there. Build a small config that maps task, then model, so you can A/B models later.
- **Prompt engineering:** Store prompts as versioned files (`prompts/extract_v1.txt`, `v2`...), never inline strings. Include few-shot examples and an "if unknown, return null, never guess" instruction.
- **Token/cost management:** Truncate or chunk very long docs, read `usage.inputTokens/outputTokens` from each response, compute cost from a **pricing config file** (not hardcoded in logic).
- **Analogy:** Handing a form to a very smart temp worker. The Pydantic schema is the form's boxes; "return null if unknown" is the sticky note saying "leave blank rather than guess."

### Component 6: Hybrid Rules Engine (the "deterministic half")
- **What:** Plain Python rules that cross-check the LLM's output, because LLMs shouldn't be the final authority on facts that can be verified.
- **Example rules:**
  - NPI is exactly 10 digits **and passes the Luhn check** (NPIs use a Luhn variant with the 80840 prefix; a real, checkable detail)
  - Disputed amount > 0 and within a configured range
  - Dates parse and the service date is not in the future
  - Payer name normalizes to a known payer list (fuzzy match)
  - Required fields present per document type
- **Routing:** Pass → `validated`; soft fail → `needs_review`; hard fail → `rejected` with a reason code.
- **Analogy:** The LLM is the intern filling forms; the rules engine is the supervisor with a checklist and a calculator.

### Component 7: Storage + REST API + Caching
- **Postgres (RDS or local Docker for dev):** validated claim records, review queue, extraction history. Use a **claim_id (UUID)** as the primary key.
- **DynamoDB:** processing status + **idempotency keys** (S3 events can be delivered more than once, and you don't want to process or pay twice). Key on `document_id`, not on `provider_npi + payer_name` (the earlier draft's keys would overwrite one provider's multiple claims from the same payer).
- **Redis (ElastiCache or Docker locally):** cache payer-name normalization and dedupe by content hash (same document re-uploaded = reuse previous extraction, saving tokens).
- **REST API (FastAPI):** `POST /documents` (presigned upload URL), `GET /documents/{id}`, `GET /claims?status=needs_review`, `POST /claims/{id}/review` (human correction, which feeds your eval set). Add OpenAPI docs automatically.
- **JD hit:** "RESTful services and event-driven backends," Postgres + DynamoDB + Redis in one project.

### Component 8: Security and HIPAA-Style Controls
- **VPC isolation:** Private subnets and **VPC endpoints for every service the worker touches**: S3 (gateway), DynamoDB (gateway), Bedrock Runtime, Textract, KMS, CloudWatch Logs, SQS, ECR (api + dkr), STS. *Important fix:* the earlier Terraform only created the Bedrock endpoint, so a Lambda/Fargate task in private subnets with no NAT would fail to reach S3, DynamoDB, Textract, KMS, or CloudWatch.
- **Encryption:** KMS CMK for S3, DynamoDB, RDS, SQS, CloudWatch Logs, ECR.
- **Least-privilege IAM:** One role per component (worker, API, deploy), scoped to specific ARNs, no `Resource: "*"` except where AWS requires it (document why in a comment). Use IAM Access Analyzer to check.
- **PHI-safe logging:** A log filter/redactor so document text, names, and SSN-like patterns never reach CloudWatch. Log IDs and metrics only. Add a unit test that feeds fake PHI to the logger and asserts it's scrubbed.
- **Audit logging:** CloudTrail (data events on the bucket), plus an application audit table recording who viewed/changed which claim.
- **Bedrock note:** Document that you'd confirm the account's HIPAA eligibility/BAA coverage for each service before real PHI, since synthetic data is used here.
- **Analogy:** A bank vault with a guest log: locked door (VPC/KMS), keycards that open only specific drawers (IAM), a camera (CloudTrail), and a rule that you never read account numbers aloud (log redaction).

### Component 9: Evaluation, Observability, and Cost Tooling
This is the component that most separates you from other candidates.
- **Ground truth set:** 50-100 synthetic documents with hand-labeled expected JSON (generate with a script + variations: typos, different layouts, scanned versions, emails with attachments).
- **Metrics:** Per-field precision, recall, F1, exact-match rate, plus classification confusion matrix.
- **pytest suite:**
  - Unit tests: parsers, rules engine, redaction, cost calculator
  - Integration tests: use `moto` (mock AWS) for S3/DynamoDB/SQS; stub Bedrock responses
  - Eval test: runs the labeled set and **fails the build if F1 drops below a threshold** (regression gate)
  - `pytest-cov` coverage report with a minimum threshold
- **A/B comparisons:** Prompt v1 vs v2, Haiku-class vs Sonnet-class model, native-parse vs OCR. Output a comparison table (accuracy, cost per doc, latency).
- **Error analysis:** Script that lists the most-missed fields and groups failures by cause (OCR noise, ambiguous amount, layout). Write short notes on what you changed in the prompt and the measured result.
- **CloudWatch:** Custom metrics (tokens in/out, cost per doc, OCR rate, validation-fail rate, DLQ depth), a dashboard, and alarms (DLQ not empty, cost per doc spike).
- **Analogy:** A school report card for your pipeline. You don't just ask "is it smart?"; you grade each subject, find the weak ones, and tutor those.

### Component 10: Infrastructure as Code + Containers + CI/CD
- **Terraform** (your strength) in modules: `network`, `storage`, `queue_events`, `compute`, `data`, `observability`, `iam`. Remote state with S3 + DynamoDB locking (document it even if you use local state for dev).
- **Docker:** Multi-stage build for the worker (and API); pinned base image; non-root user; push to ECR with scanning on push.
- **Compute choice:** Worker on **ECS Fargate** (long-running batch, no 15-minute Lambda cap) and a **Lambda** for lightweight glue (e.g., the presigned-URL generator). Explaining *why* each is used is the point.
- **CI/CD (GitHub Actions):** lint (ruff) → type-check (mypy) → pytest + coverage → eval regression gate → build image → Trivy scan → push to ECR → `terraform plan` on PR → manual-approval `terraform apply`. Use OIDC to assume an AWS role (no stored keys).
- **Jenkins (optional):** Provide a `Jenkinsfile` equivalent to show you can translate the pipeline.
- **Fixes to the earlier draft:** the Lambda assume-role principal and EventBridge permission principals were malformed (`"://amazonaws.com"` instead of `lambda.amazonaws.com` / `events.amazonaws.com`); the `terraform apply` ordering needs the image to exist before the function is created; `:latest` tags are discouraged (use a git SHA tag); `force_destroy = true` is fine for dev but should be a variable.

### Component 11: Synthetic Data Generator
- Script that produces realistic-looking but fake IDR/surprise-billing documents: complaint letters (DOCX), appeal letters (PDF), emails with PDF attachments (`.eml`), and "scanned" versions (render to image, add noise/skew, save as PDF).
- Include fake NPIs that pass the Luhn check, fake payers, fake patient names.
- **Why it matters:** Shows domain empathy and gives you reproducible eval data.

### Component 12: Stretch Goals (the "preferred skills" column)
- **SQL analytics:** Export validated records to S3 as Parquet and query with **Athena** (Redshift/Snowflake stand-in), with a few reporting queries (claims by payer, avg dispute amount, validation-fail rate).
- **Salesforce integration:** Push validated records to a Salesforce Developer Edition org (free) via its REST API into a custom object. This maps directly to the MATS Salesforce context in the JD.
- **Human-in-the-loop review UI:** A tiny page that lists `needs_review` items so corrections become new ground truth.
- **Step Functions** to orchestrate classify → extract → validate with retries (alternative to the single worker).

### Component 13: Agile Process and Communication
- Run the project in a **Jira (free tier) board** or GitHub Projects with epics/stories matching the components above.
- Write an **ADR (architecture decision record)** for 3-4 key choices (Fargate vs Lambda, Textract vs Tesseract, Postgres vs DynamoDB roles, model selection).
- README with architecture diagram, a cost breakdown, and a "what I'd change for real PHI" section.
- Record a 3-5 minute demo. The JD stresses communicating with technical and non-technical audiences.

---

## 5. Suggested Repo Layout

```
claimlens/
├── README.md
├── docs/
│   ├── architecture.md
│   ├── adr/                    # decision records
│   └── security-notes.md
├── src/claimlens/
│   ├── parsers/                # pdf.py, docx.py, email.py, router.py
│   ├── ocr/                    # textract.py, tesseract.py, density.py
│   ├── llm/                    # bedrock_client.py, schemas.py, pricing.py
│   ├── prompts/                # classify_v1.txt, extract_v1.txt ...
│   ├── rules/                  # npi.py, amounts.py, payers.py, engine.py
│   ├── storage/                # postgres.py, dynamo.py, cache.py
│   ├── api/                    # FastAPI app
│   ├── worker.py               # SQS consumer + concurrency
│   └── logging_safe.py         # PHI-redacting log filter
├── tests/
│   ├── unit/  integration/  eval/
│   └── fixtures/ground_truth/
├── data_gen/                   # synthetic document generator
├── infra/terraform/            # modules + envs/dev
├── docker/                     # Dockerfile.worker, Dockerfile.api
├── .github/workflows/          # ci.yml, deploy.yml
└── scripts/                    # deploy.sh, run_eval.py
```

---

## 6. Phased Build Plan (do it in this order)

| Phase | Goal | You'll know it works when... |
|---|---|---|
| 0 | Repo, venv, pre-commit, pytest skeleton, synthetic data generator (10 docs) | `pytest` runs green on a trivial test |
| 1 | **Local-only core:** parsers + Bedrock extraction + Pydantic + rules, run from the command line on local files | One fake document in, validated JSON out |
| 2 | Evaluation harness: ground truth + precision/recall + cost report | You can state "F1 is 0.87 at $0.004/doc" |
| 3 | OCR fallback + scanned-document tests | Scanned docs match native-text accuracy within a few points |
| 4 | Storage: Postgres + DynamoDB idempotency + Redis dedupe + FastAPI | Re-uploading the same file doesn't re-bill tokens |
| 5 | Terraform + Docker + AWS deploy with the event flow (S3 → EventBridge → SQS → Fargate) | Drop a file in S3, see a row appear in Postgres |
| 6 | Security hardening: VPC endpoints, KMS everywhere, IAM tightening, log redaction tests, CloudTrail | IAM Access Analyzer is clean; redaction test passes |
| 7 | CI/CD with eval regression gate + dashboards/alarms | A bad prompt change fails the pipeline |
| 8 | Stretch: Athena, Salesforce, review UI; polish README + demo video | You can demo it end to end in 5 minutes |

**Beginner-friendly tip:** Phase 1 is deliberately *local only*. Get the brain working before building the plumbing. (Analogy: learn to cook the recipe in your kitchen before building a restaurant around it.)

---

## 7. Cost and Safety Guardrails

- Set an **AWS Budget alert** (e.g., $25/mo) *before* deploying anything.
- NAT gateways, interface VPC endpoints, RDS, and ElastiCache all bill hourly; `terraform destroy` at the end of every session, or use a small `dev` toggle.
- Cap Bedrock spend by limiting eval runs and caching results by document hash.
- Never commit AWS keys; use SSO/OIDC.

---

## 8. Interview Talking Points (honest version)

1. "I measured it": F1 per field, cost per document, OCR trigger rate, and what I changed after error analysis.
2. "Hybrid, not LLM-only": the rules engine caught X% of bad extractions the model was confident about.
3. "Security by design": VPC endpoints, KMS CMKs, least-privilege IAM, PHI-safe logs, audit trail, and what I'd still need for a real BAA environment.
4. "Event-driven and resilient": idempotency, DLQ, retries, and what happens when Bedrock throttles.
5. "Trade-offs I chose": Fargate vs Lambda, Textract vs Tesseract, model tiering for cost.
6. Be upfront that it's a synthetic-data reference project; connect it to your real DevOps/IaC/agentic-AI work.

---

## 9. Corrections to Carry Forward from the Earlier Conversation

1. Hardcoded legacy model ID: make it configurable and verify what's available in your region.
2. Lambda in private subnets needs VPC endpoints for *all* services it calls, not just Bedrock.
3. Malformed service principals in IAM/Lambda permission blocks (`://amazonaws.com`).
4. EventBridge rule comment says it filters file types but the pattern doesn't; add suffix/prefix matching.
5. DynamoDB key design (`provider_npi` + `payer_name`) would overwrite records; use a unique document/claim ID.
6. Reading every S3 object with `.decode("utf-8")` breaks on real PDFs/DOCX; route through the parser layer.
7. Textract sync API is single-page only; use async for multi-page.
8. 60-second Lambda timeout and 512 MB memory are tight for OCR + LLM calls; use Fargate for heavy work or raise limits.
9. "Temperature 0 = deterministic" is a simplification; still validate every output.
10. Pricing constants should live in config, not code.
11. Skipping SQS/DLQ means failures vanish; add them.
12. Use image tags tied to git SHAs instead of `:latest`.
