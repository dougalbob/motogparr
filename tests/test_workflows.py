"""
Tests for the CI workflows and release automation.

Workflows cannot be run here (no Docker daemon, and the release notes API is not
reachable), so what is testable is the set of properties that have already caused
real failures:

  * a tag format that the container workflow cannot turn into :MAJOR.MINOR,
  * notes that are generated but never published,
  * a smoke test that does not actually assert the non-root behaviour it exists
    to protect.

When a workflow changes, these fail loudly rather than the next release failing
quietly.
"""

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
SMOKE = WORKFLOWS / "container-smoke.yml"
RELEASE = WORKFLOWS / "release.yml"
PUBLISH = WORKFLOWS / "docker-publish.yml"
RELEASE_CONFIG = REPO_ROOT / ".github" / "release.yml"


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Every workflow is at least structurally valid
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("*.yml")), ids=lambda p: p.name)
def test_workflow_is_valid_yaml_with_a_trigger_and_jobs(path):
    data = load(path)
    # PyYAML reads the bare key `on:` as the boolean True.
    assert "on" in data or True in data, f"{path.name} has no trigger"
    assert data.get("jobs"), f"{path.name} has no jobs"


# ---------------------------------------------------------------------------
# Container smoke test (item 8)
# ---------------------------------------------------------------------------

def test_smoke_test_builds_and_runs_the_image():
    text = SMOKE.read_text()
    assert "docker/build-push-action" in text
    assert "load: true" in text, "the image must be loaded locally to run it"
    assert "docker run -d" in text


def test_smoke_test_asserts_the_app_is_not_root():
    """
    The privilege drop is the change most likely to regress silently: a missing
    setpriv or a misordered entrypoint would put the app back to root and
    nothing else would notice.
    """
    text = SMOKE.read_text()
    assert "docker top" in text
    assert "Running as root" in text


def test_smoke_test_checks_healthcheck_and_startup_output():
    text = SMOKE.read_text()
    assert "/healthz" in text
    assert "State.Health.Status" in text
    assert "Version: smoke" in text


def test_smoke_test_verifies_auth_rejects_unauthenticated_requests():
    """
    Guards the feature that would be silently absent if PyJWT or its crypto
    dependency were missing from the image.
    """
    text = SMOKE.read_text()
    assert "AUTH_ISSUER" in text
    assert '= "401"' in text
    assert "Cf-Access-Jwt-Assertion" in text


def test_smoke_test_asserts_development_files_are_absent():
    text = SMOKE.read_text()
    assert "backup" in text
    assert "cleanup_utility" in text


# ---------------------------------------------------------------------------
# Release automation (item 7)
# ---------------------------------------------------------------------------

def test_release_workflow_runs_on_a_version_tag():
    data = load(RELEASE)
    triggers = data.get("on") or data.get(True)
    assert "v*" in str(triggers)


def test_release_notes_are_generated_not_hand_written():
    """A hand-written body would silently omit merged changes."""
    text = RELEASE.read_text()
    assert "generate-notes" in text
    # generate-notes is POST; gh api defaults to GET and would 404.
    assert "--method POST" in text


def test_release_notes_use_the_categorised_config():
    text = RELEASE.read_text()
    assert "configuration_file_path=.github/release.yml" in text
    assert RELEASE_CONFIG.is_file()


def test_release_config_categorises_by_label():
    config = load(RELEASE_CONFIG)
    categories = config["changelog"]["categories"]
    assert any(c["title"] == "Security" for c in categories)
    # Everything unlabelled must still appear somewhere.
    assert any("*" in c.get("labels", []) for c in categories)


def test_release_validates_the_tag_format():
    """
    'V.2.2.1' did not match the container workflow's semver patterns, so it
    produced no :2.2 and no :latest. Catching it at release time is cheaper than
    noticing an untagged image later.
    """
    text = RELEASE.read_text()
    assert "v[0-9]+" in text
    assert "Bad tag format" in text


def test_release_publishes_and_updates_existing_releases():
    text = RELEASE.read_text()
    assert "gh release create" in text
    assert "gh release edit" in text, "re-running must not fail on an existing release"


def test_release_workflow_requests_pull_requests_write():
    """Opening a release-notes PR needs pull-requests: write on the token."""
    data = load(RELEASE)
    perms = data.get("permissions") or {}
    assert perms.get("pull-requests") == "write", perms
    assert perms.get("contents") == "write", perms


def test_release_commits_to_a_branch_never_main():
    """The notes file is committed to a release-notes branch, never pushed
    directly to main."""
    text = RELEASE.read_text()
    assert "release-notes/v" in text, "no release-notes branch target"
    assert "${{ steps.version.outputs.version }}" in text, "branch name must derive from the tag"
    # The commit/push targets the release-notes branch; main must not be the
    # push target.
    assert "git push origin main" not in text
    assert "git push -u origin main" not in text


def test_release_notes_filename_comes_from_the_tag():
    """The committed file name matches RELEASE_NOTES_v<version>.md where the
    version is taken from the tag, never hard-coded."""
    text = RELEASE.read_text()
    assert 'file="RELEASE_NOTES_v${{ steps.version.outputs.version }}.md"' in text
    assert 'RELEASE_NOTES_v${{ steps.version.outputs.version }}.md' in text
    # No literal version baked into the workflow.
    assert "RELEASE_NOTES_v2.5.0" not in text


def test_artifact_path_and_committed_path_are_the_same_file():
    text = RELEASE.read_text()
    committed = 'RELEASE_NOTES_v${{ steps.version.outputs.version }}.md'
    assert committed in text
    assert "path: RELEASE_NOTES_v*.md" in text
    # The writeup output (the committed file) and the artifact glob refer to
    # the same notes file.
    assert "steps.writeup.outputs.file" in text


def test_release_pr_is_idempotent_on_existing_branch_and_pr():
    """Re-runs must update the existing branch/PR instead of failing."""
    text = RELEASE.read_text()
    # Guards on an existing branch.
    assert "git rev-parse --verify" in text or "git checkout -b" in text
    # Guards on an existing PR: list then edit, else create.
    assert "gh pr list" in text
    assert "gh pr edit" in text
    assert "gh pr create" in text


def test_docker_publish_tag_patterns_agree_with_the_release_tag_format():
    """
    The two workflows must agree on what a version tag looks like, or the release
    is published and the image is not.
    """
    publish = PUBLISH.read_text()
    assert "type=semver,pattern={{version}}" in publish
    assert "type=semver,pattern={{major}}.{{minor}}" in publish
