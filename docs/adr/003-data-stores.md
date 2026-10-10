# ADR 003: SQL as record of truth, DynamoDB for leases, Redis for acceleration

**Status:** accepted.

Postgres provides structured fields, review history, audit rows, unique constraints and optimistic locking.
DynamoDB coordinates work ownership; Redis avoids unnecessary lookups/model work but fails open.
The same bytes plus processing recipe identify a result, allowing changed prompts/models to produce history.

Consequences: multiple services have different failure modes, all need contract tests. Redis is never authoritative.
Production data-schema changes use versioned migrations; static/moto tests do not prove live DynamoDB atomicity.
