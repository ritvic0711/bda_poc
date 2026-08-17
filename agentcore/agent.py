"""A minimal agent showing how the business rules feed BOTH tiers at runtime.

Per user message the agent assembles context in this order:
    1. STM system preamble  (business rules from business_rules.yaml)
    2. LTM records          (retrieved facts + preferences for this actor)
    3. STM turns            (last-k raw turns of this session)
    4. the new user message

The LLM call is optional (--live) so the demo runs end-to-end even without
Bedrock model access; without it the agent just prints the assembled context
and a stub reply, then writes both turns back to STM.
"""
from __future__ import annotations

from typing import Any

import rules as rules_mod
from memory import Memory


class Agent:
    def __init__(self, mem: Memory, actor_id: str, live: bool = False):
        self.mem = mem
        self.actor_id = actor_id
        self.live = live
        self.session_id = mem.new_session_id()
        # Startup: pull the business-rule preamble once.
        self.preamble = rules_mod.build_stm_preamble(mem.rules)
        self.k = rules_mod.rehydrate_turns(mem.rules)

    def _assemble_context(self, user_msg: str) -> str:
        rules = self.mem.rules
        blocks = [f"[BUSINESS RULES]\n{self.preamble}"]

        # LTM: facts + preferences for this actor
        for key in ("semantic", "user_preference"):
            cfg = rules.get("ltm", {}).get(key, {})
            if not cfg.get("enabled"):
                continue
            ns = rules_mod.resolve_namespace(cfg["namespace"], self.actor_id, self.session_id)
            records = self.mem.retrieve_ltm(ns, query=user_msg, top_k=3)
            if records:
                lines = [r.get("content", {}).get("text", "") for r in records]
                blocks.append(f"[LTM {key}]\n" + "\n".join(f"- {l}" for l in lines if l))

        # STM: recent raw turns
        turns = self.mem.last_k_turns(self.actor_id, self.session_id, self.k)
        if turns:
            blocks.append(f"[RECENT TURNS x{len(turns)}]")

        blocks.append(f"[USER]\n{user_msg}")
        return "\n\n".join(blocks)

    def _reply(self, context: str) -> str:
        if not self.live:
            return "(stub reply — run with --live to call Bedrock)"
        # Live path: call Bedrock Converse with the assembled context.
        import boto3
        rt = boto3.client("bedrock-runtime", region_name=rules_mod.region(self.mem.rules))
        resp = rt.converse(
            modelId=rules_mod.ltm_model_id(self.mem.rules),
            messages=[{"role": "user", "content": [{"text": context}]}],
        )
        return resp["output"]["message"]["content"][0]["text"]

    def send(self, user_msg: str) -> str:
        context = self._assemble_context(user_msg)
        print("\n" + "=" * 70 + "\nASSEMBLED CONTEXT:\n" + context + "\n" + "=" * 70)
        reply = self._reply(context)
        # Persist both turns to STM; LTM extraction happens async server-side.
        self.mem.write_turn(self.actor_id, self.session_id, "USER", user_msg)
        self.mem.write_turn(self.actor_id, self.session_id, "ASSISTANT", reply)
        return reply
