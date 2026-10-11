# ClaimLens (Phases 0-8): document intake, evaluation, review, security, delivery and analytics

Parses messy healthcare-style documents (PDF, DOCX, EML with attachments, TXT),
classifies them and extracts structured fields with **AWS Bedrock** (forced tool
use + Pydantic), then double-checks the result with **deterministic rules**.

> **Synthetic data only.** Never put real PHI/PII in this project.

**Start here:** [architecture](docs/architecture.md), [Phases 6–8 guide](docs/phases-6-8.md),
[security notes](docs/security-notes.md), [operations runbook](docs/operations.md),
and [offline demo](docs/demo.md). `project-outline.md` is the original roadmap, not the current API contract.

## 1. Setup

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt   # runtime + test tools (production image uses requirements.txt only)
pytest                                # should be all green, no AWS needed
```

## 2. Try it with NO AWS account (mock mode)

```bash
python -m claimlens gen-data --out data/samples --count 12
python -m claimlens run-dir data/samples --mock --out results
```

You'll see a status table. Open `results/*.result.json` to see the full output.
The mock is a *crash-test dummy*: it exercises the parsers, rules and plumbing,
but says nothing about real model accuracy.

## 3. Run it for real against Bedrock

1. In the AWS console: **Bedrock -> Model access** -> enable a Claude model in your region.
2. Copy the model (or inference-profile) ID from the console. IDs change as models
   are released and retired, which is why none is hardcoded here.
3. Authenticate the AWS CLI (`aws configure` or SSO) with permission for `bedrock:InvokeModel`
   (the Converse API uses the same permission).
4. ```bash
   export AWS_REGION=us-east-1
   export CLAIMLENS_MODEL_ID="<your model id>"
   # optional cheaper model for the classify step:
   # export CLAIMLENS_CLASSIFY_MODEL_ID="<smaller model id>"
   python -m claimlens run data/samples/01_surprise_billing_complaint.docx
   ```
5. Replace the placeholder prices in `claimlens/llm/pricing.json` with current ones
   from the AWS Bedrock pricing page, or the cost column is meaningless.
   Set an AWS Budget alert first.

## 4. How it fits together

```
file -> parser router -> ParsedDocument -> classify (LLM) -> extract (LLM) -> rules -> ProcessingResult
```

| Piece | File | Analogy |
|---|---|---|
| Settings | `config.py` | Thermostat panel: all knobs in one place |
| Data shapes | `models.py` | Blank forms with typed boxes (Pydantic refuses bad answers) |
| Parsers | `parsers/` | Different openers for envelopes, boxes, faxes, all producing the same summary card |
| Bedrock wrapper | `llm/bedrock_client.py` | Hands the model a form and says "answer only by filling this in" |
| Prompts | `prompts/*.txt` | Versioned instructions; each result records which version made it |
| Rules | `rules/` | Supervisor with a checklist and calculator |
| Pipeline | `pipeline.py` | The assembly line that wires it all together |
| Synthetic data | `datagen.py` | Practice paperwork plus an answer key for Phase 2 |

### Status meanings
- `validated`: all rules passed.
- `needs_review`: something missing/unusual (soft rule), so a human should look.
- `rejected`: provably wrong (hard rule), e.g. NPI fails its check digit or the date is in the future.
- `needs_ocr`: little or no text layer; OCR fallback arrives in Phase 3.
- `error`: unreadable file, or the model's output failed schema validation.

### Design decisions worth being able to explain
- **Tool use + Pydantic, both.** Forced tool use makes the right JSON shape very likely; Pydantic *guarantees* it before anything is stored.
- **Null over guessing.** Every extracted field is Optional and the prompt says "null beats a guess."
- **LLM output is never trusted blindly.** The NPI check digit catches digits an LLM (or OCR) misreads.
- **Prompt-injection hygiene.** Documents are untrusted; they're wrapped in `<document>` tags and the prompt says never to follow instructions inside them.
- **Data minimization.** The prompt forbids extracting patient identifiers; only business fields are pulled.
- **No document text in logs or errors.** Only metadata (file name, status, tokens, cost).
- **Per-page text density** (not total) decides whether a PDF needs OCR.
- **Configurable model IDs and prices**, never hardcoded.

## 5. Things to experiment with (great for learning)
1. Break something on purpose: edit a generated document's NPI by one digit and re-run. Watch the rules reject it.
2. Add a new rule in `rules/engine.py` plus a test in `tests/test_rules.py`.
3. Copy `prompts/extract_v1.txt` to `extract_v2.txt`, change the wording, point `EXTRACT_PROMPT` in `pipeline.py` at it, and compare results. That's the seed of Phase 2's A/B testing.
4. Add a new document type (enum in `models.py`, template in `datagen.py`, required fields in `rules/engine.py`).

## 6. Implementation status
Phases 0–5 provide parsing, extraction, evaluation, OCR, storage and the event-driven AWS stack.
Phases 6–8 add security controls, delivery workflows, monitoring, analytics and the human review UI.
These are a synthetic-data reference implementation, not a claim of HIPAA compliance or production readiness.
Development AWS infrastructure was provisioned and checked on October 10, 2026; worker/API activation remains deferred.

---

# Phase 2: the evaluation harness

> **Analogy: a driving test for your pipeline.** Phase 1 built the car. Phase 2 is the
> examiner with a clipboard: same road, same rules, every time, so you can tell whether a
> change (new prompt, new model) actually made the driver better or just *felt* better.

## Quick start

```bash
# 1. A HARD practice set: typo'd NPIs, distractor amounts, missing dates (60 docs)
python -m claimlens gen-data --out data/hard --count 60 --seed 3 --difficulty hard

# 2. Offline demo of the tooling (mock "driver" profiles; NOT real model accuracy)
python -m claimlens compare --data data/hard \
  --config "label=A_naive,llm=mock:naive" \
  --config "label=B_careful,llm=mock:careful"

# 3. Real thing: score your Bedrock model, then A/B a prompt change
export CLAIMLENS_MODEL_ID="<your model id>"
python -m claimlens eval --data data/hard --config "label=base,llm=bedrock"
python -m claimlens compare --data data/hard \
  --config "label=prompt_v1,llm=bedrock,extract_prompt=extract_v1" \
  --config "label=prompt_v2,llm=bedrock,extract_prompt=extract_v2"

# 4. Compare two MODELS (e.g. a small vs a larger one) the same way
python -m claimlens compare --data data/hard \
  --config "label=small,llm=bedrock,model_id=<small id>" \
  --config "label=large,llm=bedrock,model_id=<large id>"
```

Reports land in `results/eval/` as `.md` (human) and `.json` (machines/CI).

## What the numbers mean

| Metric | Plain English |
|---|---|
| **Precision** | When the pipeline gives an answer, how often is it right? |
| **Recall** | Of the answers that exist in the document, how many did it find? |
| **F1** | One blended score of the two. |
| **Auto-accept precision** | Of documents the pipeline marked `validated` (no human looks at them), how many were truly perfect? **The most important number for regulated data.** |
| **Error catch rate** | Of documents that contained *any* error, how many did the pipeline refuse to auto-accept? |
| **Cost per perfect doc** | Total spend / perfect documents. A cheap model that is often wrong can cost *more* per usable result. |

A *wrong* value counts as both a false positive and a false negative (it is a wrong answer
AND a missed right answer). A confident wrong answer is usually worse than a blank.

## The big lesson baked into the demo
Run the offline comparison and look at **auto-accept precision**. Both "drivers" auto-accept
the same number of documents, but one is right about half the time. The rules engine catches
OCR-style NPI typos (the check digit can't be fooled by one wrong digit), but it **cannot**
catch a plausible-looking wrong amount. Rules catch the *impossible*; only evaluation catches
the *plausible but wrong*. That is why both halves of the hybrid design exist.

## Use it as a CI gate
```bash
python -m claimlens eval --data data/hard --config "label=ci,llm=bedrock" \
   --min-f1 0.90 --min-auto-accept-precision 0.99 --min-error-catch-rate 0.95
echo $?   # 0 = pass, 1 = a gate failed (reasons printed to stderr)
```
In Phase 7 this becomes a GitHub Actions step so a prompt change that lowers quality fails the build.

## Good eval habits (interview-worthy)
1. **Fix the dataset, vary one thing.** Compare prompts on the same documents with the same model.
2. **Look at regressions, not just the average.** The compare report lists documents B broke.
3. **Respect sample size.** Under ~30 documents, a 2-document difference is luck. Aim for 50-100+.
4. **Start error analysis with `unexplained` errors.** Errors with no matching injected difficulty are genuine model/prompt bugs.
5. **Do not tune on your only test set.** Once you iterate prompts against `data/hard` many times, generate a fresh set with a different `--seed` for a final check.
6. **Values are hidden in reports by default.** `--show-values` is for synthetic data only. On real data, values could be PHI.

## Suggested exercise
Read the `distractor_amount` errors from the naive run, then read `prompts/extract_v2.txt`.
It adds one rule targeting exactly that failure. Run the real A/B with Bedrock and see whether
the prompt closed the gap, and what it cost in tokens.

---

# Phase 3: the OCR fallback

> **Analogy: the fax pile.** Most mail arrives typed (we can read it directly for free). Some arrives
> as photos or faxes (just pixels, no letters). OCR is the person who reads the pixels and types them
> out, and you have a choice of readers: a free app on your phone (Tesseract), an expensive expert
> service (Textract), or both in sequence (ask the free app first, call the expert only if it's unsure).

## How routing works
```
parse -> text density OK? --yes--> classify/extract...
              |no
              v
        OCR engine configured? --no--> status needs_ocr (stop, spend nothing)
              |yes
              v
        OCR (every scanned file AND every scanned email attachment) -> replace the empty text
              |
        confidence below threshold? -> soft finding -> can NEVER be auto-accepted
```
Per-page density (not total characters) triggers OCR, so a 20-page scan with one typed header line
isn't mistaken for a "text" document.

## Engines (`--ocr` / `ocr=` in configs)
| Spec | What it is | Cost | Notes |
|---|---|---|---|
| `none` | no OCR | $0 | scans stop at `needs_ocr` |
| `tesseract` | local, open source | $0 API (you pay CPU) | needs the Tesseract program + `pytesseract`/`pypdfium2` |
| `textract` | AWS Textract | per page | sync for images/1-page PDFs; **async via S3** for multi-page PDFs (set `CLAIMLENS_TEXTRACT_BUCKET`); with no bucket it splits into single pages |
| `tesseract+textract` | **cascade**: Tesseract first, escalate to Textract if confidence/density is low | pay only for hard pages | the cost-saver |
| `oracle[:rate]` | demo only: reads the true text the generator saved | placeholder | tests plumbing without AWS; `rate` simulates a sloppy engine. Reads no pixels. |

### Textract IAM you will need (Phase 5 Terraform will codify this)
`textract:DetectDocumentText`, `textract:StartDocumentTextDetection`, `textract:GetDocumentTextDetection`;
plus `s3:PutObject/GetObject/DeleteObject` on the temp prefix and `kms:GenerateDataKey/Decrypt` if the bucket
uses a customer-managed key. Production would swap polling for an **SNS completion notification**.
The engine always deletes its temporary S3 object, even when the job fails or times out (tested).

## Try it
```bash
# Scanned documents mixed into a hard set (noisy images, multi-page PDFs, PNGs, scanned email attachments)
python -m claimlens gen-data --out data/scans --count 36 --seed 21 --difficulty hard --scan-fraction 0.4

# Without OCR vs with real local OCR (A/B)
python -m claimlens compare --data data/scans \
  --config "label=no_ocr,llm=mock:careful,ocr=none" \
  --config "label=tesseract,llm=mock:careful,ocr=tesseract"

# With real AWS (needs credentials + a model): Textract alone vs the cheap-first cascade
python -m claimlens compare --data data/scans \
  --config "label=textract,llm=bedrock,ocr=textract" \
  --config "label=cascade,llm=bedrock,ocr=tesseract+textract"
```

## What the real-OCR testing found (and fixed)
Running real Tesseract through the evaluation harness exposed two leaks where wrong data was
marked `validated`:
1. OCR garbled a provider **name** into symbols; names have no check digit, so nothing objected.
   -> new rule `name_plausible` (soft): names with unexpected characters go to review.
2. OCR dropped the **service date** on an appeal; the field is optional for appeals, so the gap was silent.
   -> new rule `ocr_completeness` (soft): after OCR, a missing expected field means "confirm", not "fine".

Result on the same documents: auto-accept precision **86.7% -> 100%**, at the price of fewer documents
auto-accepted (15 -> 13). That trade (a little more human review for far fewer silent errors) is the
core design tension in regulated document processing; be ready to explain it.

## Cascade economics (offline demo; the "expensive" engine is a priced stand-in for Textract)
36 documents, 19 scanned, escalation bar = confidence 92:

| Strategy | Scanned docs exact | Docs auto-accepted | OCR cost |
|---|---|---|---|
| Tesseract only | 68% | 24 | $0.0000 |
| Expensive engine only | 100% | 36 | $0.0420 |
| **Tesseract, escalate if unsure** | **95%** | **35** | **$0.0240** |

The cascade recovered most of the accuracy for roughly 43% less OCR spend. The "expensive" row is a
*perfect* reader, so it is an upper bound; real Textract will not be perfect. Measure your own mix.

## Caveats worth knowing
- Textract is tested against fake clients (pagination, polling, timeout, cleanup, error handling), **not** the live service. Do a smoke test with real AWS before relying on it.
- Tesseract is CPU-heavy (seconds per page). In Phase 5 it belongs on the Fargate worker, not a small Lambda.
- Scanned-document *extraction* scores here come from the offline mock model's simple pattern matching, so only the OCR-side behavior (routing, cost, escalation, rule gating) is meaningful. Re-run with Bedrock for real extraction quality.
- Cost figures use placeholder prices in `llm/pricing.json`. Replace them with current AWS numbers.
- OCR text, like all document text, is never logged.

---

# Phase 4: storage, idempotency, caching and the REST API

> **Analogy: the records department.** Phase 1-3 built the people who read mail. Phase 4 builds the
> records department around them: a **filing cabinet** (Postgres: permanent, searchable), a **coat-check
> ticket** system so two clerks never process the same letter (DynamoDB lease), a **sticky note on the front
> desk** saying "already filed, #123" (Redis: fast but never trusted), and a **front counter** with ID
> checks and a visitor log (the API).

## Run it locally in 2 minutes (no AWS, no Docker)
```bash
export CLAIMLENS_LLM=mock:careful CLAIMLENS_API_AUTH=disabled     # demo mode: mock model, no auth
python -m claimlens db-init                                       # creates claimlens.db (SQLite)
python -m claimlens gen-data --out data/samples --count 6
python -m claimlens serve                                         # http://127.0.0.1:8080/docs

curl -F "file=@data/samples/01_surprise_billing_complaint.docx" localhost:8080/documents   # 201 created
curl -F "file=@data/samples/01_surprise_billing_complaint.docx" localhost:8080/documents   # 200 duplicate, no model call
curl localhost:8080/claims                                        # the human review queue
```
For anything real: Postgres + tokens (the API **refuses to start** with no tokens unless you explicitly disable auth):
```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"      # make tokens
export CLAIMLENS_API_TOKENS="<tok1>:rita:reader,<tok2>:rob:reviewer,<tok3>:ada:admin"
export CLAIMLENS_DATABASE_URL=postgresql+psycopg://claimlens:claimlens@localhost:5432/claimlens
export CLAIMLENS_REDIS_URL=redis://localhost:6379/0
curl -H "Authorization: Bearer <tok2>" localhost:8080/claims
```
`docker-compose.yml` starts Postgres, Redis and DynamoDB Local (written but untested here: see caveats).

## The API
| Endpoint | Role | Notes |
|---|---|---|
| `POST /documents` | uploader | multipart `file`. **201** created, **200** duplicate, **202** another request is processing it (`Retry-After`), **503** transient failure (`Retry-After`), 413 too big, 415 bad type |
| `GET /documents/{id}` | reader | audited |
| `GET /documents?status=&limit=&cursor=` | reader | keyset pagination |
| `GET /claims` | reader | the review queue (`status=needs_review` by default) |
| `POST /documents/{id}/review` | reviewer | `{decision, expected_version, corrections, note}` |
| `GET /healthz`, `GET /readyz` | none | liveness / readiness (DB check) |

Roles are ordered: reader < uploader < reviewer < admin. Responses carry `Cache-Control: no-store`.
Every upload, read, list and review writes an **audit row** (who, what, when). No document text is ever stored.

## Three layers of duplicate protection (each has a different job)
| Layer | Job | If it is wrong or down |
|---|---|---|
| **Redis** | fast path: "seen this exact file + recipe?" skips DB and lease | fails open: treated as a miss, nothing breaks |
| **DynamoDB lease** | only ONE worker processes a document at a time; a crashed worker's lease expires so another may take over; a *late* old worker is fenced out and cannot overwrite the newer result | contract-tested against the SQL twin |
| **Postgres `UNIQUE(content_sha256, fingerprint)`** | the database refuses a second copy no matter what | last line of defence |

**"Same document" = same bytes + same recipe.** The *fingerprint* hashes model IDs, the prompt **text**, the OCR
engine and `RULES_VERSION`. Re-uploading an unchanged file costs nothing; after you improve a prompt the same file
is *correctly* reprocessed (that's what makes prompt A/B history possible).

**Retry semantics.** Errors are tagged `transient` (Bedrock throttling, timeouts) or `permanent` (corrupt file).
Transient failures save **nothing**, release the lease, and answer 503 + `Retry-After`, so a retry works.
Permanent failures are recorded once (status `error`) so a bad file is not retried forever.

## Human review (and how it feeds evaluation)
`POST /documents/{id}/review` approves (optionally correcting fields) or rejects.
- **Humans may override soft concerns** ("please confirm this date is really absent") **but not provably invalid data** (bad NPI check digit, future date) and cannot approve with a required field empty. The 422 names rules and fields, never values.
- **Optimistic locking:** you send the `version` you loaded. If a colleague changed the document first you get **409**, not a silent overwrite.
- Approvals become answer keys: `python -m claimlens export-ground-truth --out data/reviewed` writes files the Phase 2 evaluator reads. **Model is wrong -> human fixes -> the fix becomes a test case.**

## Decisions worth being able to explain
- **Why not cache payer normalization in Redis?** It's a microsecond in-process function; a network hop to Redis would be slower. Redis earns its place only where it skips real work (a database + lease + model call).
- **Why a DynamoDB lease *and* a unique constraint?** Different failure modes: the lease prevents wasted *work* (two model bills); the constraint prevents wrong *data*.
- **Why store extracted fields as columns, not just JSON?** So you can filter, join and aggregate in SQL (Phase 8's Athena/analytics goal). The raw model output is kept as JSON for audit/eval.
- **Money is `NUMERIC(12,2)`** but read back as float in this learning project; a real billing system would keep `Decimal` end to end.
- **Filenames can contain PHI.** New uploads and stored `source_name` values use opaque IDs by default.
  Existing rows are not silently rewritten. `CLAIMLENS_OPAQUE_SOURCE_NAMES=0` is a local synthetic-demo escape hatch only.

## Caveats (read these)
- **DynamoDB's atomicity is NOT proven by these tests.** Experiment showed moto's in-process emulator lets two threads "win" a conditional write ~1 time in 40 under contention (even for the simplest `attribute_not_exists` condition), so the DynamoDB contract tests serialize calls and validate *our condition logic*. Real DynamoDB makes conditional writes atomic; to verify against a real implementation run `CLAIMLENS_TEST_DDB_ENDPOINT=http://localhost:8000 pytest tests/test_idempotency.py -k true_dynamodb` (DynamoDB Local), or the same code against a dev AWS account. The SQL backend's 16-thread race *is* tested for real on Postgres.
- `docker-compose.yml` is untested. Postgres, Redis and the DynamoDB wiring were verified natively and over HTTP (moto server).
- `db-init` remains a local/test bootstrap. Deployments now use **Alembic migrations** via `db-migrate`.
  Back up legacy databases before the explicit `db-migrate --adopt-legacy` transition.
- Auth supports static bearer tokens or RS256 OIDC ID tokens with pinned issuer/audience/JWKS and explicit roles.
  Production OIDC needs private-network reachability to the identity provider. There is no rate limiting yet.
- `POST /documents` processes synchronously for local demos. Production disables it and uses presigned S3 upload + queue + worker.
- Running the Postgres tests: `export CLAIMLENS_TEST_PG_URL=postgresql+psycopg://user:pass@localhost/dbname` (the tests drop and recreate tables in that database, so use a throwaway one).

---

# Phase 5: deploying to AWS (Terraform + Fargate worker)

> **Analogy: moving the records department into a secured building.** The building has no street door
> (no internet gateway); everything arrives through badge-controlled service hatches (VPC endpoints). Uploads go
> to a locked mailbox (S3) that rings a bell (EventBridge) and drops a ticket on a rail (SQS). Clerks (Fargate workers)
> take tickets, keep telling the rail "still working" (heartbeat), and tickets that keep failing go to a
> problem pile (dead-letter queue) with an alarm on it.

```
browser --POST /uploads--> API (internal ALB) --> short-lived, size-limited presigned S3 POST
browser --file--> S3 (KMS, private) --event--> EventBridge (prefix filter) --> SQS --> Fargate worker
                                                          |                        |-- Textract / Tesseract (OCR)
                                                    DLQ + alarms                   |-- Bedrock (classify + extract)
                                                                                   |-- DynamoDB lease, Redis fast path
browser --GET /uploads/{id}--> processed | duplicate | failed  --> GET /documents/{id}    +-- Postgres (RDS)
```

## What is in the box
| Path | What |
|---|---|
| `claimlens/worker.py` | SQS consumer: concurrency, visibility **heartbeat**, backoff tied to receive count, graceful SIGTERM, no keys in logs |
| `Dockerfile`, `.dockerignore` | one image, non-root, read-only-friendly; command picks `worker` or `serve` |
| `infra/terraform/` | the whole stack (flat root module, one file per concern, 100+ resources) |
| `infra/state-bootstrap/` | separately managed private, versioned, KMS-encrypted Terraform state bucket |
| `scripts/deploy.sh` | `bootstrap` -> `release` (git-SHA tag) -> `migrate`; refuses to ship uncommitted code |
| `tests/infra/` | policy-as-code tests over application and state-bootstrap HCL |
| `tests/test_worker.py` | 45 worker tests incl. real boto3 S3+SQS (moto) and a poison message reaching the DLQ |

## Design decisions worth being able to defend
- **No internet path at all.** No IGW, no NAT. Nine interface endpoints (ECR x2, Logs, SQS, KMS, STS, Secrets Manager, Bedrock, Textract) plus S3/DynamoDB gateway endpoints. The Gemini draft created only a Bedrock endpoint, so its Lambda could not have reached S3, DynamoDB, Textract or KMS.
- **No `0.0.0.0/0` anywhere.** Security-group egress is allow-listed by destination (endpoint SG, S3/DynamoDB prefix lists, DB SG, Redis SG). A test fails the build if one appears.
- **Fargate worker, not Lambda.** OCR jobs exceed Lambda's 15-minute cap and Tesseract is CPU-heavy; the heartbeat keeps long jobs invisible to other workers.
- **Queue-depth autoscaling, not CPU.** Workers mostly wait on Bedrock/Textract, so CPU stays low while a backlog grows. Scale-out when waiting messages >= `scale_out_backlog`; scale-in only when waiting **plus in-flight** has been 0 for 15 minutes.
- **Worker retry policy.** Transient failures requeue with 30s/60s/120s/... backoff based on SQS's delivery count; after `max_receive_count` deliveries SQS itself moves the message to the DLQ. Things that can never succeed (malformed event, missing object, wrong type, too big) are deleted/quarantined immediately.
- **Three IAM roles, none with wildcards.** Execution role (pull image, logs, inject secrets), worker task role, API task role. The API role cannot read documents or call a model; the worker may delete only its temporary OCR copies, never originals. The only `Resource: "*"` are `ecr:GetAuthorizationToken` and Textract, which AWS cannot scope; a test enforces that allow-list.
- **Secrets never touch code or tfvars.** RDS manages its own master password (`manage_master_user_password`); ECS injects it by reference; the API-token secret is created empty and filled out-of-band.
- **Immutable image tags.** `:latest` is impossible by design; every release is a git SHA; the circuit breaker rolls back a bad image.
- **Pinned AZs.** Zones are an explicit variable: auto-discovery could silently reshuffle subnets (and so the database) if AWS changes the zone list. (A scanner check flagged this, and it was right.)
- **Two-phase rollout.** The ECR repo must exist before an image can be pushed and services need an image, so `enable_services` starts false.

## Deploy
Use Terraform **1.10+** (CI uses **1.16.5**) for S3-native state locking. Provision state storage with
[`infra/state-bootstrap/README.md`](infra/state-bootstrap/README.md), then keep backend settings in a protected
private file. Set `allowed_account_ids` in both the backend settings and deployment variables before planning.

```bash
cp infra/terraform/envs/dev.tfvars.example infra/terraform/envs/dev.tfvars   # edit: account, AZs, approved model ARNs
cp infra/terraform/backend.tf.example infra/terraform/backend.tf
# Copy backend.hcl.example to a protected location, fill it using state-bootstrap outputs, then:
terraform -chdir=infra/terraform init -backend-config=/absolute/private/path/backend.hcl
./scripts/deploy.sh bootstrap     # everything except the services
# Stop here for an infrastructure-only deployment; the following commands require separate release approval.
./scripts/deploy.sh release       # build + push :<git-sha>, then enable the services
./scripts/deploy.sh migrate       # create database tables (once)
```

The optional private API additionally requires an issued ACM certificate, allowed client CIDRs and actual
network connectivity. Configure bearer tokens through an approved Secrets Manager workflow, not command-line
arguments, or configure OIDC with reachable private JWKS networking. See [`docs/operations.md`](docs/operations.md).

## READ THIS: what was and was NOT verified
On October 10, 2026, the development infrastructure was planned and applied against AWS:
**9 state-bootstrap and 102 application Terraform additions**, no updates or deletions.
PostgreSQL and Redis were available, all 11 VPC endpoints were available, CloudTrail delivered logs,
and remote state used versioned SSE-KMS storage with locking. The final application plan reported **no changes**.

Worker, API and analytics remain disabled. No application image was published to ECR and no migration task
was run. Upload-to-queue delivery, live Bedrock/Textract processing, identity-provider networking and
end-to-end application behavior still need the activation checks. Hosted deployment/model-evaluation workflows
and account-specific GitHub OIDC roles have not been configured or verified.

Historical Phase 5 checks (scanner counts below were not re-run for Phases 6–8):
- `tofu fmt` clean; all files parse (python-hcl2); every `resource.name`, `var.x`, `local.x` reference resolves to something declared (a typo-catcher standing in for `validate`).
- 44 policy tests: encryption, no open CIDRs, no internet path, least-privilege IAM, hardening, secrets by reference, safe defaults.
- `checkov`: **314 passed, 0 failed, 12 skipped** (each skip is documented inline with its reason; see "Known gaps"). The Dockerfile passes 64/64.
- The worker against real boto3 S3 + SQS (moto), including DLQ redrive, heartbeat, backoff values, URL-encoded keys, shutdown.
- A clean venv with only `requirements.txt` imports everything the image runs.

### First-deploy checklist (the things only a real deploy can confirm)
1. `terraform validate` then `plan`. Fix any provider-schema errors.
2. **Upload events reach the queue.** Upload a file, then check the queue within a minute. If not: the `eventbridge-delivery-failing` alarm / EventBridge `FailedInvocations` metric. The KMS key policy lets `events.amazonaws.com` use the key with a `kms:ViaService = sqs...` condition; that is the most likely cross-service detail to need adjusting. Also confirm whether keys in the event arrive URL-encoded (the worker tries both forms).
3. **Bedrock:** model access enabled in the console; ARNs match (cross-region inference profiles need the underlying model ARNs in each destination region too).
4. **Textract async:** run a multi-page scanned PDF. It reads the temporary S3 copy *as the worker role*.
5. **Read-only root filesystem:** only `/tmp` is writable. If a library writes elsewhere the container will crash on first use; check the worker log group.
6. **Health check:** the task should turn HEALTHY within ~90s (it reads the worker's heartbeat file).
7. Run `migrate` before the first upload. Set the API-token secret before enabling the API.
8. Run the DynamoDB race test against real DynamoDB (see Phase 4 caveats).

### Known gaps (honest list)
- **Secret rotation** is not configured (Redis token, API tokens). The RDS-managed password does rotate if you enable it. Needs a rotation function and a rollout plan.
- The app connects as the **database master user**. Production should use a least-privilege app user (or IAM database auth, already enabled on the instance).
- **One application-data KMS key**, plus a separate Terraform-state key. Per-service data keys are a valid hardening step.
- S3/DynamoDB gateway endpoint policies are now scoped. Interface endpoint policies and WAF remain production hardening tasks.
  There is no public exposure path (deliberate).
- CI/CD workflows, a dashboard, application alarms and Alembic migrations are now present.
  Required reviewers, OIDC trust, environment variables and live service tests must be configured by an operator.
- Single region, no cross-region replication or disaster-recovery plan (a deliberate decision to make, not a default).
- `original_retention_days` (30) is a **policy placeholder**: set it to your real retention requirement.
- **Cost:** nine interface endpoints across two AZs cost about $131/month at the checked us-east-1 hourly rate.
  The infrastructure-only development configuration totals roughly **$210/month plus usage and taxes**;
  Fargate/API activation adds more. An estimate or Budget alert is not a spending cap. Set a Budget and alert recipient.
  Teardown requires separate approval, deliberate changes to deletion guards, and a plan for versioned data and backups.
- Tesseract throughput per Fargate vCPU is unmeasured; size `worker_cpu`/`worker_max_capacity` from a real load test, and mind Bedrock/Textract rate limits when raising the ceiling.
