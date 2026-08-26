# ============================================================================
# Pattern B — Terraform owns the memory (built-in + custom strategies); the
# AgentCore CLI owns only the runtime and consumes MEMORY_ID from the output.
#
#   Provision:  terraform apply
#   Wire agent: agentcore create --memory none           (don't let the CLI make one)
#               agentcore launch --env MEMORY_ID=$(terraform output -raw memory_id) \
#                                --env AWS_REGION=<region>
#
# One source of truth for memory (this file), one for the runtime (agentcore.json).
#
# STRATEGY RULES (why the layout below is what it is):
#   * Each FAMILY (SEMANTIC, USER_PREFERENCE, SUMMARY) appears ONCE — as a
#     built-in OR a custom override, never both. Different families coexist.
#   * Max 6 strategies per memory. Strategy mutations must be serial (depends_on).
#   * This file ships:  built-in semantic + user_preference,  custom summary.
#     To move a family between built-in and custom, use the commented templates.
#   * All prompts live in `locals` below — teams edit there, nothing else.
# ============================================================================

locals {
  memory_name = replace("${var.application}_${var.app_environment}", "-", "_")

  # --- Prompts for CUSTOM strategies (appended to the built-in system prompt) --
  prompt_summary_consolidation = <<-EOT
    Summarize the session as a handoff note: intent, what was tried, current
    status, and the single next action. Under 120 words. No PII beyond first name.
  EOT

  # Provided for the commented custom variants; unused until enabled.
  prompt_semantic_extraction = <<-EOT
    Extract only durable, business-relevant facts about the user: role, industry,
    the Gartner research topics they follow, and open questions. Ignore small talk
    and transient state. Never extract secrets or PII beyond a first name.
  EOT
  prompt_semantic_consolidation = <<-EOT
    When a new fact conflicts with a stored fact about the same attribute, replace
    the old one. Otherwise merge. Drop anything no longer relevant.
  EOT
  prompt_preference_extraction = <<-EOT
    Extract only communication and product preferences (channel, tone, language,
    configuration choices). Ignore one-off choices.
  EOT
  prompt_preference_consolidation = <<-EOT
    Prefer the most recently stated preference. Keep one value per dimension.
  EOT
}

# Model used for CUSTOM (override) strategies only. Billed to this account and
# must be enabled in Bedrock -> Model access. Move to variables.tf if preferred.
variable "memory_override_model_id" {
  type    = string
  default = "us.anthropic.claude-3-5-haiku-20241022-v1:0"
}

# Raw-event (short-term) retention in days (7-365).
variable "event_expiry_days" {
  type    = number
  default = 90
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

# --- Execution role ----------------------------------------------------
# AgentCore Memory assumes this role to invoke the foundation models that
# perform extraction/consolidation for the CUSTOM strategies. Built-in
# strategies run in a service-managed account and don't use it, but it's
# harmless to keep.
data "aws_iam_policy_document" "memory_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }

    # Confused-deputy guards, per the AgentCore docs. SourceArn is scoped to the
    # account/region rather than to memory/* - the service does not guarantee the
    # resource segment it presents when assuming this role.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values = [
        format(
          "arn:%s:bedrock-agentcore:%s:%s:*",
          data.aws_partition.current.partition,
          data.aws_region.current.region,
          data.aws_caller_identity.current.account_id,
        )
      ]
    }
  }
}

resource "aws_iam_role" "memory" {
  name               = format("%s-memory-execution", local.memory_name)
  description        = format("Execution role for AgentCore memory %s", local.memory_name)
  assume_role_policy = data.aws_iam_policy_document.memory_assume_role.json
  tags               = module.data-utils.tags
}

# AWS publishes a managed policy for exactly this role. It grants
# bedrock:InvokeModel/InvokeModelWithResponseStream on foundation models and
# inference profiles, plus the bedrock-mantle actions the service needs to
# process memories.
resource "aws_iam_role_policy_attachment" "memory" {
  role = aws_iam_role.memory.name
  policy_arn = format(
    "arn:%s:iam::aws:policy/AmazonBedrockAgentCoreMemoryBedrockModelInferenceExecutionRolePolicy",
    data.aws_partition.current.partition,
  )
}

# --- Memory ------------------------------------------------------------
# event_expiry_duration governs raw conversational events only (7-365 days).
# Records extracted into the strategies below are retained independently.
resource "aws_bedrockagentcore_memory" "this" {
  name                      = local.memory_name
  description               = "AgentCore memory (Pattern B): facts, preferences, and session summaries."
  event_expiry_duration     = var.event_expiry_days
  memory_execution_role_arn = aws_iam_role.memory.arn
  tags                      = module.data-utils.tags

  # NOTE: indexed_key declares *custom metadata* attributes the service extracts
  # for use with metadataFilters on RetrieveMemoryRecords. Actor and session are
  # already carried by the namespaces below and must not be listed here. Adding a
  # key forces replacement and cannot be removed once declared — add only when
  # the filterable attributes are known.
}

# --- Strategies --------------------------------------------------------
# Shared /user/{actorId} root so one hierarchical read returns everything about
# a user. Facts and preferences persist across sessions; summaries append
# {sessionId}. Chained with depends_on (serial strategy mutations required).

# ============================ SECTION A: BUILT-IN ============================
# Service-managed prompt + model. No account LLM cost, no execution role needed.

resource "aws_bedrockagentcore_memory_strategy" "semantic" {
  name        = format("%s_facts", local.memory_name)
  memory_id   = aws_bedrockagentcore_memory.this.id
  type        = "SEMANTIC"
  description = "Extracts durable facts from conversations."
  namespaces  = ["/user/{actorId}/facts"]

  # --- Move to CUSTOM: delete the `type` line above, then uncomment. Do NOT
  #     also keep a custom SEMANTIC_OVERRIDE elsewhere - one semantic per memory.
  # memory_execution_role_arn = aws_iam_role.memory.arn
  # type                      = "CUSTOM"
  # configuration {
  #   type = "SEMANTIC_OVERRIDE"
  #   extraction    { model_id = var.memory_override_model_id, append_to_prompt = local.prompt_semantic_extraction }
  #   consolidation { model_id = var.memory_override_model_id, append_to_prompt = local.prompt_semantic_consolidation }
  # }
}

resource "aws_bedrockagentcore_memory_strategy" "user_preference" {
  name        = format("%s_preferences", local.memory_name)
  memory_id   = aws_bedrockagentcore_memory.this.id
  type        = "USER_PREFERENCE"
  description = "Captures user preferences, choices, and interaction style."
  namespaces  = ["/user/{actorId}/preferences"]

  depends_on = [aws_bedrockagentcore_memory_strategy.semantic]

  # --- Move to CUSTOM (USER_PREFERENCE_OVERRIDE uses extraction + consolidation):
  # memory_execution_role_arn = aws_iam_role.memory.arn
  # type                      = "CUSTOM"
  # configuration {
  #   type = "USER_PREFERENCE_OVERRIDE"
  #   extraction    { model_id = var.memory_override_model_id, append_to_prompt = local.prompt_preference_extraction }
  #   consolidation { model_id = var.memory_override_model_id, append_to_prompt = local.prompt_preference_consolidation }
  # }
}

# ============================ SECTION B: CUSTOM =============================
# Your prompts + model. Uses the execution role above and bills model usage to
# this account. Added ALONGSIDE the built-ins - a different family, so no clash.

resource "aws_bedrockagentcore_memory_strategy" "summary_custom" {
  name                      = format("%s_summaries", local.memory_name)
  memory_id                 = aws_bedrockagentcore_memory.this.id
  memory_execution_role_arn = aws_iam_role.memory.arn
  type                      = "CUSTOM"
  description               = "Session summary shaped by our handoff-note prompt."
  namespaces                = ["/user/{actorId}/summaries/{sessionId}"]

  configuration {
    type = "SUMMARY_OVERRIDE" # summary supports consolidation ONLY (no extraction)
    consolidation {
      model_id         = var.memory_override_model_id
      append_to_prompt = local.prompt_summary_consolidation
    }
  }

  depends_on = [aws_bedrockagentcore_memory_strategy.user_preference]

  # --- Prefer the plain built-in summary instead? Replace this whole resource
  #     with a SUMMARIZATION built-in:
  # resource "aws_bedrockagentcore_memory_strategy" "summary" {
  #   name        = format("%s_summaries", local.memory_name)
  #   memory_id   = aws_bedrockagentcore_memory.this.id
  #   type        = "SUMMARIZATION"
  #   description = "Maintains a rolling summary of each session."
  #   namespaces  = ["/user/{actorId}/summaries/{sessionId}"]
  #   depends_on  = [aws_bedrockagentcore_memory_strategy.user_preference]
  # }
}

# --- Outputs (consumed by the AgentCore CLI runtime) -------------------
output "memory_id" {
  description = "Pass to the runtime: agentcore launch --env MEMORY_ID=$(terraform output -raw memory_id)"
  value       = aws_bedrockagentcore_memory.this.id
}

output "memory_arn" {
  description = "ARN of the memory resource."
  value       = aws_bedrockagentcore_memory.this.arn
}

output "memory_execution_role_arn" {
  description = "ARN of the execution role the memory service assumes."
  value       = aws_iam_role.memory.arn
}
