"""Focused contract tests for the standalone render-policy primitive."""
from __future__ import annotations

import json

import pytest

from meridian_docs.render_policy import (
    DEFAULT_RENDER_POLICY,
    POLICY_NAMES,
    RenderPolicy,
    RenderPolicyError,
    deserialize_render_policy,
    normalize_render_policy,
    serialize_render_policy,
    validate_render_policy,
)


def test_default_policy_is_structural_for_draft_iteration():
    policy = normalize_render_policy()

    assert policy == DEFAULT_RENDER_POLICY
    assert policy["policy"] == "structural"
    assert policy["structural_validation"] is True
    assert policy["render"] is False
    assert policy["raster"] is False
    assert policy["tms"] is False
    assert policy["require_visual_render"] is False


def test_all_named_policies_are_available_with_explicit_flags():
    assert set(POLICY_NAMES) == {"structural", "targeted", "publication", "release"}

    for name in POLICY_NAMES:
        policy = normalize_render_policy(name)
        assert set(policy) == {
            "version",
            "policy",
            "structural_validation",
            "render",
            "raster",
            "tms",
            "require_visual_render",
        }
        assert policy["structural_validation"] is True
        assert all(isinstance(policy[key], bool) for key in policy if key not in {"version", "policy"})


def test_release_requires_visual_render():
    release = normalize_render_policy("release")

    assert release["render"] is True
    assert release["require_visual_render"] is True


def test_policy_ladder_enables_progressively_stricter_checks():
    structural = normalize_render_policy("structural")
    targeted = normalize_render_policy("targeted")
    publication = normalize_render_policy("publication")
    release = normalize_render_policy("release")

    assert structural["render"] is False
    assert targeted["render"] is True
    assert targeted["raster"] is False and targeted["tms"] is False
    assert publication["render"] is True and publication["raster"] is True and publication["tms"] is True
    assert release["require_visual_render"] is True


def test_explicit_preset_object_round_trips_through_validation():
    policy = normalize_render_policy({"policy": "publication", "version": 1})

    assert policy == normalize_render_policy("publication")
    assert validate_render_policy(policy) == policy


@pytest.mark.parametrize(
    "bad_policy",
    [
        {"policy": "unknown"},
        {"policy": "release", "render": False},
        {"policy": "structural", "structural_validation": False},
        {"policy": "publication", "raster": "yes"},
        {"policy": "targeted", "unexpected": True},
    ],
)
def test_validation_rejects_unknown_or_inconsistent_policies(bad_policy):
    with pytest.raises(RenderPolicyError):
        normalize_render_policy(bad_policy)


def test_serialization_is_deterministic_and_validated():
    encoded = serialize_render_policy("release")

    assert encoded == serialize_render_policy(normalize_render_policy("release"))
    assert json.loads(encoded)["require_visual_render"] is True
    assert deserialize_render_policy(encoded) == normalize_render_policy("release")


@pytest.mark.parametrize("serialized", ["not-json", "[]", "null", '{"policy":"release","render":false}'])
def test_deserialization_rejects_invalid_policy_documents(serialized):
    with pytest.raises(RenderPolicyError):
        deserialize_render_policy(serialized)


def test_render_policy_value_object_is_immutable_and_serializable():
    policy = RenderPolicy.from_value("release")

    assert policy.to_dict() == normalize_render_policy("release")
    assert json.loads(policy.to_json())["policy"] == "release"
    with pytest.raises((AttributeError, TypeError)):
        policy.render = False
