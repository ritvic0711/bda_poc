"""End-to-end demo.

  python -m poc.demo               # create/reuse memory, seed a session, retrieve
  python -m poc.demo --live        # also call Bedrock for replies
  python -m poc.demo --sync        # push edited LTM prompts on demand (UpdateMemory)
  python -m poc.demo --catalog     # just print all strategies and exit

Prereqs: AWS creds with AgentCore access; for override strategies also set
MEMORY_EXECUTION_ROLE_ARN. Edit config/business_rules.yaml to change behavior.
"""
from __future__ import annotations

import argparse
import time

import rules as rules_mod
import strategies
from agent import Agent
from memory import Memory


def print_catalog() -> None:
    print("\nALL AGENTCORE MEMORY STRATEGIES")
    print("-" * 78)
    for name, tier, ext, con, note in strategies.STRATEGY_CATALOG:
        steps = "+".join([s for s, on in (("extract", ext), ("consolidate", con)) if on])
        print(f"{name}\n    tier={tier}  steps={steps}\n    {note}\n")


SEED_CONVERSATION = [
    ("USER", "Hi, I'm Priya. I'm on the Growth plan and I mostly use the Slack integration."),
    ("ASSISTANT", "Thanks Priya — I can see the Growth plan supports the Slack integration."),
    ("USER", "Please always email me rather than call, and keep replies short."),
    ("ASSISTANT", "Noted: email only, concise replies."),
    ("USER", "My webhook has been failing since this morning, can you look into it?"),
    ("ASSISTANT", "I've logged the webhook failure and started investigating."),
]


def run(live: bool) -> None:
    rules = rules_mod.load_rules()
    mem = Memory(rules)
    mem.ensure_memory()

    actor_id = "priya-001"
    agent = Agent(mem, actor_id=actor_id, live=live)

    print("\n[demo] seeding a conversation into STM...")
    for role, text in SEED_CONVERSATION:
        mem.write_turn(actor_id, agent.session_id, role, text)

    print("[demo] STM written. LTM extraction is async — waiting ~60s...")
    time.sleep(60)

    # Retrieve extracted LTM per namespace to prove the overrides worked.
    for key in ("semantic", "user_preference"):
        cfg = rules["ltm"][key]
        ns = rules_mod.resolve_namespace(cfg["namespace"], actor_id, agent.session_id)
        print(f"\n[LTM:{key}] namespace={ns}")
        for r in mem.retrieve_ltm(ns, query="plan integration preferences issues", top_k=5):
            print("   -", r.get("content", {}).get("text", ""))

    # New turn: agent assembles business rules + LTM + STM automatically.
    print("\n[demo] new user turn (watch the assembled context):")
    print("REPLY:", agent.send("Any update on my webhook?"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="call Bedrock for replies")
    ap.add_argument("--sync", action="store_true", help="push edited LTM prompts on demand")
    ap.add_argument("--catalog", action="store_true", help="print strategies and exit")
    args = ap.parse_args()

    if args.catalog:
        print_catalog()
        return

    if args.sync:
        rules = rules_mod.load_rules()
        mem = Memory(rules)
        mem.ensure_memory()
        mem.sync_rules(rules_mod.load_rules())  # re-read file, apply on demand
        print("[demo] LTM prompt overrides synced. Applies to future extractions.")
        return

    print_catalog()
    run(live=args.live)


if __name__ == "__main__":
    main()
