"""Full-content recording restricted to declared synthetic scenarios."""

import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Any, Literal, TextIO
from uuid import UUID

from anthropic import transform_schema
from anthropic.types import Message, MessageTokensCount
from pydantic import Field

from rent_navigator.agent import (
    _FINAL_CHOICE,
    _SELECTION_CHOICE,
    _final_schema,
    _retrieve,
    _system,
    _tool_call,
)
from rent_navigator.context import ContextProvenance, context_provenance, query_for_request
from rent_navigator.corpus import Corpus
from rent_navigator.eval.models import GoldCase
from rent_navigator.eval.provider_evidence import capture_provider_response
from rent_navigator.extract import EXTRACTION_SYSTEM
from rent_navigator.guards import redact_text
from rent_navigator.index import SearchHit, build_index, search
from rent_navigator.model_policy import ACTOR_MODEL, MODEL_POLICIES
from rent_navigator.models import (
    AskRequest,
    CanonicalUUID,
    Extraction,
    ExtractRequest,
    NoticeFacts,
    RentFacts,
    StrictModel,
    ToolResult,
    provider_tool_definitions,
)
from rent_navigator.provider import MessagesPort, ProviderFailure, _error_code
from rent_navigator.security_cases import SecurityCase, load_security_cases

RecordedCase = GoldCase | SecurityCase


class RawProviderRecord(StrictModel):
    run_id: CanonicalUUID
    attempt_id: CanonicalUUID
    trace_id: CanonicalUUID
    phase: Literal["extraction", "analysis"]
    operation: Literal["count_tokens", "generation"]
    operation_index: Annotated[int, Field(strict=True, gt=0)]
    event: Literal["request", "response", "failure"]
    context_provenance: ContextProvenance
    value: dict[str, Any]


@dataclass(frozen=True)
class SyntheticAllowlist:
    """An explicit fixture declaration; never inferred from arbitrary request text."""

    cases: tuple[str, ...]

    @classmethod
    def from_fixtures(cls, cases: Sequence[RecordedCase]) -> "SyntheticAllowlist":
        return cls(tuple(case.model_dump_json() for case in cases))

    def require(self, case: RecordedCase) -> None:
        if case.model_dump_json() not in self.cases:
            raise ValueError("Scenario is outside the declared synthetic allowlist")


class SyntheticRecorder:
    """Wrap an injected port, validating prepared inputs before retaining content."""

    def __init__(
        self,
        port: MessagesPort,
        *,
        case: RecordedCase,
        allowlist: SyntheticAllowlist,
        corpus: Corpus,
        stream: TextIO,
        run_id: UUID,
        attempt_id: UUID,
    ) -> None:
        allowlist.require(case)
        if isinstance(case, SecurityCase) and case not in load_security_cases(corpus=corpus):
            raise ValueError("Security recording requires an unchanged packaged fixture")
        self._port = port
        self._case = case
        self._corpus = corpus
        self._stream = stream
        self._run_id = run_id
        self._attempt_id = attempt_id
        self._trace_id: UUID | None = None
        self._request: AskRequest | None = None
        self._phase: Literal["extraction", "analysis"] = "analysis"
        self._arm: Literal["production", "baseline"] = "production"
        self._returned: list[dict[str, Any]] = []
        self._index = 0
        self.actual_tool_args: NoticeFacts | RentFacts | None = None
        self.actual_tool_result: ToolResult | None = None
        self.expected_retrieved_ids: tuple[str, ...] | None = None
        self._preflight: dict[str, Any] | None = None
        self._provenance: ContextProvenance | None = None

    def bind(
        self,
        trace_id: UUID,
        phase: Literal["extraction", "analysis"],
        request: AskRequest,
        *,
        arm: Literal["production", "baseline"] = "production",
    ) -> None:
        self._trace_id, self._phase, self._request = trace_id, phase, request
        self._arm = arm
        self._index = 0
        self._returned = []
        self._preflight = None
        self._provenance = None
        self.expected_retrieved_ids = None
        self.actual_tool_args = None
        self.actual_tool_result = None

    def observe_retrieval(self, hits: tuple[SearchHit, ...]) -> None:
        """Retain only actual validated seed observations from this bound analysis."""
        if self._request is None or self._phase != "analysis" or self._arm != "production":
            raise ValueError("Retrieval observation requires bound production analysis")
        if self._provenance is not None:
            raise ValueError("Analysis context is already recorded")
        self.expected_retrieved_ids = None
        chunks, _ = _retrieve(query_for_request(self._request), lambda _: hits, self._corpus)
        self.expected_retrieved_ids = tuple(chunk.id for chunk in chunks)

    def _bound_provenance(self) -> ContextProvenance:
        if self._phase == "extraction":
            return ContextProvenance(
                retrieved_evidence_ids=[],
                foundation_evidence_ids=[],
                initial_context_evidence_ids=[],
            )
        if self._request is None:
            raise ValueError("Missing bound synthetic request")
        if self._arm == "production" and self.expected_retrieved_ids is None:
            raise ValueError("Production retrieval has not been observed")
        return context_provenance(
            self._request.mode, self._arm, self.expected_retrieved_ids or (), self._corpus
        )

    def _evidence(self, value: object) -> tuple[str, ...]:
        if not isinstance(value, list) or len(value) > len(self._corpus.chunks):
            raise ValueError("Invalid synthetic evidence")
        seen: set[str] = set()
        identifiers: list[str] = []
        for item in value:
            if not isinstance(item, dict) or set(item) != {"id", "heading", "text"}:
                raise ValueError("Invalid synthetic evidence")
            chunk = self._corpus.chunk(item["id"])
            if item["text"] != chunk.text or item["heading"] != chunk.heading or chunk.id in seen:
                raise ValueError("Invalid synthetic evidence")
            seen.add(chunk.id)
            identifiers.append(chunk.id)
        return tuple(identifiers)

    def _prepared(self, payload: dict[str, Any], operation: str) -> None:
        allowed = {
            "model",
            "system",
            "messages",
            "thinking",
            "tools",
            "tool_choice",
            "output_config",
            "timeout",
            "max_tokens",
            "stream",
            "service_tier",
            "extra_body",
        }
        if set(payload) - allowed or self._trace_id is None or self._request is None:
            raise ValueError("Invalid synthetic request")
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError("Invalid synthetic request")
        allowed_ids = self._context(messages)
        # Reuse fixed prompt/schema builders only; execution remains in the serving entry.
        expected_config: dict[str, Any] = {
            "model": ACTOR_MODEL,
            "thinking": {"type": "disabled"},
        }
        schema: dict[str, Any] | None = None
        if self._phase == "extraction":
            expected_config["system"] = EXTRACTION_SYSTEM
            schema = transform_schema(Extraction.model_json_schema())
        elif self._request.mode == "question":
            expected_config["system"] = _system("question", self._arm)
            schema = _final_schema(self._arm, allowed_ids)
        else:
            selection = len(messages) == 1
            expected_config.update(
                system=_system(self._request.mode, self._arm, selection=selection),
                tools=provider_tool_definitions(),
                tool_choice=_SELECTION_CHOICE if selection else _FINAL_CHOICE,
            )
            if not selection:
                schema = _final_schema(self._arm, allowed_ids)
        if schema is not None:
            expected_config["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
        if operation == "generation":
            expected_config.update(
                max_tokens=MODEL_POLICIES[ACTOR_MODEL].max_output_tokens,
                stream=False,
                service_tier="standard_only",
                extra_body={"temperature": 0},
            )
        if {
            key: value for key, value in payload.items() if key not in {"messages", "timeout"}
        } != expected_config:
            raise ValueError("Unapproved prepared configuration")
        timeout = payload.get("timeout")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or not math.isfinite(timeout)
            or timeout <= 0
            or (timeout > 45 and not math.isclose(timeout, 45, rel_tol=0, abs_tol=1e-9))
        ):
            raise ValueError("Invalid synthetic timeout")
        provenance = self._bound_provenance()
        if self._provenance is not None and self._provenance != provenance:
            raise ValueError("Synthetic context changed within its analysis trace")
        self._provenance = provenance

    def _context(self, messages: list[Any]) -> set[str]:
        if self._request is None:
            raise ValueError("Missing bound synthetic request")
        first = messages[0]
        if not isinstance(first, dict) or set(first) != {"role", "content"}:
            raise ValueError("Invalid synthetic request")
        if first["role"] != "user" or not isinstance(first["content"], str):
            raise ValueError("Invalid synthetic request")
        if self._phase == "extraction":
            if (
                not isinstance(self._case, GoldCase)
                or self._case.letter is None
                or len(messages) != 1
            ):
                raise ValueError("Invalid synthetic extraction")
            if first["content"] != redact_text(self._case.letter):
                raise ValueError("Invalid synthetic extraction")
            return set()
        initial = json.loads(first["content"])
        if not isinstance(initial, dict):
            raise ValueError("Invalid synthetic analysis")
        expected: dict[str, Any] = {"mode": self._request.mode}
        if self._request.mode == "question":
            expected["question"] = redact_text(self._request.question)
        else:
            expected.update(confirmed=True, facts=self._request.facts.model_dump(mode="json"))
        provenance = self._bound_provenance()
        initial_ids = tuple(provenance.initial_context_evidence_ids)
        if "evidence" in initial:
            if self._arm != "production":
                raise ValueError("Baseline cannot receive retrieved evidence")
            if self._evidence(initial.pop("evidence")) != initial_ids:
                raise ValueError("Synthetic context differs from its observed seed composition")
        elif self._arm == "production":
            raise ValueError("Production context requires canonical evidence")
        allowed = set(initial_ids)
        if isinstance(self._case, SecurityCase):
            sidecar = self._case.injected_retrieved_text
            if self._arm == "production" and sidecar is not None:
                expected["untrusted_retrieved_text"] = sidecar
        if initial != expected:
            raise ValueError("Invalid synthetic analysis")
        if len(messages) == 1:
            return allowed
        if len(messages) != 3 or self._request.mode == "question":
            raise ValueError("Invalid synthetic continuation")
        history, followup = messages[1:]
        if (
            not isinstance(history, dict)
            or set(history) != {"role", "content"}
            or history["role"] != "assistant"
            or not any(history["content"] == value["content"] for value in self._returned)
            or not isinstance(followup, dict)
            or set(followup) != {"role", "content"}
            or followup["role"] != "user"
            or not isinstance(followup["content"], list)
        ):
            raise ValueError("Invalid synthetic continuation")
        returned = next(value for value in self._returned if value["content"] == history["content"])
        call = _tool_call(Message.model_validate(returned), self._request)
        results = [item for item in followup["content"] if item.get("type") == "tool_result"]
        if len(results) != 1 or results[0].get("tool_use_id") != call.id:
            raise ValueError("Missing synthetic tool execution evidence")
        result = ToolResult.model_validate_json(results[0]["content"])
        if call.name != result.tool:
            raise ValueError("Mismatched synthetic tool execution")
        rule_ids = dict.fromkeys(
            identifier
            for rule in result.rule_ids
            for identifier in self._corpus.rule(rule).evidence_ids
        )
        allowed.update(rule_ids)
        expected_added = tuple(
            identifier for identifier in rule_ids if identifier not in initial_ids
        )
        added: list[tuple[str, ...]] = []
        for item in followup["content"]:
            if item.get("type") == "tool_result":
                if set(item) != {"type", "tool_use_id", "content"}:
                    raise ValueError("Invalid synthetic tool result")
            elif item.get("type") == "text" and set(item) == {"type", "text"}:
                extra = json.loads(item["text"])
                if set(extra) == {"evidence"}:
                    added.append(self._evidence(extra["evidence"]))
                elif set(extra) == {"money_display_cad"}:
                    amounts = extra["money_display_cad"]
                    if not isinstance(amounts, dict) or set(amounts) != {
                        "current_rent_cad",
                        "proposed_rent_cad",
                        "exact_new_rent_ceiling_cad",
                    }:
                        raise ValueError("Invalid synthetic amount display")
                    if any(
                        value is not None
                        and (
                            not isinstance(value, str)
                            or re.fullmatch(r"-?[0-9]+\.[0-9]{2,}", value) is None
                        )
                        for value in amounts.values()
                    ):
                        raise ValueError("Invalid synthetic amount display")
                else:
                    raise ValueError("Invalid synthetic followup")
            else:
                raise ValueError("Invalid synthetic followup")
        expected_blocks = [expected_added] if self._arm == "production" and expected_added else []
        if added != expected_blocks:
            raise ValueError("Extra context differs from actual executed rule evidence")
        # This outgoing native result proves execution, even if counting then fails.
        self.actual_tool_args = self._request.facts
        self.actual_tool_result = result
        return allowed

    def _write(self, operation: str, event: str, value: object) -> None:
        if self._provenance is None:
            raise ValueError("Cannot record unvalidated synthetic context")
        self._stream.write(
            json.dumps(
                {
                    "run_id": str(self._run_id),
                    "attempt_id": str(self._attempt_id),
                    "trace_id": str(self._trace_id),
                    "phase": self._phase,
                    "operation": operation,
                    "operation_index": self._index,
                    "event": event,
                    "context_provenance": self._provenance.model_dump(mode="json"),
                    "value": value,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        self._stream.flush()

    async def _call(self, operation: str, payload: dict[str, Any]) -> Message | MessageTokensCount:
        try:
            comparable = {
                key: value
                for key, value in payload.items()
                if key not in {"timeout", "max_tokens", "stream", "service_tier", "extra_body"}
            }
            if operation == "generation" and comparable != self._preflight:
                raise ValueError("Generation differs from its successful preflight")
            self._prepared(payload, operation)
            self._preflight = None
        except Exception:
            raise ProviderFailure("provider_error") from None
        self._index += 1
        self._write(operation, "request", payload)
        try:
            result = (
                await self._port.count_tokens(**payload)
                if operation == "count_tokens"
                else await self._port.create(**payload)
            )
        except BaseException as error:
            code = _error_code(error)
            self._write(operation, "failure", {"code": code})
            raise
        if operation == "count_tokens" and isinstance(result, MessageTokensCount):
            self._preflight = json.loads(json.dumps(comparable))
        try:
            raw = capture_provider_response(
                result,
                message_fields={
                    "id",
                    "type",
                    "role",
                    "model",
                    "content",
                    "stop_reason",
                    "stop_sequence",
                    "usage",
                },
            )
        except ProviderFailure:
            self._write(operation, "failure", {"code": "provider_error"})
            raise
        if isinstance(result, Message):
            self._returned.append(raw)
        self._write(operation, "response", raw)
        return result

    async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
        result = await self._call("count_tokens", kwargs)
        if not isinstance(result, MessageTokensCount):
            raise ProviderFailure("provider_error")
        return result

    async def create(self, **kwargs: Any) -> Message:
        result = await self._call("generation", kwargs)
        if not isinstance(result, Message):
            raise ProviderFailure("provider_error")
        return result


class _VerificationOnly:
    async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
        raise ValueError("Artifact verification cannot dispatch requests")

    async def create(self, **kwargs: Any) -> Message:
        raise ValueError("Artifact verification cannot dispatch requests")


def _independent_retrieval(case: RecordedCase, corpus: Corpus) -> tuple[SearchHit, ...]:
    if isinstance(case.request, ExtractRequest):
        raise ValueError("Extraction-only security has no analysis retrieval")
    with TemporaryDirectory(prefix="rent-navigator-replay-") as directory:
        index = Path(directory) / "corpus.sqlite"
        build_index(index, corpus)
        return search(
            index, query_for_request(case.request), expected_corpus_hash=corpus.corpus_hash
        )


def verify_synthetic_records(
    records: Sequence[RawProviderRecord],
    *,
    case: RecordedCase,
    corpus: Corpus,
    arm: Literal["production", "baseline"],
    retrieved_ids: tuple[str, ...] | None = None,
) -> None:
    """Recompute ranked seeds independently, then validate every retained raw event.

    ``retrieved_ids`` is only an additional assertion. Candidate records and rows never
    supply the retrieval authority. An unfinished final request remains auditable without
    claiming a response or downstream network dispatch.
    """
    if not records:
        return
    from io import StringIO

    first = records[0]
    verifier = SyntheticRecorder(
        _VerificationOnly(),
        case=case,
        allowlist=SyntheticAllowlist.from_fixtures((case,)),
        corpus=corpus,
        stream=StringIO(),
        run_id=first.run_id,
        attempt_id=first.attempt_id,
    )
    trace_id: UUID | None = None
    trace_phase: str | None = None
    seen_traces: set[UUID] = set()
    counted: dict[tuple[UUID, int], dict[str, Any]] = {}
    count_responses: set[tuple[UUID, int]] = set()
    pending: RawProviderRecord | None = None
    last_index = 0
    independent_hits: tuple[SearchHit, ...] | None = None
    try:
        for candidate in records:
            record = RawProviderRecord.model_validate_json(candidate.model_dump_json())
            if record.run_id != first.run_id or record.attempt_id != first.attempt_id:
                raise ValueError("Raw events mix attempts or runs")
            if record.trace_id != trace_id:
                if record.trace_id in seen_traces or pending is not None:
                    raise ValueError("Raw trace is repeated or interrupted")
                seen_traces.add(record.trace_id)
                trace_id, trace_phase = record.trace_id, record.phase
                last_index = 0
                if isinstance(case.request, ExtractRequest):
                    raise ValueError("Extraction-only security is verified by the offline suite")
                verifier.bind(trace_id, record.phase, case.request, arm=arm)
                if record.phase == "analysis" and arm == "production":
                    if independent_hits is None:
                        independent_hits = _independent_retrieval(case, corpus)
                    verifier.observe_retrieval(independent_hits)
                    if (
                        retrieved_ids is not None
                        and retrieved_ids != verifier.expected_retrieved_ids
                    ):
                        raise ValueError("Candidate seeds differ from independent ranked retrieval")
                elif record.phase == "analysis" and retrieved_ids not in (None, ()):
                    raise ValueError("Baseline cannot have retrieval seeds")
            if record.phase != trace_phase:
                raise ValueError("Raw trace changes phase")
            if record.context_provenance != verifier._bound_provenance():
                raise ValueError("Raw context differs from independent context provenance")
            if record.event == "request":
                if pending is not None or record.operation_index != last_index + 1:
                    raise ValueError("Raw request order is invalid")
                pending = record
                last_index = record.operation_index
                verifier._prepared(record.value, record.operation)
                if record.operation == "count_tokens":
                    counted[(record.trace_id, record.operation_index)] = {
                        key: value for key, value in record.value.items() if key != "timeout"
                    }
                else:
                    preflight = (record.trace_id, record.operation_index - 1)
                    generation_input = {
                        key: value
                        for key, value in record.value.items()
                        if key
                        not in {"timeout", "max_tokens", "stream", "service_tier", "extra_body"}
                    }
                    if (
                        preflight not in count_responses
                        or counted.get(preflight) != generation_input
                    ):
                        raise ValueError("Generation input differs from its completed preflight")
                continue
            if (
                pending is None
                or pending.operation_index != record.operation_index
                or pending.operation != record.operation
            ):
                raise ValueError("Raw terminal event has no matching request")
            pending = None
            if record.event == "response" and record.operation == "count_tokens":
                count_responses.add((record.trace_id, record.operation_index))
            elif record.event == "response" and record.operation == "generation":
                if set(record.value) - {
                    "id",
                    "type",
                    "role",
                    "model",
                    "content",
                    "stop_reason",
                    "stop_sequence",
                    "usage",
                }:
                    raise ValueError("Unexpected raw response fields")
                verifier._returned.append(record.value)
            elif record.event == "failure":
                from pydantic import TypeAdapter

                from rent_navigator.models import ErrorCode

                if set(record.value) != {"code"}:
                    raise ValueError("Unexpected raw error fields")
                TypeAdapter(ErrorCode).validate_python(record.value["code"])
    except Exception:
        raise ValueError("Raw provider content does not match the allowed scenario") from None
