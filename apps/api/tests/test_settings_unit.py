"""INPROOM-102: settings schema v2 rules, legacy upgrade, diff and production guard (pure functions)."""

import pytest
from pydantic import ValidationError

from app.auth import Principal
from app.modules.settings import (
    ProjectSettings,
    content_hash,
    default_settings,
    diff,
    needs_production_authority,
    rule_errors,
    stamp_production,
    upgrade,
)

ALICE = Principal(user_id="alice", tenant_id=__import__("uuid").uuid4())


def s(**kw) -> ProjectSettings:
    return ProjectSettings.model_validate(kw)


def test_defaults_are_safe():
    d = default_settings()
    assert d.write_mode == "read_only" and d.model_policy == "tenant_default"
    assert d.production.actions_enabled is False and d.production.environments == []
    assert d.approvals.required_scopes == ["plan"] and d.approvals.self_approval is False
    assert rule_errors(d) == []


def test_lists_are_canonical_so_equal_settings_hash_equally():
    a = s(
        approvals={"required_scopes": ["release", "plan", "plan"]},
        sources={"allowed_kinds": ["upload", "git"]},
    )
    b = s(approvals={"required_scopes": ["plan", "release"]}, sources={"allowed_kinds": ["git", "upload"]})
    assert a.approvals.required_scopes == ["plan", "release"]
    assert content_hash(a) == content_hash(b)


@pytest.mark.parametrize(
    "bad",
    [
        {"approvals": {"self_approval": True}},
        {"approvals": {"min_approvers": 4}},
        {"approvals": {"expiry_hours": 0}},
        {"write_mode": "yolo"},
        {"sources": {"allowed_kinds": []}},
        {"sources": {"allowed_kinds": ["ftp"]}},
        {"production": {"environments": ["Prod!"]}},
        {"unknown_field": 1},
        {"schema_version": 1},
    ],
)
def test_schema_rejects_invalid_values(bad):
    with pytest.raises(ValidationError):
        s(**bad)


@pytest.mark.parametrize(
    ("settings", "loc"),
    [
        ({"process_package": "foo@1"}, ["body", "settings", "process_package"]),
        (
            {"write_mode": "approved_writes", "approvals": {"required_scopes": []}},
            ["body", "settings", "approvals", "required_scopes"],
        ),
        (
            {"production": {"actions_enabled": True}, "approvals": {"required_scopes": ["plan", "release"]}},
            ["body", "settings", "production", "environments"],
        ),
        (
            {"production": {"actions_enabled": True, "environments": ["prod"]}},
            ["body", "settings", "approvals", "required_scopes"],
        ),
    ],
)
def test_cross_field_rules(settings, loc):
    errors = rule_errors(s(**settings))
    assert [e["loc"] for e in errors] == [loc]


def test_valid_production_configuration_passes_rules():
    ok = s(
        production={"actions_enabled": True, "environments": ["prod"]},
        approvals={"required_scopes": ["release"]},
    )
    assert rule_errors(ok) == []


def test_legacy_v1_documents_are_read_as_v2():
    legacy = {
        "write_mode": "read_only",
        "approval_policy": "approval_required_for_external_writes",
        "model_policy": "tenant_default",
        "process_package": "stlc@1",
    }
    up = upgrade(legacy)
    assert up == default_settings("stlc@1")
    assert upgrade({}) == default_settings()
    assert upgrade({"write_mode": "nonsense"}).write_mode == "read_only"


def test_diff_lists_leaf_paths():
    old = default_settings()
    new = s(write_mode="draft_only", approvals={"required_scopes": ["plan", "test"], "expiry_hours": 24})
    changes = {c.path: (c.old, c.new) for c in diff(old, new)}
    assert changes == {
        "write_mode": ("read_only", "draft_only"),
        "approvals.required_scopes": (["plan"], ["plan", "test"]),
        "approvals.expiry_hours": (72, 24),
    }
    assert diff(old, old) == []


def _prod(enabled, envs=("prod",)):
    return s(
        production={"actions_enabled": enabled, "environments": list(envs)},
        approvals={"required_scopes": ["release"]},
    )


def test_production_authority_needed_to_enable_or_change_while_enabled_not_to_disable():
    assert needs_production_authority(_prod(False, ()), _prod(True))
    assert needs_production_authority(_prod(True), _prod(True, ("prod", "eu-prod")))
    assert not needs_production_authority(_prod(True), _prod(False))
    assert not needs_production_authority(_prod(True), _prod(True))
    assert not needs_production_authority(default_settings(), s(write_mode="draft_only"))


def test_server_owns_enabled_by_and_enabled_at():
    forged = s(production={"actions_enabled": True, "environments": ["prod"], "enabled_by": "mallory"})
    stamped = stamp_production(default_settings(), forged, ALICE)
    assert stamped.production.enabled_by == "alice" and stamped.production.enabled_at is not None
    kept = stamp_production(stamped, forged, Principal(user_id="bob", tenant_id=ALICE.tenant_id))
    assert (
        kept.production.enabled_by == "alice" and kept.production.enabled_at == stamped.production.enabled_at
    )
    off = stamp_production(stamped, s(), ALICE)
    assert off.production.enabled_by is None and off.production.enabled_at is None
