"""Thin wrapper around the Bedrock Converse API using *forced tool use*.

Why tool use? Analogy: instead of asking a temp worker to "write the answer
somewhere on this napkin", you hand them a form with labelled boxes and say
"you may only answer by filling in this form." Bedrock then returns the form
contents as structured JSON (the toolUse block), which we STILL double-check
with Pydantic: forced tool use makes the right shape very likely, not guaranteed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from time import perf_counter
from typing import Optional

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel

from claimlens.llm.pricing import estimate_cost

log = logging.getLogger(__name__)


TRANSIENT_CODES = {"ThrottlingException", "ModelTimeoutException", "ServiceUnavailableException",
                   "InternalServerException", "ModelNotReadyException", "ModelErrorException"}


class LLMError(RuntimeError):
    """Anything that goes wrong talking to the model. Never includes document text.
    `transient=True` means retrying later may succeed (throttling, timeouts); False means it won't."""

    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


@dataclass
class ToolCallResult:
    data: dict
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    model_id: str


class BedrockClient:
    def __init__(self, model_id: str, region: str, runtime_client=None):
        if not model_id:
            raise LLMError(
                "No model configured. Set CLAIMLENS_MODEL_ID to a model or inference-profile ID "
                "that you have enabled in the Bedrock console (Model access)."
            )
        self.model_id = model_id
        # 'adaptive' retry mode backs off automatically when Bedrock throttles us.
        self._client = runtime_client or boto3.client(
            "bedrock-runtime",
            region_name=region,
            config=Config(retries={"max_attempts": 6, "mode": "adaptive"}, read_timeout=120),
        )

    def call_tool(
        self,
        *,
        system: str,
        user_text: str,
        tool_name: str,
        tool_description: str,
        schema_model: type[BaseModel],
        max_tokens: int = 1500,
    ) -> ToolCallResult:
        request = {
            "modelId": self.model_id,
            "system": [{"text": system}],
            "messages": [{"role": "user", "content": [{"text": user_text}]}],
            "inferenceConfig": {"temperature": 0.0, "maxTokens": max_tokens},
            "toolConfig": {
                "tools": [{"toolSpec": {
                    "name": tool_name,
                    "description": tool_description,
                    "inputSchema": {"json": schema_model.model_json_schema()},
                }}],
                "toolChoice": {"tool": {"name": tool_name}},  # force this tool
            },
        }
        start = perf_counter()
        try:
            response = self._client.converse(**request)
        except (ClientError, BotoCoreError) as exc:
            code = exc.response["Error"]["Code"] if isinstance(exc, ClientError) else exc.__class__.__name__
            transient = code in TRANSIENT_CODES or not isinstance(exc, ClientError)  # network-level errors are retryable
            raise LLMError(f"Bedrock call failed: {code}", transient=transient) from exc
        latency_ms = int((perf_counter() - start) * 1000)

        if response.get("stopReason") == "max_tokens":
            raise LLMError("Model output was cut off (max_tokens). Raise max_tokens or shorten the input.")

        tool_input: Optional[dict] = None
        for block in response.get("output", {}).get("message", {}).get("content", []):
            tool_use = block.get("toolUse")
            if tool_use and tool_use.get("name") == tool_name:
                tool_input = tool_use.get("input")
                break
        if tool_input is None:
            raise LLMError("Model did not return the expected tool call.")

        usage = response.get("usage", {})
        in_tok, out_tok = usage.get("inputTokens", 0), usage.get("outputTokens", 0)
        cost = estimate_cost(self.model_id, in_tok, out_tok)
        # Log metrics only: NEVER log prompts, document text, or model output.
        log.info("bedrock tool=%s in=%d out=%d cost=$%.5f latency=%dms", tool_name, in_tok, out_tok, cost, latency_ms)
        return ToolCallResult(tool_input, in_tok, out_tok, cost, latency_ms, self.model_id)
