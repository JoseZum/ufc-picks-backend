from pathlib import Path

import pytest

from app.modules.missions.domain import (
    MISSING_VALUE,
    ReconciliationAction,
    ReconciliationBlocker,
    ReconciliationCandidate,
    ReconciliationEntityType,
    ReconciliationImpact,
    ReconciliationInputError,
    ReconciliationScope,
    build_reconciliation_preview,
)


def candidate(
    entity_type,
    entity_id,
    *,
    current,
    desired,
    fields=("status", "revision"),
    expected_revision=None,
    current_revision=None,
    impact=ReconciliationImpact.ASSIGNMENT_STATE,
):
    return ReconciliationCandidate(
        entity_type=entity_type,
        entity_id=entity_id,
        impact=impact,
        owned_fields=fields,
        current=current,
        desired=desired,
        expected_revision=expected_revision,
        current_revision=current_revision,
    )


def test_preview_classifies_insert_update_and_unchanged_without_delete():
    candidates = (
        candidate(
            ReconciliationEntityType.ASSIGNMENT,
            "assignment-1",
            current={"status": "ACTIVE", "revision": 1, "legacy": "preserved"},
            desired={"status": "COMPLETED", "revision": 2},
            expected_revision=1,
            current_revision=1,
        ),
        candidate(
            ReconciliationEntityType.XP_LEDGER_ENTRY,
            "xp-new",
            current=None,
            desired={"amount": 6, "source_id": "assignment-1"},
            fields=("amount", "source_id"),
            impact=ReconciliationImpact.XP_AWARD,
        ),
        candidate(
            ReconciliationEntityType.USER_PROGRESSION,
            "jose",
            current={"level": 2, "lifetime_xp": 6},
            desired={"level": 2, "lifetime_xp": 6},
            fields=("level", "lifetime_xp"),
            impact=ReconciliationImpact.PROGRESSION_REBUILD,
        ),
    )

    preview = build_reconciliation_preview(
        scope=ReconciliationScope(event_id=4242, user_id="jose"),
        candidates=candidates,
    )

    assert [operation.action for operation in preview.operations] == [
        ReconciliationAction.UPDATE,
        ReconciliationAction.INSERT,
    ]
    assert preview.operations[0].changed_fields == ("revision", "status")
    assert preview.operations[0].before == {"revision": 1, "status": "ACTIVE"}
    assert "legacy" not in preview.operations[0].before
    assert preview.unchanged_entities == ("USER_PROGRESSION:jose",)
    assert preview.summary() == {
        "insert_count": 1,
        "update_count": 1,
        "unchanged_count": 1,
        "blocker_count": 0,
        "safe_to_apply": True,
        "converged": False,
    }
    assert {action.value for action in ReconciliationAction} == {"INSERT", "UPDATE"}


def test_preview_is_content_addressed_and_independent_of_candidate_order():
    first = candidate(
        ReconciliationEntityType.ASSIGNMENT,
        "assignment-1",
        current={"status": "ACTIVE", "revision": 1},
        desired={"status": "FAILED", "revision": 2},
    )
    second = candidate(
        ReconciliationEntityType.CELEBRATION,
        "celebration-1",
        current=None,
        desired={"status": "PENDING", "revision": 1},
        impact=ReconciliationImpact.CELEBRATION_QUEUE,
    )
    scope = ReconciliationScope(assignment_id="assignment-1")

    forward = build_reconciliation_preview(
        scope=scope,
        candidates=(first, second),
    )
    reverse = build_reconciliation_preview(
        scope=scope,
        candidates=(second, first),
    )

    assert forward == reverse
    assert forward.plan_id.startswith("mission_reconcile_")
    assert all(
        operation.operation_id.startswith("reconcile_op_")
        for operation in forward.operations
    )


def test_missing_current_field_is_explicit_not_equal_to_none():
    preview = build_reconciliation_preview(
        scope=ReconciliationScope(user_id="jose"),
        candidates=(
            candidate(
                ReconciliationEntityType.USER_PROGRESSION,
                "jose",
                current={"level": 1},
                desired={"level": 1, "title": None},
                fields=("level", "title"),
                impact=ReconciliationImpact.PROGRESSION_REBUILD,
            ),
        ),
    )

    operation = preview.operations[0]
    assert operation.changed_fields == ("title",)
    assert operation.before["title"] == MISSING_VALUE
    assert operation.after["title"] is None


def test_revision_drift_blocks_even_when_desired_diff_is_valid():
    preview = build_reconciliation_preview(
        scope=ReconciliationScope(event_id=4242),
        candidates=(
            candidate(
                ReconciliationEntityType.ASSIGNMENT,
                "assignment-1",
                current={"status": "ACTIVE", "revision": 3},
                desired={"status": "COMPLETED", "revision": 4},
                expected_revision=2,
                current_revision=3,
            ),
        ),
    )

    assert preview.safe_to_apply is False
    assert preview.converged is False
    assert preview.blockers[0].code == "REVISION_MISMATCH"
    assert preview.operations[0].expected_revision == 2


def test_explicit_evaluator_blocker_is_preserved_and_prevents_safe_plan():
    preview = build_reconciliation_preview(
        scope=ReconciliationScope(event_id=4242),
        candidates=(),
        blockers=(
            ReconciliationBlocker(
                code="RESULTS_INCOMPLETE",
                message="One bout has no terminal result",
            ),
        ),
    )

    assert preview.summary()["blocker_count"] == 1
    assert preview.safe_to_apply is False


def test_equal_owned_state_is_converged_and_ignores_unowned_fields():
    current = {"status": "COMPLETED", "revision": 2, "private_note": "keep"}
    preview = build_reconciliation_preview(
        scope=ReconciliationScope(assignment_id="assignment-1"),
        candidates=(
            candidate(
                ReconciliationEntityType.ASSIGNMENT,
                "assignment-1",
                current=current,
                desired={"status": "COMPLETED", "revision": 2},
            ),
        ),
    )

    assert preview.operations == ()
    assert preview.converged is True
    assert current["private_note"] == "keep"


def test_malformed_scope_candidates_and_implicit_field_removal_are_rejected():
    with pytest.raises(ReconciliationInputError, match="requires"):
        ReconciliationScope()
    with pytest.raises(ReconciliationInputError, match="explicitly contain"):
        candidate(
            ReconciliationEntityType.ASSIGNMENT,
            "assignment-1",
            current={"status": "ACTIVE", "revision": 1},
            desired={"status": "FAILED"},
        )

    duplicate = candidate(
        ReconciliationEntityType.ASSIGNMENT,
        "assignment-1",
        current={"status": "ACTIVE", "revision": 1},
        desired={"status": "FAILED", "revision": 2},
    )
    with pytest.raises(ReconciliationInputError, match="identities must be unique"):
        build_reconciliation_preview(
            scope=ReconciliationScope(event_id=4242),
            candidates=(duplicate, duplicate),
        )


def test_pure_preview_module_has_no_database_or_write_adapter_imports():
    source = Path(
        "app/modules/missions/domain/reconciliation.py"
    ).read_text(encoding="utf-8")

    assert "pymongo" not in source
    assert "insert_one" not in source
    assert "update_one" not in source
    assert "delete" not in source.lower()
