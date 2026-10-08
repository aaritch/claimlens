import json
from datetime import timezone

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from claimlens.config import get_settings
from claimlens.eval.runner import load_ground_truth
from claimlens.models import DocType
from claimlens.storage import repository as repo
from claimlens.storage.db import AuditLog, Document, ReviewEvent
from tests.conftest import make_result

S = get_settings()


def save(session, n=0, **kw):
    doc = repo.save_result(session, kw.pop("result", None) or make_result(), content_sha256=f"{n:064d}",
                           fingerprint=kw.pop("fp", "fp1"), source_name=kw.pop("name", f"f{n}.pdf"))
    session.commit()
    return doc


def test_roundtrip_preserves_types(session_factory):
    with session_factory() as s:
        doc = save(s)
        loaded = repo.get_document(s, doc.id)
        s.expire_all()
        loaded = repo.get_document(s, doc.id)
        assert loaded.service_date.isoformat() == "2024-03-04"
        assert loaded.disputed_amount == 1450.5 and loaded.provider_npi == "1234567893"
        assert loaded.created_at.tzinfo is not None and loaded.created_at.utcoffset() == timezone.utc.utcoffset(None)
        assert loaded.findings[0]["rule"] == "required_field" and loaded.prompt_versions["extract"] == "extract_v1"
        assert loaded.raw_claim["payer_name"] == "Aetna" and loaded.version == 1


def test_database_itself_refuses_duplicate_document_and_recipe(session_factory):
    with session_factory() as s:
        save(s, 1)
        with pytest.raises(IntegrityError):
            save(s, 1)
        s.rollback()
        save(s, 1, fp="a-new-recipe")  # same bytes, different recipe: allowed
        assert s.scalar(select(func.count()).select_from(Document)) == 2


def test_find_by_hash_respects_fingerprint(session_factory):
    with session_factory() as s:
        doc = save(s, 5)
        assert repo.find_by_hash(s, f"{5:064d}", "fp1").id == doc.id
        assert repo.find_by_hash(s, f"{5:064d}", "other") is None


def test_keyset_pagination_is_complete_ordered_and_stable(session_factory):
    with session_factory() as s:
        ids = [save(s, i).id for i in range(7)]
        # force ties on created_at: the tie-breaker (id) must still give a deterministic, gap-free order
        s.execute(text("update documents set created_at = (select created_at from documents order by id limit 1) where 1=1"))
        s.commit()
        seen, cursor = [], None
        while True:
            page, cursor = repo.list_documents(s, limit=3, cursor=cursor)
            seen += [d.id for d in page]
            if not cursor:
                break
        assert sorted(seen) == sorted(ids) and len(seen) == len(set(seen)) == 7
        assert seen == sorted(seen, reverse=True)  # all tied, so ordered by id desc


def test_pagination_newest_first_and_status_filter(session_factory):
    import time
    with session_factory() as s:
        for i in range(4):
            save(s, i, result=make_result(status="validated" if i % 2 else "needs_review"))
            time.sleep(0.01)
        page, _ = repo.list_documents(s)
        assert [d.source_name for d in page] == ["f3.pdf", "f2.pdf", "f1.pdf", "f0.pdf"]
        queue, _ = repo.list_documents(s, status="needs_review")
        assert {d.source_name for d in queue} == {"f0.pdf", "f2.pdf"}


def test_bad_cursor_is_rejected(session_factory):
    with session_factory() as s, pytest.raises(ValueError):
        repo.list_documents(s, cursor="not-a-cursor")


def test_audit_rows_are_written(session_factory):
    with session_factory() as s:
        repo.audit(s, "alice", "view", "doc-1", {"why": "test"})
        s.commit()
        row = s.scalars(select(AuditLog)).one()
        assert (row.actor, row.action, row.document_id, row.detail) == ("alice", "view", "doc-1", {"why": "test"})


# ---- human review -----------------------------------------------------------------------------------

def needs_review_doc(s, n=0, **claim):
    return save(s, n, result=make_result(**claim))


def test_approve_with_correction_updates_fields_and_records_history(session_factory):
    with session_factory() as s:
        doc = needs_review_doc(s, disputed_amount=None)
        out = repo.apply_review(s, doc.id, "rita", "approve", S, expected_version=1, corrections={"disputed_amount": 980.25})
        s.commit()
        assert (out.status, out.disputed_amount, out.reviewed_by, out.version) == ("validated", 980.25, "rita", 2)
        ev = s.scalars(select(ReviewEvent)).one()
        assert ev.changes == {"disputed_amount": [None, 980.25]} and ev.prior_status == "needs_review"
        assert s.scalars(select(AuditLog).where(AuditLog.action == "review_approve")).one().detail == {"changed_fields": ["disputed_amount"]}


def test_approve_without_changes_confirms_soft_concerns(session_factory):
    with session_factory() as s:
        doc = save(s, 1, result=make_result(service_date=None))  # soft: absent after OCR, human confirms it's really absent
        out = repo.apply_review(s, doc.id, "rita", "approve", S, expected_version=1)
        assert out.status == "validated"


@pytest.mark.parametrize("corrections,rule", [
    ({"provider_npi": "1234567894"}, "npi_valid"),            # fails check digit: humans can't override provable errors
    ({"service_date": "2999-01-01"}, "date_not_future"),
    ({"disputed_amount": -5}, "amount_positive"),
    ({"claim_reference": None}, "required_field"),            # cannot approve with a required field empty
])
def test_review_cannot_approve_provably_bad_data(session_factory, corrections, rule):
    with session_factory() as s:
        doc = needs_review_doc(s)
        with pytest.raises(repo.CorrectionInvalid) as info:
            repo.apply_review(s, doc.id, "rita", "approve", S, expected_version=1, corrections=corrections)
        assert rule in [p.get("rule") for p in info.value.problems]
        s.rollback()
        assert repo.get_document(s, doc.id).status == "needs_review"


def test_unknown_fields_and_bad_types_rejected_without_leaking_values(session_factory):
    with session_factory() as s:
        doc = needs_review_doc(s)
        with pytest.raises(repo.CorrectionInvalid) as a:
            repo.apply_review(s, doc.id, "r", "approve", S, expected_version=1, corrections={"favourite_colour": "teal"})
        assert a.value.problems == [{"field": "favourite_colour", "rule": "unknown_field"}]
        with pytest.raises(repo.CorrectionInvalid) as b:
            repo.apply_review(s, doc.id, "r", "approve", S, expected_version=1, corrections={"disputed_amount": "SECRET-abc"})
        assert "SECRET" not in json.dumps(b.value.problems) and b.value.problems[0]["rule"] == "invalid_value"


def test_reject_needs_a_note_and_sets_status(session_factory):
    with session_factory() as s:
        doc = needs_review_doc(s)
        with pytest.raises(repo.CorrectionInvalid):
            repo.apply_review(s, doc.id, "r", "reject", S, expected_version=1)
        out = repo.apply_review(s, doc.id, "r", "reject", S, expected_version=1, note="duplicate of another claim")
        assert out.status == "rejected"


def test_unrecognized_document_needs_a_doc_type_to_approve(session_factory):
    with session_factory() as s:
        doc = save(s, 1, result=make_result(doc_type=DocType.OTHER))
        with pytest.raises(repo.CorrectionInvalid) as info:
            repo.apply_review(s, doc.id, "r", "approve", S, expected_version=1)
        assert info.value.problems[0]["rule"] == "doc_type_required"
        out = repo.apply_review(s, doc.id, "r", "approve", S, expected_version=1, corrections={"doc_type": "provider_appeal"})
        assert out.doc_type == "provider_appeal" and out.status == "validated"


def test_wrong_status_not_found_and_bad_decision(session_factory):
    with session_factory() as s:
        ok = save(s, 1, result=make_result(status="validated"))
        with pytest.raises(repo.ReviewNotAllowed):
            repo.apply_review(s, ok.id, "r", "approve", S, expected_version=1)
        with pytest.raises(repo.NotFound):
            repo.apply_review(s, "nope", "r", "approve", S, expected_version=1)
        pending = needs_review_doc(s, 2)
        with pytest.raises(repo.CorrectionInvalid):
            repo.apply_review(s, pending.id, "r", "maybe", S, expected_version=1)


def test_second_reviewer_with_stale_version_gets_a_conflict(session_factory):
    with session_factory() as s:
        doc = needs_review_doc(s)
        repo.apply_review(s, doc.id, "first", "approve", S, expected_version=1)
        s.commit()
        with pytest.raises(repo.VersionConflict):
            repo.apply_review(s, doc.id, "second", "approve", S, expected_version=1)


def test_optimistic_lock_catches_a_write_that_slipped_in_between(session_factory):
    """Reviewer B already holds a loaded copy; reviewer A commits first; B's UPDATE must NOT silently overwrite."""
    with session_factory() as a, session_factory() as b:
        doc = needs_review_doc(a)
        stale = repo.get_document(b, doc.id)            # B loaded version 1
        assert stale.version == 1
        repo.apply_review(a, doc.id, "A", "approve", S, expected_version=1)
        a.commit()
        with pytest.raises(repo.VersionConflict):
            repo.apply_review(b, doc.id, "B", "reject", S, expected_version=1, note="too late")
        b.rollback()
        assert repo.get_document(a, doc.id).reviewed_by == "A"


def test_export_ground_truth_feeds_the_evaluator(session_factory, tmp_path):
    with session_factory() as s:
        fixed = needs_review_doc(s, 1, disputed_amount=None)
        repo.apply_review(s, fixed.id, "rita", "approve", S, expected_version=1, corrections={"disputed_amount": 1111.11})
        rejected = needs_review_doc(s, 2)
        repo.apply_review(s, rejected.id, "rita", "reject", S, expected_version=1, note="not ours")
        s.commit()
        assert repo.export_ground_truth(s, tmp_path) == 1   # only human-APPROVED documents become answer keys
    truths = load_ground_truth(tmp_path)
    assert truths[0]["expected"]["disputed_amount"] == 1111.11 and truths[0]["doc_type"] == "provider_appeal"
    assert truths[0]["difficulty"] == "human_reviewed"


def test_api_dict_has_no_document_text_fields(session_factory):
    with session_factory() as s:
        d = repo.document_to_dict(needs_review_doc(s))
        assert not {"text", "content", "body", "raw_text"} & set(d)
        assert d["claim"]["provider_npi"] == "1234567893"
