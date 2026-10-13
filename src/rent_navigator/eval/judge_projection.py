"""Discard reasoning payloads before serialization, retaining only closed counts."""

import re
from typing import Annotated, Any, Literal, Self

from anthropic.types import Message, RedactedThinkingBlock, TextBlock, ThinkingBlock
from anthropic.types import Usage as SDKUsage
from anthropic.types.output_tokens_details import OutputTokensDetails
from pydantic import Field, field_validator, model_validator

from rent_navigator.models import StrictModel

PROJECTION_POLICY = {
    "version": 1,
    "content": "known reasoning prefix, exactly one final text; otherwise invalid",
    "omission": "discard thinking, signatures and redacted data before serialization",
    "usage": "inclusive output; optional strict subset thinking count, never added",
}
Count = Annotated[int, Field(strict=True, ge=0)]


class JudgeProjection(StrictModel):
    version: Literal[1]
    layout: Literal["text", "thinking_then_text", "invalid"]
    omitted_blocks: Count
    thinking_blocks: Count
    redacted_thinking_blocks: Count
    thinking_tokens: Count | None

    @field_validator("version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Projection version must be an integer")
        return value

    @model_validator(mode="after")
    def counts_match(self) -> Self:
        if self.omitted_blocks != self.thinking_blocks + self.redacted_thinking_blocks:
            raise ValueError("Omitted block count mismatch")
        if self.layout == "text" and self.omitted_blocks != 0:
            raise ValueError("Text layout cannot omit reasoning blocks")
        if self.layout == "thinking_then_text" and self.omitted_blocks == 0:
            raise ValueError("Reasoning layout requires an omitted block")
        return self


def _thinking_tokens(usage: object) -> int | None:
    if not isinstance(usage, SDKUsage) and type(usage) is not dict:
        return None
    total = (
        usage.get("output_tokens") if type(usage) is dict else getattr(usage, "output_tokens", None)
    )
    details = (
        usage.get("output_tokens_details")
        if type(usage) is dict
        else getattr(usage, "output_tokens_details", None)
    )
    value = (
        details.get("thinking_tokens")
        if type(details) is dict
        else getattr(details, "thinking_tokens", None)
        if isinstance(details, OutputTokensDetails)
        else None
    )
    return value if type(total) is int and type(value) is int and 0 <= value <= total else None


def project_judge_response(response: Message) -> tuple[Message, JudgeProjection]:
    existing = getattr(response, "_judge_projection", None)
    if isinstance(existing, JudgeProjection):
        return response, existing
    content = getattr(response, "content", None)
    blocks = content if type(content) is list else []
    thinking = sum(isinstance(block, ThinkingBlock) for block in blocks)
    redacted = sum(isinstance(block, RedactedThinkingBlock) for block in blocks)
    texts = [
        block
        for block in blocks
        if isinstance(block, TextBlock) and type(getattr(block, "text", None)) is str
    ]
    valid = (
        len(texts) == 1
        and bool(blocks)
        and blocks[-1] is texts[0]
        and all(isinstance(block, ThinkingBlock | RedactedThinkingBlock) for block in blocks[:-1])
    )
    projection = JudgeProjection(
        version=1,
        layout=("thinking_then_text" if thinking + redacted else "text") if valid else "invalid",
        omitted_blocks=thinking + redacted,
        thinking_blocks=thinking,
        redacted_thinking_blocks=redacted,
        thinking_tokens=_thinking_tokens(getattr(response, "usage", None)),
    )
    usage = getattr(response, "usage", None)
    if isinstance(usage, SDKUsage) and "output_tokens_details" in usage.model_fields_set:
        usage = usage.model_copy(
            update={
                "output_tokens_details": OutputTokensDetails.model_construct(
                    thinking_tokens=projection.thinking_tokens
                )
                if projection.thinking_tokens is not None
                else None
            }
        )
    fields: dict[str, Any] = {
        name: getattr(response, name, None)
        for name in ("id", "type", "role", "model", "stop_reason", "stop_sequence")
        if name in response.model_fields_set
    }
    fields["content"] = [TextBlock(type="text", text=texts[0].text)] if len(texts) == 1 else []
    safe = Message.model_construct(**fields)
    if "usage" in response.model_fields_set:
        object.__setattr__(safe, "usage", usage)
        safe.__pydantic_fields_set__.add("usage")
    request_id = getattr(response, "_request_id", None)
    if type(request_id) is str and re.fullmatch(r"req_[A-Za-z0-9_-]{1,120}", request_id):
        object.__setattr__(safe, "_request_id", request_id)
    object.__setattr__(safe, "_judge_projection", projection)
    return safe, projection


def validate_projection_raw(raw: dict[str, Any]) -> JudgeProjection:
    projection = JudgeProjection.model_validate(raw.get("judge_projection"), strict=True)
    content = raw.get("content")
    if (
        type(content) is not list
        or len(content) > 1
        or any(
            type(block) is not dict
            or set(block) != {"type", "text"}
            or block["type"] != "text"
            or type(block["text"]) is not str
            for block in content
        )
    ):
        raise ValueError("Projected response cannot retain reasoning or tools")
    if projection.layout != "invalid" and len(content) != 1:
        raise ValueError("Valid projection requires exactly one final text")
    if projection.thinking_tokens != _thinking_tokens(raw.get("usage")):
        raise ValueError("Projection thinking count differs from usage")
    return projection


def replay_judge_response(raw: dict[str, Any]) -> Message:
    projection = validate_projection_raw(raw)
    response = Message.model_validate(
        {key: value for key, value in raw.items() if key != "judge_projection"}
    )
    object.__setattr__(response, "_judge_projection", projection)
    return response


def thinking_metrics(response: Message) -> dict[str, Any]:
    safe, projection = project_judge_response(response)
    total = getattr(getattr(safe, "usage", None), "output_tokens", None)
    total = total if type(total) is int and total >= 0 else None
    return {
        "thinking_blocks": projection.thinking_blocks,
        "redacted_thinking_blocks": projection.redacted_thinking_blocks,
        "thinking_present": projection.omitted_blocks > 0,
        "output_tokens": total,
        "thinking_tokens": projection.thinking_tokens,
        "thinking_output_share": projection.thinking_tokens / total
        if projection.thinking_tokens is not None and total is not None and total > 0
        else None,
    }
