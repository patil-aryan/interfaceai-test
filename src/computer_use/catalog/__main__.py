"""The capability catalog: what an AI agent can see, and call.

List what is available, as an agent would receive it:

    python -m computer_use.catalog

Call one directly, by name and typed arguments:

    python -m computer_use.catalog --call member.lookup_profile_and_savings_balance \
        --arg member_number=100253

Or hand the catalog to a model and give it a job in plain English, which is how
this is used in production:

    python -m computer_use.catalog --ask "what is the savings balance for member 100253?"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from computer_use.catalog.runner import for_the_agent, invoke
from computer_use.catalog.tools import (
    CAPABILITY_DIR,
    by_tool_name,
    catalog_tools,
    load_catalog,
    tool_name,
)
from computer_use.discovery.env import load_env
from computer_use.guardrails.policy import DEFAULT_ALLOWLIST_PATH, load_allowlist
from computer_use.schema.capability import ApprovalStatus

DEFAULT_MODEL = "claude-sonnet-5"

AGENT_BRIEF = """\
You are an assistant at a credit union. You have no access to the core banking
system except through the capabilities below, each of which drives the real
back-office application on your behalf.

Pick the one that fits, call it with typed arguments, and answer from what it
returns. If it reports a business outcome such as MEMBER_NOT_FOUND, that is the
answer and not a failure: say so plainly. If it reports an error, say what went
wrong rather than guessing at the data.
"""


def approve(args: argparse.Namespace, catalog) -> int:
    """Promote a reviewed draft so it may be invoked unattended.

    Deliberately a separate, explicit act. Discovery produces drafts, and a
    draft may be exercised with a person watching but never by a production
    agent. The record of who approved what is the artifact's history in version
    control: this rewrites a tracked file, so the commit is the audit trail.
    """
    wanted = tool_name(args.approve)
    artifact = by_tool_name(catalog).get(wanted)
    if artifact is None:
        raise SystemExit(f"no capability named {args.approve!r}")

    path = Path(args.capabilities) / f"{artifact.id}@{artifact.version}.json"
    promoted = artifact.model_copy(update={"status": ApprovalStatus.APPROVED})
    path.write_text(promoted.model_dump_json(indent=2), encoding="utf-8")

    print(f"{artifact.id} v{artifact.version}: {artifact.status.value} -> approved")
    print(f"  written to {path}")
    print(f"  risk tier  {artifact.risk_tier.value}")
    if artifact.risk_tier.value != "read_only":
        print("  note       approval permits unattended invocation. It does not waive the")
        print("             allowlist, nor the person asked before the step that commits.")
    print("  commit this change; the commit is the record of who approved it.")
    return 0


def show(catalog, as_json: bool) -> int:
    tools = catalog_tools(catalog)
    if as_json:
        print(json.dumps(tools, indent=2))
        return 0
    print(f"{len(tools)} capabilities an agent can discover and invoke:\n")
    for artifact, tool in zip(catalog, tools):
        args = ", ".join(
            f"{n}: {s['type']}" for n, s in tool["input_schema"]["properties"].items()
        )
        print(f"  {tool['name']}({args})")
        print(f"      {artifact.title}")
        print(f"      returns  {', '.join(o.name for o in artifact.outputs) or 'nothing'}")
        print(f"      outcomes {', '.join(o.code for o in artifact.outcomes) or 'none'}")
        print(f"      risk     {artifact.risk_tier.value}, approval {artifact.status.value}")
        print()
    return 0


async def call_one(args: argparse.Namespace, catalog) -> int:
    wanted = tool_name(args.call)
    artifact = by_tool_name(catalog).get(wanted)
    if artifact is None:
        raise SystemExit(f"no capability named {args.call!r}. Run without --call to list them.")

    arguments = {}
    for pair in args.arg:
        if "=" not in pair:
            raise SystemExit(f"--arg expects name=value, got {pair!r}")
        name, value = pair.split("=", 1)
        arguments[name] = value

    result = await invoke(
        artifact, arguments,
        policy=load_allowlist(args.allowlist), base_url=args.base_url,
        credentials=(args.user, args.password), evidence_root=args.evidence_dir,
        headed=args.headed, slow_mo_ms=args.slow,
    )
    print(json.dumps(for_the_agent(result), indent=2))
    return 0 if result.status in ("success", "business_outcome") else 1


async def ask(args: argparse.Namespace, catalog) -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY is not set; --ask needs a model. --call does not.")
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic()
    tools = catalog_tools(catalog)
    lookup = by_tool_name(catalog)
    messages = [{"role": "user", "content": args.ask}]

    print(f"task     : {args.ask}")
    print(f"catalog  : {', '.join(t['name'] for t in tools)}\n")

    for _ in range(4):
        response = await client.messages.create(
            model=args.model, max_tokens=1024,
            system=AGENT_BRIEF, tools=tools, messages=messages,
        )
        calls = [b for b in response.content if b.type == "tool_use"]
        said = " ".join(b.text for b in response.content if b.type == "text").strip()

        if not calls:
            print(f"\nanswer   : {said}")
            return 0

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for call in calls:
            artifact = lookup.get(call.name)
            print(f"agent calls {call.name}({json.dumps(dict(call.input))})")
            if artifact is None:
                payload = {"status": "failed", "error": "no such capability"}
            else:
                result = await invoke(
                    artifact, dict(call.input),
                    policy=load_allowlist(args.allowlist), base_url=args.base_url,
                    credentials=(args.user, args.password),
                    evidence_root=args.evidence_dir,
                    headed=args.headed, slow_mo_ms=args.slow,
                )
                payload = for_the_agent(result)
            print(f"  -> {json.dumps(payload)}\n")
            results.append({
                "type": "tool_result", "tool_use_id": call.id,
                "content": json.dumps(payload),
            })
        messages.append({"role": "user", "content": results})

    print("\nthe agent did not settle on an answer within four turns")
    return 1


async def run(args: argparse.Namespace) -> int:
    catalog = load_catalog(args.capabilities)
    if not catalog:
        raise SystemExit(f"no capabilities in {args.capabilities}")
    if args.call:
        return await call_one(args, catalog)
    if args.approve:
        return approve(args, catalog)
    if args.ask:
        return await ask(args, catalog)
    return show(catalog, args.json)


def main() -> int:
    load_env()
    parser = argparse.ArgumentParser(
        prog="python -m computer_use.catalog",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--call", metavar="CAPABILITY", help="invoke one by name")
    parser.add_argument("--arg", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--ask", metavar="TASK", help="give a model the catalog and a job")
    parser.add_argument("--approve", metavar="CAPABILITY",
                        help="promote a reviewed draft so it may be invoked unattended")
    parser.add_argument("--json", action="store_true", help="print the tool schemas verbatim")
    parser.add_argument("--capabilities", default=str(CAPABILITY_DIR))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default="http://127.0.0.1:8081")
    parser.add_argument("--allowlist", default=str(DEFAULT_ALLOWLIST_PATH))
    parser.add_argument("--evidence-dir", default="evidence")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--slow", type=int, default=0, metavar="MS")
    parser.add_argument("--user", default=os.environ.get("TARGET_APP_USERNAME", "operator1"))
    parser.add_argument("--password", default=os.environ.get("TARGET_APP_PASSWORD", ""))
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
