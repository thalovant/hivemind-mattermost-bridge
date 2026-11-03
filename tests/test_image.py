"""The image starts the bridge the way the platform runs it.

Uid 10001, ``HOME=/home/bridge`` (the identity and Noise key live under
``~/.config/hivemind``), and an entrypoint with no arguments: every setting
comes from the environment, so no secret is ever on the command line.
"""
from pathlib import Path

DOCKERFILE = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
LINES = DOCKERFILE.splitlines()


def test_the_image_passes_no_secret_and_no_argument_on_the_command_line():
    entrypoint = next(line for line in LINES if line.startswith("ENTRYPOINT"))
    assert entrypoint == 'ENTRYPOINT ["hivemind-mattermost-bridge"]'
    assert not any(line.startswith("CMD") for line in LINES)


def test_the_image_runs_as_uid_10001_with_its_home_where_the_platform_mounts_it():
    assert "USER bridge" in LINES
    assert any("--uid 10001" in line and "bridge" in line for line in LINES)
    assert "HOME=/home/bridge" in DOCKERFILE


def test_the_build_context_leaves_local_state_out():
    ignored = (Path(__file__).resolve().parents[1] / ".dockerignore").read_text(encoding="utf-8").split()
    assert {".git", ".venv"} <= set(ignored)
