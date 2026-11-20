"""Run deploy preflight with a fake Docker command; never change real containers."""

import os
import subprocess

import pytest

from src.migration_support import ROOT


@pytest.fixture
def deploy(tmp_path):
    calls = tmp_path / "docker-calls"
    docker = tmp_path / "docker"
    docker.write_text(
        '#!/bin/bash\n'
        'echo "$*|$NODE_PROTOCOL_PROFILE|$NATIVE_TOKEN_DECIMALS" >> "$DEPLOY_TEST_CALLS"\n'
        'if [ "$*" = "compose config --quiet" ]; then\n'
        '  exit "${DEPLOY_TEST_CONFIG_EXIT:-0}"\n'
        'fi\n'
        'exit 73\n'
    )
    docker.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        "DEPLOY_TEST_CALLS": str(calls),
        "NODE_HOST": "unused.invalid",
        "HTTP_PORT": "40403",
        "GRPC_PORT": "40401",
        "HASURA_BASE": "http://unused.invalid",
        "HASURA_ADMIN_SECRET": "preflight-test-secret",
    }
    for name in ("NODE_PROTOCOL_PROFILE", "NATIVE_TOKEN_DECIMALS", "DEPLOY_TEST_CONFIG_EXIT"):
        env.pop(name, None)

    def run(overrides=None, args=(), dotenv=None):
        if dotenv is not None:
            (tmp_path / ".env").write_text(dotenv)
        result = subprocess.run(
            ["bash", str(ROOT / "deploy.sh"), *args],
            cwd=tmp_path,
            env={**env, **(overrides or {})},
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result, calls.read_text().splitlines() if calls.exists() else []

    return run


@pytest.mark.parametrize(
    "selection,expected",
    [
        ({}, "uint256|18"),
        ({"NODE_PROTOCOL_PROFILE": "", "NATIVE_TOKEN_DECIMALS": ""}, "uint256|18"),
        ({"NODE_PROTOCOL_PROFILE": "classic", "NATIVE_TOKEN_DECIMALS": "8"}, "classic|8"),
        ({"NODE_PROTOCOL_PROFILE": "classic"}, "classic|18"),
        ({"NATIVE_TOKEN_DECIMALS": "8"}, "uint256|8"),
    ],
)
def test_deploy_validates_and_exports_defaults_or_explicit_overrides(deploy, selection, expected):
    result, calls = deploy(selection)
    assert result.returncode == 73  # Stop at the first fake container mutation.
    assert calls == [f"compose config --quiet|{expected}", f"compose up -d --build|{expected}"]
    assert "preflight-test-secret" not in result.stdout + result.stderr


def test_deploy_reads_explicit_legacy_selection_from_dotenv(deploy):
    result, calls = deploy(dotenv="NODE_PROTOCOL_PROFILE=classic\nNATIVE_TOKEN_DECIMALS=8\n")
    assert result.returncode == 73
    assert all(line.endswith("|classic|8") for line in calls)


@pytest.mark.parametrize(
    "selection",
    [
        {"NODE_HOST": ""},
        {"NODE_PROTOCOL_PROFILE": "auto"},
        {"NATIVE_TOKEN_DECIMALS": "-1"},
        {"NATIVE_TOKEN_DECIMALS": "78"},
        {"NATIVE_TOKEN_DECIMALS": "18.0"},
    ],
)
def test_invalid_configuration_stops_before_cleaning_or_any_docker_call(deploy, selection):
    result, calls = deploy(selection, args=("--clean",))
    assert result.returncode == 1
    assert calls == []


def test_compose_configuration_failure_stops_before_cleaning(deploy):
    result, calls = deploy({"DEPLOY_TEST_CONFIG_EXIT": "1"}, args=("--clean",))
    assert result.returncode == 1
    assert calls == ["compose config --quiet|uint256|18"]
