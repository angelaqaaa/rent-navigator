"""Full-content recording restricted to declared synthetic scenarios."""

import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TextIO
from uuid import UUID

from anthropic import transform_schema
from anthropic.types import Message, MessageTokensCount
from pydantic import Field

from rent_navigator.agent import _FINAL_CHOICE, _SELECTION_CHOICE, _system
from rent_navigator.corpus import Corpus
from rent_navigator.eval.models import GoldCase
from rent_navigator.extract import EXTRACTION_SYSTEM
from rent_navigator.guards import redact_text
from rent_navigator.model_policy import ACTOR_MODEL, MODEL_POLICIES
from rent_navigator.models import (
    AskRequest,
    CanonicalUUID,
    Extraction,
    GeneratedResult,
    NoticeFacts,
    RentFacts,
    StrictModel,
    ToolResult,
    provider_tool_definitions,
)
from rent_navigator.provider import MessagesPort, ProviderFailure, _error_code


class RawProviderRecord(StrictModel):
    run_id: CanonicalUUID
    attempt_id: CanonicalUUID
    trace_id: CanonicalUUID
    phase: Literal["extraction", "analysis"]
    operation: Literal["count_tokens", "generation"]
    operation_index: Annotated[int, Field(strict=True, gt=0)]
    event: Literal["request", "response", "failure"]
    value: dict[str, Any]


@dataclass(frozen=True)
class SyntheticAllowlist:
    """An explicit fixture declaration; never inferred from arbitrary request text."""

    cases: tuple[str, ...]

    @classmethod
    def from_fixtures(cls, cases: Sequence[GoldCase]) -> "SyntheticAllowlist":
        return cls(tuple(case.model_dump_json() for case in cases))

    def require(self, case: GoldCase) -> None:
        if case.model_dump_json() not in self.cases:
            raise ValueError("Scenario is outside the declared synthetic allowlist")


class SyntheticRecorder:
    """Wrap an injected port, validating prepared inputs before retaining content."""

    def __init__(
        self,
        port: MessagesPort,
        *,
        case: GoldCase,
        allowlist: SyntheticAllowlist,
        corpus: Corpus,
        stream: TextIO,
        run_id: UUID,
        attempt_id: UUID,
    ) -> None:
        allowlist.require(case)
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

    def _evidence(self, value: object) -> None:
        if not isinstance(value, list) or len(value) > len(self._corpus.chunks):
            raise ValueError("Invalid synthetic evidence")
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, dict) or set(item) != {"id", "heading", "text"}:
                raise ValueError("Invalid synthetic evidence")
            chunk = self._corpus.chunk(item["id"])
            if item["text"] != chunk.text or item["heading"] != chunk.heading or chunk.id in seen:
                raise ValueError("Invalid synthetic evidence")
            seen.add(chunk.id)

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
            schema = transform_schema(GeneratedResult.model_json_schema())
        else:
            selection = len(messages) == 1
            expected_config.update(
                system=_system(self._request.mode, self._arm, selection=selection),
                tools=provider_tool_definitions(),
                tool_choice=_SELECTION_CHOICE if selection else _FINAL_CHOICE,
            )
            if not selection:
                schema = transform_schema(GeneratedResult.model_json_schema())
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
        first = messages[0]
        if not isinstance(first, dict) or set(first) != {"role", "content"}:
            raise ValueError("Invalid synthetic request")
        if first["role"] != "user" or not isinstance(first["content"], str):
            raise ValueError("Invalid synthetic request")
        if self._phase == "extraction":
            if self._case.letter is None or len(messages) != 1:
                raise ValueError("Invalid synthetic extraction")
            if first["content"] != redact_text(self._case.letter):
                raise ValueError("Invalid synthetic extraction")
            return
        initial = json.loads(first["content"])
        if not isinstance(initial, dict):
            raise ValueError("Invalid synthetic analysis")
        expected: dict[str, Any] = {"mode": self._request.mode}
        if self._request.mode == "question":
            expected["question"] = redact_text(self._request.question)
        else:
            expected.update(confirmed=True, facts=self._request.facts.model_dump(mode="json"))
        if "evidence" in initial:
            self._evidence(initial.pop("evidence"))
        if initial != expected:
            raise ValueError("Invalid synthetic analysis")
        if len(messages) == 1:
            return
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
        calls = [item for item in history["content"] if item.get("type") == "tool_use"]
        if len(calls) != 1 or calls[0].get("input") != expected["facts"]:
            raise ValueError("Unconfirmed synthetic tool arguments")
        results = [item for item in followup["content"] if item.get("type") == "tool_result"]
        if len(results) != 1 or results[0].get("tool_use_id") != calls[0].get("id"):
            raise ValueError("Missing synthetic tool execution evidence")
        result = ToolResult.model_validate_json(results[0]["content"])
        if calls[0].get("name") != result.tool:
            raise ValueError("Mismatched synthetic tool execution")
        for item in followup["content"]:
            if item.get("type") == "tool_result":
                if set(item) != {"type", "tool_use_id", "content"}:
                    raise ValueError("Invalid synthetic tool result")
            elif item.get("type") == "text" and set(item) == {"type", "text"}:
                extra = json.loads(item["text"])
                if set(extra) == {"evidence"}:
                    self._evidence(extra["evidence"])
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
        # This outgoing native result proves execution, even if counting then fails.
        self.actual_tool_args = self._request.facts
        self.actual_tool_result = result

    def _write(self, operation: str, event: str, value: object) -> None:
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
            self._prepared(payload, operation)
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
        if isinstance(result, Message):
            raw = result.model_dump(
                mode="json",
                warnings=False,
                exclude_none=True,
                include={
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
            self._returned.append(raw)
        else:
            raw = result.model_dump(mode="json", warnings=False, include={"input_tokens"})
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


def verify_synthetic_records(
    records: Sequence[RawProviderRecord],
    *,
    case: GoldCase,
    corpus: Corpus,
    arm: Literal["production", "baseline"],
) -> None:
    """Revalidate prepared data against the fixture; never invoke serving operations."""
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
    try:
        for record in records:
            if record.trace_id != trace_id:
                trace_id = record.trace_id
                verifier.bind(trace_id, record.phase, case.request, arm=arm)
            if record.event == "request":
                verifier._prepared(record.value, record.operation)
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
