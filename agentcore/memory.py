"""Thin wrapper over AgentCore Memory control + data planes.

Control plane (bedrock-agentcore-control):  create_memory / update_memory / get_memory
Data plane    (bedrock-agentcore):          create_event / list_events / retrieve_memory_records

Startup flow (ensure_memory): read business_rules.yaml -> build strategies ->
create the memory resource (or reuse a cached one) -> poll until ACTIVE.

On-demand flow (sync_rules): re-read the file -> update_memory with the new
strategy overrides. LTM prompt changes take effect for *subsequent* extractions.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

import boto3

import rules as rules_mod
import settings, strategies


class Memory:
    def __init__(self, rules: dict[str, Any]):
        self.rules = rules
        region = rules_mod.region(rules)
        self.control = boto3.client("bedrock-agentcore-control", region_name=region)
        self.data = boto3.client("bedrock-agentcore", region_name=region)
        self.memory_id: str | None = None

    # ---- control plane ----------------------------------------------------

    def ensure_memory(self, reuse_cache: bool = True) -> str:
        """Create the memory resource from the rules file, or reuse a cached id."""
        if reuse_cache and settings.MEMORY_ID_CACHE.exists():
            self.memory_id = settings.MEMORY_ID_CACHE.read_text().strip()
            print(f"[memory] reusing cached memory_id={self.memory_id}")
            return self.memory_id

        strat = strategies.build_strategies_from_rules(self.rules)
        kwargs: dict[str, Any] = {
            "name": rules_mod.memory_name(self.rules),
            "description": "POC: STM + LTM with business-rule prompt overrides",
            "eventExpiryDuration": rules_mod.retention_days(self.rules),  # STM retention (days)
            "memoryStrategies": strat,
        }
        # Overrides invoke Bedrock in your account -> execution role required.
        if rules_mod.uses_overrides(self.rules):
            arn = settings.execution_role_arn(
                self.rules["memory"]["execution_role_arn_env"])
            if not arn:
                raise RuntimeError(
                    "This config uses override strategies. Set the "
                    f"{self.rules['memory']['execution_role_arn_env']} env var to "
                    "your AgentCore memory execution role ARN.")
            kwargs["memoryExecutionRoleArn"] = arn

        print(f"[memory] creating '{kwargs['name']}' with {len(strat)} strategies...")
        resp = self.control.create_memory(**kwargs)
        self.memory_id = resp["memory"]["id"]
        settings.MEMORY_ID_CACHE.write_text(self.memory_id)
        self._wait_active()
        return self.memory_id

    def sync_rules(self, new_rules: dict[str, Any]) -> None:
        """Push edited LTM prompt overrides on demand, without recreating memory."""
        assert self.memory_id, "call ensure_memory() first"
        strat = strategies.build_strategies_from_rules(new_rules)
        print(f"[memory] update_memory: syncing {len(strat)} strategies on demand...")
        self.control.update_memory(memoryId=self.memory_id, memoryStrategies=strat)
        self.rules = new_rules
        self._wait_active()

    def _wait_active(self, timeout_s: int = 300) -> None:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            status = self.control.get_memory(memoryId=self.memory_id)["memory"]["status"]
            if status == "ACTIVE":
                print(f"[memory] ACTIVE ({self.memory_id})")
                return
            if status == "FAILED":
                raise RuntimeError("memory resource entered FAILED state")
            time.sleep(5)
        raise TimeoutError("memory did not become ACTIVE in time")

    # ---- data plane: SHORT-TERM MEMORY -----------------------------------

    @staticmethod
    def new_session_id() -> str:
        # AgentCore requires session ids of at least 33 chars.
        return "sess-" + uuid.uuid4().hex + uuid.uuid4().hex[:4]

    def write_turn(self, actor_id: str, session_id: str,
                   role: str, text: str) -> None:
        """Append one conversational turn to STM (raw, verbatim)."""
        self.data.create_event(
            memoryId=self.memory_id,
            actorId=actor_id,
            sessionId=session_id,
            payload=[{"conversational": {"role": role.upper(),
                                         "content": {"text": text}}}],
        )

    def last_k_turns(self, actor_id: str, session_id: str, k: int) -> list[dict]:
        """Rehydrate the most recent raw turns from STM."""
        resp = self.data.list_events(
            memoryId=self.memory_id,
            actorId=actor_id,
            sessionId=session_id,
            maxResults=k,
        )
        return resp.get("events", [])

    # ---- data plane: LONG-TERM MEMORY ------------------------------------

    def retrieve_ltm(self, namespace: str, query: str, top_k: int = 5) -> list[dict]:
        """Semantic search over extracted LTM records in a namespace."""
        resp = self.data.retrieve_memory_records(
            memoryId=self.memory_id,
            namespace=namespace,
            searchCriteria={"searchQuery": query, "topK": top_k},
            maxResults=top_k,
        )
        return resp.get("memoryRecordSummaries", [])
