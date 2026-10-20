import re
from pathlib import Path

import yaml

WORKFLOW_DIR = Path(".github/workflows")
RELEASE_WORKFLOW = WORKFLOW_DIR / "release.yml"


def _workflow(path: Path) -> dict:
    return yaml.load(path.read_text(), Loader=yaml.BaseLoader)


def test_workflows_pin_actions_and_disable_checkout_credentials():
    action_use = re.compile(r"^\s*(?:-\s+)?uses:\s*([^\s#]+)(?:\s+#\s*(\S+))?", re.MULTILINE)
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        workflow = _workflow(path)
        for action, comment in action_use.findall(path.read_text()):
            assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", action)
            assert comment.startswith("v")
        for job in workflow["jobs"].values():
            for step in job["steps"]:
                if str(step.get("uses", "")).startswith("actions/checkout@"):
                    assert step.get("with", {}).get("persist-credentials") == "false"


def test_workflows_use_least_privilege_permissions():
    tests = _workflow(WORKFLOW_DIR / "charm-tests.yml")
    assert tests["permissions"] == {"contents": "read"}
    assert tests["jobs"]["charm-tests"]["permissions"] == {"contents": "read"}
    release = _workflow(RELEASE_WORKFLOW)
    assert release["permissions"] == {"contents": "read"}
    assert release["jobs"]["build-ubuntu-22"]["permissions"] == {"contents": "read"}
    assert release["jobs"]["build-ubuntu-24"]["permissions"] == {"contents": "read"}
    assert release["jobs"]["release"]["permissions"] == {"actions": "read", "contents": "read"}
    assert release["jobs"]["github-release"]["permissions"] == {
        "actions": "read",
        "contents": "write",
    }


def test_release_is_manual_main_only_and_serialized():
    workflow = _workflow(RELEASE_WORKFLOW)
    assert workflow["on"] == {"workflow_dispatch": ""}
    assert workflow["concurrency"] == {
        "group": "${{ github.workflow }}-latest-edge",
        "cancel-in-progress": "false",
    }
    jobs = workflow["jobs"]
    assert jobs["build-ubuntu-22"]["if"] == "github.ref == 'refs/heads/main'"
    assert jobs["build-ubuntu-24"]["if"] == "github.ref == 'refs/heads/main'"
    assert jobs["release"]["needs"] == ["build-ubuntu-22", "build-ubuntu-24"]
    assert jobs["github-release"]["needs"] == ["release"]
    assert all("concurrency" not in job for job in jobs.values())


def test_release_preserves_base_artifacts_and_only_edge():
    text = RELEASE_WORKFLOW.read_text()
    assert "./alloy-nm_ubuntu@22.04-amd64.charm" in text
    assert "./alloy-nm_ubuntu@24.04-amd64.charm" in text
    commands = re.findall(r"^\s*[^#\n]*charmcraft\s+(?:upload|release)[^\n]*$", text, re.MULTILINE)
    assert len(commands) == 2
    assert all("upload" in command and "--release latest/edge" in command for command in commands)
    assert not any(re.search(r"latest/(?:candidate|stable)", command) for command in commands)


def test_revisions_metadata_and_existing_tag_are_hardened():
    workflow = _workflow(RELEASE_WORKFLOW)
    release_steps = workflow["jobs"]["release"]["steps"]
    for step_id in ("upload_ubuntu_22", "upload_ubuntu_24"):
        script = next(step["run"] for step in release_steps if step.get("id") == step_id)
        assert '[[ ! "$revision" =~ ^[1-9][0-9]*$ ]]' in script
    steps = workflow["jobs"]["github-release"]["steps"]
    metadata = next(step for step in steps if step.get("id") == "release_meta")
    assert metadata["env"] == {
        "CHANNEL": "${{ needs.release.outputs.release_channel }}",
        "REVISION_22": "${{ needs.release.outputs.rev_ubuntu_22 }}",
        "REVISION_24": "${{ needs.release.outputs.rev_ubuntu_24 }}",
    }
    assert metadata["run"].count("=~ ^[1-9][0-9]*$") == 2
    assert 'tag="latest-edge-r${REVISION_22}-r${REVISION_24}"' in metadata["run"]
    provenance = next(
        step for step in steps if step["name"] == "Verify existing release tag provenance"
    )
    assert provenance["env"] == {
        "GITHUB_TOKEN": "${{ github.token }}",
        "TAG": "${{ steps.release_meta.outputs.tag }}",
    }
    assert "credential.helper=" in provenance["run"]
    assert '"${tag_ref}^{}"' in provenance["run"]
    assert '"$existing_sha" != "$GITHUB_SHA"' in provenance["run"]
    action = next(step for step in steps if str(step.get("uses", "")).startswith("softprops/"))
    assert action["with"]["prerelease"] == "true"
    assert action["with"]["make_latest"] == "false"
    assert action["with"]["fail_on_unmatched_files"] == "true"
