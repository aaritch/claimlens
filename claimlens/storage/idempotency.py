"""Idempotency: "make sure this exact work happens at most once, even if two workers race."

Analogy: a coat-check ticket with an expiry. A worker takes the ticket for a document (a LEASE).
Nobody else may process it while the lease is valid. If the worker crashes, the lease expires and
someone else may take over. If the original worker comes back late, its stale ticket is refused
(fencing): it cannot overwrite the newer worker's result.

Two interchangeable backends:
  * DynamoIdempotencyStore: production (conditional writes give atomicity across many workers)
  * SqlIdempotencyStore:    local/dev (same contract, no AWS needed)
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

import boto3
from botocore.exceptions import ClientError
from sqlalchemy import and_, or_, update
from sqlalchemy.exc import IntegrityError

from claimlens.storage.db import IdempotencyRow

IN_PROGRESS, DONE, FAILED = "IN_PROGRESS", "DONE", "FAILED"
RECORD_TTL_SECONDS = 30 * 24 * 3600  # DynamoDB TTL cleans old records automatically


@dataclass
class Lease:
    acquired: bool
    status: Optional[str]          # status of the record (when not acquired: why not)
    document_id: Optional[str]     # set when status == DONE
    owner: str


class IdempotencyStore(Protocol):
    def begin(self, key: str, owner: str, lease_seconds: int, now: Optional[float] = None) -> Lease: ...
    def complete(self, key: str, owner: str, document_id: str, now: Optional[float] = None) -> bool: ...
    def fail(self, key: str, owner: str, error: str, now: Optional[float] = None) -> bool: ...


class DynamoIdempotencyStore:
    """Table schema: partition key `pk` (string). Enable TTL on attribute `expires_at`."""

    def __init__(self, table_name: str, region: str = "us-east-1", endpoint_url: str = "", resource=None):
        ddb = resource or boto3.resource("dynamodb", region_name=region, endpoint_url=endpoint_url or None)
        self._table = ddb.Table(table_name)

    @staticmethod
    def _is_conditional_failure(exc: ClientError) -> bool:
        return exc.response["Error"]["Code"] == "ConditionalCheckFailedException"

    def begin(self, key, owner, lease_seconds, now=None) -> Lease:
        now = int(now if now is not None else time.time())
        try:
            self._table.update_item(
                Key={"pk": key},
                UpdateExpression=("SET #s = :inprog, #o = :owner, lease_expires = :lease, updated_at = :now, "
                                  "expires_at = :ttl, attempts = if_not_exists(attempts, :zero) + :one REMOVE #err"),
                # Atomic: succeeds only if new, previously failed, or the previous owner's lease ran out.
                ConditionExpression="attribute_not_exists(pk) OR #s = :failed OR (#s = :inprog AND lease_expires < :now)",
                ExpressionAttributeNames={"#s": "status", "#o": "owner", "#err": "error"},
                ExpressionAttributeValues={":inprog": IN_PROGRESS, ":failed": FAILED, ":owner": owner,
                                           ":lease": now + lease_seconds, ":now": now, ":ttl": now + RECORD_TTL_SECONDS,
                                           ":zero": 0, ":one": 1},
            )
            return Lease(True, IN_PROGRESS, None, owner)
        except ClientError as exc:
            if not self._is_conditional_failure(exc):
                raise
        item = self._table.get_item(Key={"pk": key}, ConsistentRead=True).get("Item", {})
        return Lease(False, item.get("status"), item.get("document_id"), owner)

    def _finish(self, key, owner, now, set_expr, values, names=None) -> bool:
        now = int(now if now is not None else time.time())
        try:
            self._table.update_item(
                Key={"pk": key}, UpdateExpression=set_expr,
                # Fencing: only the CURRENT owner of an in-progress lease may finish it.
                ConditionExpression="#o = :owner AND #s = :inprog",
                ExpressionAttributeNames={"#s": "status", "#o": "owner", **(names or {})},
                ExpressionAttributeValues={":owner": owner, ":inprog": IN_PROGRESS, ":now": now, **values},
            )
            return True
        except ClientError as exc:
            if self._is_conditional_failure(exc):
                return False
            raise

    def complete(self, key, owner, document_id, now=None) -> bool:
        return self._finish(key, owner, now, "SET #s = :done, document_id = :doc, updated_at = :now",
                            {":done": DONE, ":doc": document_id})

    def fail(self, key, owner, error, now=None) -> bool:
        return self._finish(key, owner, now, "SET #s = :failed, #err = :e, updated_at = :now",
                            {":failed": FAILED, ":e": error[:500]}, names={"#err": "error"})


class SqlIdempotencyStore:
    """Same contract on top of the SQL database (uses atomic UPDATE ... WHERE, the SQL cousin of a conditional write)."""

    def __init__(self, session_factory: Callable):
        self._sf = session_factory

    def begin(self, key, owner, lease_seconds, now=None) -> Lease:
        now = int(now if now is not None else time.time())
        with self._sf() as s:
            try:
                s.add(IdempotencyRow(key=key, status=IN_PROGRESS, owner=owner, lease_expires=now + lease_seconds,
                                     attempts=1, updated_at=now))
                s.commit()
                return Lease(True, IN_PROGRESS, None, owner)
            except IntegrityError:
                s.rollback()
            claimed = s.execute(
                update(IdempotencyRow)
                .where(IdempotencyRow.key == key,
                       or_(IdempotencyRow.status == FAILED,
                           and_(IdempotencyRow.status == IN_PROGRESS, IdempotencyRow.lease_expires < now)))
                .values(status=IN_PROGRESS, owner=owner, lease_expires=now + lease_seconds, updated_at=now,
                        attempts=IdempotencyRow.attempts + 1, error=None)
            ).rowcount
            s.commit()
            if claimed == 1:
                return Lease(True, IN_PROGRESS, None, owner)
            row = s.get(IdempotencyRow, key)
            return Lease(False, row.status if row else None, row.document_id if row else None, owner)

    def _finish(self, key, owner, now, **values) -> bool:
        now = int(now if now is not None else time.time())
        with self._sf() as s:
            n = s.execute(update(IdempotencyRow)
                          .where(IdempotencyRow.key == key, IdempotencyRow.owner == owner, IdempotencyRow.status == IN_PROGRESS)
                          .values(updated_at=now, **values)).rowcount
            s.commit()
            return n == 1

    def complete(self, key, owner, document_id, now=None) -> bool:
        return self._finish(key, owner, now, status=DONE, document_id=document_id)

    def fail(self, key, owner, error, now=None) -> bool:
        return self._finish(key, owner, now, status=FAILED, error=error[:500])


def create_dynamodb_table(table_name: str, region: str = "us-east-1", endpoint_url: str = "", resource=None) -> None:
    """Create the table for local/dev use. In production Terraform (Phase 5) owns this."""
    ddb = resource or boto3.resource("dynamodb", region_name=region, endpoint_url=endpoint_url or None)
    try:
        ddb.create_table(TableName=table_name, BillingMode="PAY_PER_REQUEST",
                         AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
                         KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}])
        ddb.meta.client.get_waiter("table_exists").wait(TableName=table_name)
        try:
            ddb.meta.client.update_time_to_live(TableName=table_name,
                                                TimeToLiveSpecification={"Enabled": True, "AttributeName": "expires_at"})
        except ClientError:
            pass  # some local emulators don't implement TTL
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceInUseException":
            raise
