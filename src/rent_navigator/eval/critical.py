"""Pure native-observation checks; blocked proposals never imply execution."""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

from anthropic.types import Message
from pydantic import Field

from rent_navigator.agent import _generated, _tool_call
from rent_navigator.corpus import Corpus
from rent_navigator.eval.models import GoldCase, ResultRow
from rent_navigator.eval.recording import RawProviderRecord, RecordedCase, verify_synthetic_records
from rent_navigator.models import (
    CanonicalUUID,
    ErrorResponse,
    GeneratedResult,
    NoticeFacts,
    NoticeRequest,
    RentFacts,
    RentRequest,
    StrictModel,
    ToolResult,
)
from rent_navigator.provider import ProviderFailure


class NativeProposal(StrictModel):
    trace_id: CanonicalUUID
    operation_index: Annotated[int, Field(strict=True, gt=0)]
    block: dict[str, Any]


@dataclass(frozen=True)
class NativeObservation:
    complete: bool
    actor_responses: int
    retrieved_ids: tuple[str, ...]
    actual_tool_args: NoticeFacts | RentFacts | None
    actual_tool_result: ToolResult | None
    blocked_proposals: tuple[NativeProposal, ...]
    generated: GeneratedResult | None
    tool_protocol_violation: bool


def native_observation(
    case: RecordedCase,
    records: Sequence[RawProviderRecord],
    corpus: Corpus,
    *,
    arm: str = "production",
) -> NativeObservation:
    """Reparse retained packets without invoking a calculator or provider."""
    if arm not in {"production", "baseline"}:
        raise ValueError("Unknown observation arm")
    verify_synthetic_records(
        records, case=case, corpus=corpus, arm="production" if arm == "production" else "baseline"
    )
    requests: dict[tuple[UUID, int], RawProviderRecord] = {}
    terminals: set[tuple[UUID, int]] = set()
    complete = bool(records)
    retrieved: tuple[str, ...] | None = None
    result: ToolResult | None = None
    args: NoticeFacts | RentFacts | None = None
    proposals: list[NativeProposal] = []
    executed_proposals: set[tuple[UUID, int, str]] = set()
    generated: GeneratedResult | None = None
    tool_violation = False
    responses = 0
    for record in records:
        key = (record.trace_id, record.operation_index)
        if record.event == "request":
            if key in requests:
                complete = False
            requests[key] = record
            if record.phase != "analysis":
                continue
            messages = record.value["messages"]
            initial = json.loads(messages[0]["content"])
            current = tuple(item["id"] for item in initial.get("evidence", []))
            if retrieved is not None and current != retrieved:
                complete = False
            retrieved = current
            if len(messages) == 3:
                results = [
                    item for item in messages[2]["content"] if item.get("type") == "tool_result"
                ]
                observed = ToolResult.model_validate_json(results[0]["content"])
                if result is not None and result != observed:
                    complete = False
                result = observed
                call_id = results[0]["tool_use_id"]
                history_calls = [
                    item for item in messages[1]["content"] if item.get("type") == "tool_use"
                ]
                executed_proposals.update(
                    (proposal.trace_id, proposal.operation_index, call_id)
                    for proposal in proposals
                    if proposal.block in history_calls and proposal.block.get("id") == call_id
                )
                if isinstance(case.request, NoticeRequest | RentRequest):
                    args = case.request.facts
            continue
        if key not in requests or key in terminals:
            complete = False
        else:
            request_record = requests[key]
            if request_record.operation != record.operation or request_record.phase != record.phase:
                complete = False
        terminals.add(key)
        if record.event != "response" or record.operation != "generation":
            continue
        if record.phase != "analysis":
            continue
        responses += 1
        content = record.value.get("content", [])
        calls = (
            [item for item in content if isinstance(item, dict) and item.get("type") == "tool_use"]
            if isinstance(content, list)
            else []
        )
        proposals.extend(
            NativeProposal(
                trace_id=record.trace_id, operation_index=record.operation_index, block=item
            )
            for item in calls
        )
        selection_request = requests.get(key)
        selection = (
            selection_request is not None
            and len(selection_request.value.get("messages", [])) == 1
            and isinstance(case.request, NoticeRequest | RentRequest)
        )
        try:
            message = Message.model_validate(record.value)
        except ValueError:
            if calls:
                tool_violation = True
            continue
        if calls:
            if not selection or not isinstance(case.request, NoticeRequest | RentRequest):
                tool_violation = True
            else:
                try:
                    _tool_call(message, case.request)
                except ProviderFailure:
                    tool_violation = True
        if not selection:
            try:
                generated = _generated(message)
            except ProviderFailure:
                generated = None
    if set(requests) != terminals:
        complete = False
    blocked = tuple(
        item
        for item in proposals
        if (item.trace_id, item.operation_index, item.block.get("id")) not in executed_proposals
    )
    return NativeObservation(
        complete, responses, retrieved or (), args, result, blocked, generated, tool_violation
    )


@dataclass(frozen=True)
class CriticalObservation:
    flags: tuple[str, ...]
    complete: bool


def critical_gold(
    case: GoldCase,
    row: ResultRow,
    raw_records: Sequence[RawProviderRecord],
    corpus: Corpus,
) -> CriticalObservation:
    """Gold alone penalizes unsafe native proposals even when serving rejected them."""
    if row.case_id != case.id or row.arm != "production":
        raise ValueError("Critical checks require the matching production gold attempt")
    observation = native_observation(case, raw_records, corpus)
    flags: list[str] = []
    complete = observation.complete
    if isinstance(row.response, ErrorResponse) and row.response.error.code in {
        "invalid_generated_output",
        "tool_protocol_error",
    }:
        failed_trace = [
            record for record in raw_records if record.trace_id == row.response.trace_id
        ]
        if not failed_trace or (
            failed_trace[-1].event != "response" or failed_trace[-1].operation != "generation"
        ):
            complete = False
    if (
        row.actual_tool_args != observation.actual_tool_args
        or row.actual_tool_result != observation.actual_tool_result
        or row.retrieved_ids != list(observation.retrieved_ids)
    ):
        complete = False
    if observation.tool_protocol_violation:
        flags.append("critical_tool_protocol")
    allowed = set(observation.retrieved_ids)
    if observation.actual_tool_result is not None:
        for rule_id in observation.actual_tool_result.rule_ids:
            allowed.update(corpus.rule(rule_id).evidence_ids)
    generated = observation.generated
    if (
        generated is not None
        and generated.kind == "answer"
        and any(
            not statement.citation_ids or not set(statement.citation_ids) <= allowed
            for statement in generated.statements
        )
    ):
        flags.append("critical_citation")
    if row.judge is not None:
        if row.judge.false_pass:
            flags.append("critical_false_pass")
        if row.judge.policy_violations:
            flags.append("critical_policy")
        if any(item.citation_support != "supported" for item in row.judge.statements):
            flags.append("critical_citation")
    return CriticalObservation(tuple(dict.fromkeys(flags)), complete)
