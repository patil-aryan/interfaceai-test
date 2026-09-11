"""Replay a capability artifact against a live surface. No model is involved.

    python -m computer_use.replay <artifact.json> --param member_id=100253

The fake bank must be running:

    python -m fake_core_banking_app.app
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

from computer_use.discovery.env import load_env
from computer_use.escalation.handoff import HandoffBroker
from computer_use.guardrails.policy import DEFAULT_ALLOWLIST_PATH, load_allowlist
from computer_use.replay.engine import ReplayEngine
from computer_use.schema.capability import CapabilityArtifact
from computer_use.schema.profile import AppProfile, load_app_profile
from computer_use.surfaces import build_surface
from computer_use.surfaces.base import Surface

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


async def sign_on(surface: Surface, profile: AppProfile, base_url: str,
                  user: str, password: str) -> None:
    """Establish a session using the profile's sign-on definition.

    Sign-on belongs to the application, not to any one capability, which is why
    it lives in the app profile and not in an artifact. Credentials are supplied
    here and never enter an artifact, a log, or the model's context.
    """
    if profile.sign_on is None:
        raise SystemExit(f"app profile {profile.id} defines no sign-on")
    spec = profile.sign_on
    await surface.navigate(f"{base_url.rstrip('/')}{spec.path}")
    for target, value in ((spec.username_field, user), (spec.password_field, password)):
        handle, _ = await surface.resolve(target, {})
        await surface.fill(handle, value)
    handle, _ = await surface.resolve(spec.submit, {})
    await surface.activate(handle)
    await surface.wait_for(spec.success, {}, 10_000)


async def replay(args: argparse.Namespace) -> int:
    artifact = CapabilityArtifact.model_validate_json(
        find_artifact(args.artifact).read_text(encoding="utf-8")
    )
    run_id = f"run_{uuid.uuid4().hex[:10]}"
    handoff = HandoffBroker(Path(args.evidence_dir) / run_id, wait_seconds=args.operator_wait)
    profile = load_app_profile(artifact.app_profile)
    surface = build_surface(profile, headed=args.headed, slow_mo_ms=args.slow)
    await surface.start()
    try:
        engine = ReplayEngine(
            surface,
            base_url=args.base_url,
            evidence_root=args.evidence_dir,
            policy=load_allowlist(args.allowlist),
            profile=profile,
            credentials=(args.user, args.password),
            handoff=handoff,
            confirm_irreversible=not args.no_confirm,
        )
        result = await engine.run(
            artifact,
            parse_params(args.param),
            institution=args.institution,
            unattended=not args.attended,
            run_id=run_id,
        )
    finally:
        await surface.stop()

    print(json.dumps(json.loads(result.model_dump_json()), indent=2))
    return 0 if result.status in ("success", "business_outcome") else 1


def main() -> int:
    load_env()          # before argparse, which reads defaults from the environment
    parser = argparse.ArgumentParser(prog="python -m computer_use.replay",
                                     description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("artifact", help="path to an artifact, or a capability id")
    parser.add_argument("--param", action="append", default=[], metavar="KEY=VALUE",
                        help="an input parameter; repeatable")
    parser.add_argument("--base-url", default="http://127.0.0.1:8081")
    parser.add_argument("--institution", default="pinecrest-cu")
    parser.add_argument("--evidence-dir", default="evidence")
    parser.add_argument("--allowlist", default=str(DEFAULT_ALLOWLIST_PATH),
                        help="path to the permissions file")
    parser.add_argument("--operator-wait", type=int, default=300, metavar="SECONDS",
                        help="how long to hold the session open for a person")
    parser.add_argument("--no-confirm", action="store_true",
                        help="do not ask a person before an irreversible step")
    parser.add_argument("--attended", action="store_true",
                        help="a human is watching; relaxes the approved-artifact requirement")
    parser.add_argument("--headed", action="store_true", help="show the browser")
    parser.add_argument("--slow", type=int, default=0, metavar="MS",
                        help="pause between actions, to watch it work")
    parser.add_argument("--user", default=os.environ.get("TARGET_APP_USERNAME", "operator1"),
                        help="operator to sign in as; also read from TARGET_APP_USERNAME")
    parser.add_argument("--password", default=os.environ.get("TARGET_APP_PASSWORD", ""),
                        help="read from TARGET_APP_PASSWORD; never stored in an artifact")
    return asyncio.run(replay(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
