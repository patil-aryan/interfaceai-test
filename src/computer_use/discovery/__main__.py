"""Discover how to do something, by driving a real application with a model.

    python -m computer_use.discovery "look up member 100234 and read their savings balance"

The goal is a sentence. The model first proposes the typed contract it implies,
which is saved to artifacts/goals/ so a human can read and edit it, and then
drives the application until the goal is met.

To re-run a contract that was already proposed and reviewed, pass its file:

    python -m computer_use.discovery artifacts/goals/member.savings_balance.json

The fake bank must be running:

    python -m fake_core_banking_app.app
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from computer_use.discovery.agent import DiscoveryAgent
from computer_use.discovery.compiler import compile_artifact, fragile_targets
from computer_use.discovery.contract import propose_contract, write_spec
from computer_use.discovery.env import load_env
from computer_use.discovery.redact import redacted_trace
from computer_use.discovery.spec import GoalSpec, load_goal_spec
from computer_use.discovery.verify import verify_outcomes
from computer_use.guardrails.policy import DEFAULT_ALLOWLIST_PATH, load_allowlist
from computer_use.replay.__main__ import sign_on
from computer_use.replay.engine import ReplayEngine
from computer_use.schema.profile import load_app_profile
from computer_use.surfaces.web import WebSurface

DEFAULT_MODEL = "claude-sonnet-5"
GOAL_DIR = Path("artifacts/goals")
CAPABILITY_DIR = Path("artifacts/capabilities")


def _detector(condition: Any) -> str:
    """Say how an outcome is recognised, whatever kind of condition recognises it."""
    if condition.kind in ("text_present", "text_absent"):
        return repr(condition.text)
    if condition.kind in ("element_present", "element_absent"):
        gone = "the absence of " if condition.kind == "element_absent" else ""
        return f"{gone}{condition.target.description!r}"
    return condition.kind


def require_api_key() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit(
            "ANTHROPIC_API_KEY is not set. Put it in .env (which is gitignored) "
            "or export it. Discovery needs a model; replay never does."
        )


async def get_spec(args: argparse.Namespace, client) -> GoalSpec:
    """Either load a reviewed contract, or propose one from the goal sentence."""
    as_path = Path(args.goal)
    if as_path.is_file():
        print(f"contract : {as_path} (already reviewed)")
        return load_goal_spec(as_path)

    print(f"goal     : {args.goal}")
    print("proposing the typed contract this implies ...")
    spec = await propose_contract(
        client, args.model, args.goal,
        app_profile=args.app_profile, institution=args.institution,
    )
    path = GOAL_DIR / f"{spec.id}.json"
    write_spec(spec, path)

    print(f"contract : {path}")
    print(f"  id      {spec.id}   risk {spec.risk_tier.value}")
    for i in spec.inputs:
        key = " unique-key" if i.unique_key else ""
        print(f"  input   {i.name} ({i.type}, {i.sensitivity.value}{key}) example={i.example!r}")
    for o in spec.outputs:
        print(f"  output  {o.name} ({o.type}, {o.sensitivity.value})")
    return spec


async def discover(args: argparse.Namespace) -> int:
    load_env()
    require_api_key()
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic()
    spec = await get_spec(args, client)
    if args.contract_only:
        print("\nstopping after the contract, as asked.")
        return 0

    policy = load_allowlist(args.allowlist)
    surface = WebSurface(headless=not args.headed, slow_mo_ms=args.slow)
    await surface.start()
    try:
        profile = load_app_profile(spec.app_profile)
        if spec.requires_authenticated_session:
            await sign_on(surface, profile, args.base_url, args.user, args.password)
            await surface.wait_for_ready()
            held, observed = await surface.wait_for(profile.sign_on.success, {}, 5_000)
            if not held:
                raise SystemExit(
                    f"could not sign in as {args.user!r}: {observed}. Discovery stops "
                    f"here on purpose. Left at a sign-on screen the model will try to "
                    f"work around it, which is not something to let it learn."
                )

        agent = DiscoveryAgent(
            surface, client,
            base_url=args.base_url, spec=spec, policy=policy,
            model=args.model, evidence_root=args.evidence_dir,
            sign_on_path=profile.sign_on.path if profile.sign_on else None,
        )
        outcome = await agent.run()

        artifact = dropped = None
        if outcome.reached_goal:
            artifact = compile_artifact(spec, outcome)
            if spec.outcomes and not args.skip_verify:
                print("\nconfirming declared outcomes against the running application ...")
                # An outcome probe drives the flow on purpose, so it must not
                # be stopped to ask for confirmation, and it must not commit.
                engine = ReplayEngine(
                    surface, base_url=args.base_url, policy=policy,
                    evidence_root=args.evidence_dir, profile=profile,
                    credentials=(args.user, args.password),
                    confirm_irreversible=False, stop_before_irreversible=True,
                )
                verified, dropped = await verify_outcomes(
                    engine, surface, client, args.model, spec,
                    artifact.model_copy(update={"outcomes": []}),
                    institution=spec.institution,
                )
                artifact = artifact.model_copy(update={"outcomes": verified})
    finally:
        await surface.stop()

    trace = Path(outcome.evidence_dir) / "discovery.json"
    trace.parent.mkdir(parents=True, exist_ok=True)
    trace.write_text(
        redacted_trace(outcome, spec).model_dump_json(indent=2), encoding="utf-8"
    )

    print(f"\n{'goal met' if outcome.reached_goal else 'GOAL NOT MET'}  ({outcome.stop_reason})")
    print(f"turns    : {outcome.turns}")
    print(f"outputs  : {json.dumps(outcome.outputs)}")
    print(f"trace    : {trace}")
    for i, step in enumerate(outcome.steps, 1):
        strategy = step.target.recorded_strategy if step.target else "-"
        scope = f"  in row {step.target.scope.text.value!r}" if step.target and step.target.scope else ""
        print(f"  {i:>2}. {step.action.value:<9} via {strategy:<11}{scope}")
        print(f"      {step.intent}")

    if artifact is None:
        return 1

    CAPABILITY_DIR.mkdir(parents=True, exist_ok=True)
    written = CAPABILITY_DIR / f"{artifact.id}@{artifact.version}.json"
    written.write_text(artifact.model_dump_json(indent=2), encoding="utf-8")

    # The saved contract keeps what was guessed from the sentence. Put back what
    # the application turned out to offer, so the next run starts from the truth.
    write_spec(spec.model_copy(update={"inputs": artifact.inputs}),
               GOAL_DIR / f"{spec.id}.json")

    checkpointed = sum(1 for s in artifact.steps if s.checkpoint is not None)
    parameterised = sum(
        1 for s in artifact.steps if s.value is not None and s.value.kind == "param"
    )
    print(f"\nartifact : {written}")
    print(f"  status        {artifact.status.value}   risk {artifact.risk_tier.value}")
    print(f"  steps         {len(artifact.steps)}, {checkpointed} with a checkpoint, "
          f"{parameterised} parameterised")
    for o in artifact.outcomes:
        print(f"  outcome       {o.code} confirmed, detected by {_detector(o.detector)}")
    for reason in dropped or []:
        print(f"  outcome       dropped: {reason}")
    print(f"  success       {artifact.success_condition.description}")
    for warning in fragile_targets(artifact):
        print(f"  FRAGILE       {warning}")
    print("\nreplay it with:")
    print(f"  python -m computer_use.replay {artifact.id} \\")
    print(f"      --param {artifact.inputs[0].name}=<value> --attended"
          if artifact.inputs else "      --attended")
    return 0


def main() -> int:
    load_env()          # before argparse, which reads defaults from the environment
    parser = argparse.ArgumentParser(
        prog="python -m computer_use.discovery",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("goal", help="a goal in plain English, or a path to a saved contract")
    parser.add_argument("--skip-verify", action="store_true",
                        help="do not confirm declared outcomes against the application")
    parser.add_argument("--contract-only", action="store_true",
                        help="propose the contract and stop, so it can be reviewed first")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--app-profile", default="meridian-core@8.4")
    parser.add_argument("--institution", default="pinecrest-cu")
    parser.add_argument("--base-url", default="http://127.0.0.1:8081")
    parser.add_argument("--allowlist", default=str(DEFAULT_ALLOWLIST_PATH))
    parser.add_argument("--evidence-dir", default="evidence")
    parser.add_argument("--headed", action="store_true", help="show the browser")
    parser.add_argument("--slow", type=int, default=0, metavar="MS")
    parser.add_argument("--user", default=os.environ.get("TARGET_APP_USERNAME", "operator1"),
                        help="operator to sign in as; also read from TARGET_APP_USERNAME")
    parser.add_argument("--password", default=os.environ.get("TARGET_APP_PASSWORD", ""),
                        help="read from TARGET_APP_PASSWORD; never stored in an artifact")
    return asyncio.run(discover(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
