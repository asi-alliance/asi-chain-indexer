"""Generate one complete, explicitly selected wire profile without mixing schemas."""

import argparse
import os
from pathlib import Path

from grpc_tools import protoc
import grpc_tools


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--profile",
        choices=("classic", "uint256"),
        default=os.environ.get("NODE_PROTOCOL_PROFILE", "uint256"),
    )
    parser.add_argument("--output", type=Path, default=Path("src/grpc_stubs"))
    args = parser.parse_args()
    if args.profile not in ("classic", "uint256"):
        parser.error("unknown NODE_PROTOCOL_PROFILE")
    source = Path("protos") / ("uint256" if args.profile == "uint256" else "")
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "scalapb").mkdir(exist_ok=True)
    files = [
        "DeployServiceV1.proto",
        "DeployServiceCommon.proto",
        "CasperMessage.proto",
        "RhoTypes.proto",
        "ServiceError.proto",
        "scalapb/scalapb.proto",
    ]
    result = protoc.main(
        [
            "grpc_tools.protoc",
            f"-I{source}",
            f"-I{Path(grpc_tools.__file__).parent / '_proto'}",
            f"--python_out={args.output}",
            f"--grpc_python_out={args.output}",
            *[str(source / name) for name in files],
        ]
    )
    if result:
        raise SystemExit(result)
    (args.output / "protocol_profile.txt").write_text(args.profile, encoding="ascii")


if __name__ == "__main__":
    main()
