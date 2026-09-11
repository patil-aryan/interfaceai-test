"""Replay a capability artifact against a live surface. No model is involved.

    python -m computer_use.replay <artifact.json> --param member_id=100253

The fake bank must be running:

    python -m fake_core_banking_app.app
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from computer_use.replay.engine import ReplayEngine
from computer_use.schema.capability import (
    CapabilityArtifact,
    ElementTarget,
    RoleNameLocator,
)
from computer_use.surfaces.base import Surface
from computer_use.surfaces.web import WebSurface

CAPABILITY_DIR = Path("artifacts/capabilities")


def find_artifact(reference: str) -> Path:
    """Accept a path, or a capability id to look up in artifacts/capabilities."""
    direct = Path(reference)
    if direct.is_file():
        return direct
    matches = sorted(CAPABILITY_DIR.glob(f"{reference}@*.json"))
    if matches:
        return matches[-1]  # highest version
    raise SystemExit(f"no artifact found for {reference!r}")


def parse_params(pairs: list[str]) -> dict[str, str]:
    params: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--param expects key=value, got {pair!r}")
        key, value = pair.split("=", 1)
        params[key] = value
    return params


async def sign_on_stub(surface: Surface, base_url: str, user: str, password: str) -> None:
    """Establish a session by hand.

    TEMPORARY. This belongs in the app profile, which knows the sign-on screen
    for a vendor product and resolves credentials from a secret provider. Until
    that exists, the caller does it, which is the same division of
    responsibility performed manually. Credentials never enter an artifact.
    """
    await surface.navigate(f"{base_url}/login")
    for field, value in (("USER ID", user), ("PASSWORD", password)):
        handle, _ = await surface.resolve(
            ElementTarget(
                description=field,
                strategies=[RoleNameLocator(role="textbox", name=field)],
                recorded_strategy="role_name",
            ),
            {},
        )
        await surface.fill(handle, value)
    handle, _ = await surface.resolve(
        ElementTarget(
            description="Sign On button",
            strategies=[RoleNameLocator(role="button", name="Sign On")],
            recorded_strategy="role_name",
        ),
        {},
    )
    await surface.activate(handle)


async def replay(args: argparse.Namespace) -> int:
    artifact = CapabilityArtifact.model_validate_json(
        find_artifact(args.artifact).read_text(encoding="utf-8")
    )
    surface = WebSurface(headless=not args.headed, slow_mo_ms=args.slow)
    await surface.start()
    try:
        if artifact.requires_authenticated_session:
            await sign_on_stub(surface, args.base_url, args.user, args.password)
        engine = ReplayEngine(
            surface, base_url=args.base_url, evidence_root=args.evidence_dir
        )
        result = await engine.run(
            artifact, parse_params(args.param), institution=args.institution
        )
    finally:
        await surface.stop()

    print(json.dumps(json.loads(result.model_dump_json()), indent=2))
    return 0 if result.status in ("success", "business_outcome") else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m computer_use.replay",
                                     description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("artifact", help="path to an artifact, or a capability id")
    parser.add_argument("--param", action="append", default=[], metavar="KEY=VALUE",
                        help="an input parameter; repeatable")
    parser.add_argument("--base-url", default="http://127.0.0.1:8081")
    parser.add_argument("--institution", default="pinecrest-cu")
    parser.add_argument("--evidence-dir", default="evidence")
    parser.add_argument("--headed", action="store_true", help="show the browser")
    parser.add_argument("--slow", type=int, default=0, metavar="MS",
                        help="pause between actions, to watch it work")
    parser.add_argument("--user", default="operator1", help="operator for the session stub")
    parser.add_argument("--password", default="changeme")
    return asyncio.run(replay(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
