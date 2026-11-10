import os
import subprocess
import sys

import pytest

from src.config import Settings


def test_unset_profile_uses_uint256(monkeypatch):
    monkeypatch.delenv("NODE_PROTOCOL_PROFILE", raising=False)
    assert Settings(_env_file=None).node_protocol_profile == "uint256"


@pytest.mark.parametrize("profile", ["classic", "uint256"])
def test_explicit_profile_overrides_default(monkeypatch, profile):
    monkeypatch.setenv("NODE_PROTOCOL_PROFILE", profile)
    assert Settings(_env_file=None).node_protocol_profile == profile


@pytest.mark.parametrize(
    "environment,argument,expected",
    [
        (None, None, "uint256"),
        ("classic", None, "classic"),
        ("uint256", "classic", "classic"),
        ("classic", "uint256", "uint256"),
    ],
)
def test_generator_selects_complete_profile(tmp_path, environment, argument, expected):
    env = os.environ.copy()
    env.pop("NODE_PROTOCOL_PROFILE", None)
    if environment is not None:
        env["NODE_PROTOCOL_PROFILE"] = environment
    output = tmp_path / "stubs"
    command = [sys.executable, "scripts/generate-protos.py", "--output", str(output)]
    if argument is not None:
        command.extend(["--profile", argument])
    subprocess.run(
        command, env=env, check=True, capture_output=True, text=True, timeout=30
    )
    assert (output / "protocol_profile.txt").read_text() == expected

    schema_check = """
import sys
sys.path.insert(0, sys.argv[1])
import DeployServiceCommon_pb2 as common
import CasperMessage_pb2 as casper
import DeployServiceV1_pb2_grpc
price = common.Status.DESCRIPTOR.fields_by_name['minPhloPrice']
pending = casper.DeployDataProto.DESCRIPTOR.fields_by_name['phloPrice']
expected = (18, 9, 15, 12) if sys.argv[2] == 'uint256' else (7, 3, 7, 3)
assert (price.number, price.type, pending.number, pending.type) == expected
"""
    subprocess.run(
        [sys.executable, "-c", schema_check, str(output), expected],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
