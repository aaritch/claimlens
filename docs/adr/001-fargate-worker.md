# ADR 001: Fargate worker instead of Lambda for document processing

**Status:** accepted.

OCR is CPU-heavy and asynchronous Textract polling can outlast Lambda's 15-minute limit.
Keep SQS consumption and processing in Fargate, with visibility heartbeats and graceful shutdown.
Scale on queue backlog, not CPU, since workers spend much of their time waiting on model/OCR calls.

Consequences: always-on task/endpoints cost, explicit task sizing, measured concurrency and queue monitoring.
Step Functions remains a possible alternative if per-step orchestration is needed, not a second active pipeline.
