"""Bounded asynchronous provider calls with conservative metadata accounting."""

import asyncio
import json
import logging
import math
import os
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
from hashlib import sha256
from threading import Lock
from typing import Annotated, Any, Literal, NoReturn, Protocol, Self

from anthropic import (
    APIConnectionError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
    AsyncAnthropic,
)
from anthropic.types import Message, MessageParam, MessageTokensCount, ToolChoiceParam, ToolParam
from anthropic.types import Usage as SDKUsage
from pydantic import (
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from rent_navigator.model_policy import (
    ACTOR_MODEL,
    JUDGE_MODEL,
    MODEL_POLICIES,
    RequestedModel,
    policy_manifest,
)
from rent_navigator.models import ErrorCode, StrictModel
from rent_navigator.trace import (
    CostSummary,
    ReturnedModel,
    TraceRecorder,
    Usage,
    cost_for_usage,
)

_SAFE_MESSAGES: dict[ErrorCode, str] = {
    "invalid_request": "The request could not be accepted.",
    "body_too_large": "The request is too large.",
    "rate_limited": "The request limit has been reached.",
    "busy": "The service is busy.",
    "budget_exhausted": "The request exceeds the available budget.",
    "stale_corpus": "The source snapshot requires review.",
    "provider_error": "The provider request could not be completed safely.",
    "invalid_generated_output": "The generated output could not be validated.",
    "tool_protocol_error": "The tool response could not be validated.",
    "deadline_exceeded": "The request deadline was exceeded.",
}


class FailureDiagnostic(StrictModel):
    """Closed diagnostic categories; no provider text or objects are retained."""

    model_config = ConfigDict(frozen=True, revalidate_instances="always")

    schema_version: Literal[1]
    category: Literal[
        "sdk_timeout",
        "sdk_connection",
        "http_error",
        "sdk_response_validation",
        "local_timeout",
        "cancelled",
        "local_guard",
        "unknown",
    ]
    http_status: Annotated[int, Field(strict=True)] | None

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("failure diagnostic version must be integer 1")
        return value

    @model_validator(mode="after")
    def category_status(self) -> Self:
        if self.category == "http_error":
            valid = self.http_status is not None and 400 <= self.http_status <= 599
        elif self.category == "sdk_response_validation":
            valid = self.http_status is None or 100 <= self.http_status <= 599
        else:
            valid = self.http_status is None
        if not valid:
            raise ValueError("failure category and HTTP status are inconsistent")
        return self


class FailureDiagnosticV2(StrictModel):
    """Versioned rejection labels without retaining a provider error body."""

    model_config = ConfigDict(frozen=True, revalidate_instances="always")

    schema_version: Literal[2]
    category: Literal[
        "sdk_timeout",
        "sdk_connection",
        "http_error",
        "sdk_response_validation",
        "local_timeout",
        "cancelled",
        "local_guard",
        "unknown",
    ]
    http_status: Annotated[int, Field(strict=True)] | None
    rejection_reason: Literal["schema_complexity", "unclassified"] | None

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int or value != 2:
            raise ValueError("failure diagnostic version must be integer 2")
        return value

    @model_validator(mode="after")
    def category_status_reason(self) -> Self:
        if self.category == "http_error":
            valid = self.http_status is not None and 400 <= self.http_status <= 599
        elif self.category == "sdk_response_validation":
            valid = self.http_status is None or 100 <= self.http_status <= 599
        else:
            valid = self.http_status is None
        if not valid:
            raise ValueError("failure category and HTTP status are inconsistent")
        rejected_request = self.category == "http_error" and self.http_status == 400
        if rejected_request != (self.rejection_reason is not None):
            raise ValueError("failure category, HTTP status and rejection reason are inconsistent")
        return self


def _diagnostic_version(value: object) -> int | None:
    if isinstance(value, FailureDiagnostic | FailureDiagnosticV2):
        version: object = value.schema_version
    elif isinstance(value, dict):
        version = value.get("schema_version")
    else:
        return None
    if type(version) is int and version == 1:
        return 1
    if type(version) is int and version == 2:
        return 2
    return None


class FailureEnvelope(StrictModel):
    """Bind a safe error code to the exact versioned diagnostic semantics."""

    model_config = ConfigDict(frozen=True, revalidate_instances="always")

    code: ErrorCode
    diagnostic: Annotated[
        Annotated[FailureDiagnostic, Tag(1)] | Annotated[FailureDiagnosticV2, Tag(2)],
        Discriminator(_diagnostic_version),
    ]

    @model_validator(mode="after")
    def code_category(self) -> Self:
        if self.diagnostic.category in {"sdk_timeout", "local_timeout", "cancelled"}:
            valid = self.code == "deadline_exceeded"
        elif self.diagnostic.category == "local_guard":
            valid = self.code != "deadline_exceeded"
        else:
            valid = self.code == "provider_error"
        if not valid:
            raise ValueError("failure code and diagnostic category are inconsistent")
        return self


class ProviderFailure(Exception):
    """Retain only a fixed safe code/message and the validated diagnostic."""

    def __init__(
        self,
        code: ErrorCode,
        *,
        diagnostic: FailureDiagnostic | FailureDiagnosticV2 | None = None,
    ) -> None:
        envelope = FailureEnvelope(
            code=code,
            diagnostic=diagnostic
            if diagnostic is not None
            else FailureDiagnosticV2(
                schema_version=2,
                category="local_timeout" if code == "deadline_exceeded" else "local_guard",
                http_status=None,
                rejection_reason=None,
            ),
        )
        self.code = envelope.code
        self.message = _SAFE_MESSAGES[envelope.code]
        self.diagnostic = envelope.diagnostic
        super().__init__(self.message)

    @classmethod
    def from_exception(cls, error: BaseException) -> "ProviderFailure":
        envelope = failure_envelope(error)
        return cls(envelope.code, diagnostic=envelope.diagnostic)


def _http_400_rejection_reason(
    error: APIStatusError,
) -> Literal["schema_complexity", "unclassified"]:
    try:
        body = error.body
        if type(body) is not dict:
            return "unclassified"
        if "error" in body:
            root_type = body.get("type")
            if type(root_type) is str and root_type == "invalid_request_error":
                return "unclassified"
            detail = body["error"]
            if type(detail) is not dict:
                return "unclassified"
        else:
            detail = body
        error_type = detail.get("type")
        message = detail.get("message")
        if (
            type(error_type) is str
            and type(message) is str
            and error_type == "invalid_request_error"
            and message == "Schema is too complex for compilation."
        ):
            return "schema_complexity"
    except BaseException:
        return "unclassified"
    return "unclassified"


def classify_failure(error: BaseException) -> FailureDiagnostic | FailureDiagnosticV2:
    """Classify typed errors with one bounded HTTP 400 body observation."""
    if isinstance(error, ProviderFailure):
        return FailureEnvelope(code=error.code, diagnostic=error.diagnostic).diagnostic
    category: Literal[
        "sdk_timeout",
        "sdk_connection",
        "http_error",
        "sdk_response_validation",
        "local_timeout",
        "cancelled",
        "unknown",
    ] = "unknown"
    status: int | None = None
    reason: Literal["schema_complexity", "unclassified"] | None = None
    if isinstance(error, APITimeoutError):
        category = "sdk_timeout"
    elif isinstance(error, APIConnectionError):
        category = "sdk_connection"
    elif isinstance(error, (APIResponseValidationError, APIStatusError)):
        try:
            value = error.status_code
        except BaseException:
            value = None
        if isinstance(error, APIResponseValidationError):
            category = "sdk_response_validation"
            status = value if type(value) is int and 100 <= value <= 599 else None
        elif type(value) is int and 400 <= value <= 599:
            category, status = "http_error", value
            if status == 400:
                reason = _http_400_rejection_reason(error)
    elif isinstance(error, asyncio.CancelledError):
        category = "cancelled"
    elif isinstance(error, TimeoutError):
        category = "local_timeout"
    return FailureDiagnosticV2(
        schema_version=2, category=category, http_status=status, rejection_reason=reason
    )


def failure_envelope(error: BaseException) -> FailureEnvelope:
    diagnostic = classify_failure(error)
    code: ErrorCode = "provider_error"
    if isinstance(error, ProviderFailure):
        code = error.code
    elif diagnostic.category in {"sdk_timeout", "local_timeout", "cancelled"}:
        code = "deadline_exceeded"
    return FailureEnvelope(code=code, diagnostic=diagnostic)


def _raise_failure(failure: ProviderFailure) -> NoReturn:
    try:
        raise failure from None
    finally:
        # A context-manager caller may still be handling a raw exception here.
        failure.__context__ = None
        failure.__cause__ = None


@dataclass(frozen=True)
class Deadline:
    expires_at: float
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)

    @classmethod
    def start(
        cls,
        *,
        clock: Callable[[], float] = time.monotonic,
        duration_seconds: float = 45.0,
    ) -> "Deadline":
        if (
            isinstance(duration_seconds, bool)
            or not isinstance(duration_seconds, int | float)
            or not 0 < duration_seconds <= 180
            or not math.isfinite(duration_seconds)
        ):
            raise ValueError("Deadline duration must be finite numeric seconds within (0, 180]")
        return cls(clock() + float(duration_seconds), clock)

    def remaining(self) -> float:
        remaining = self.expires_at - self.clock()
        if not math.isfinite(remaining) or remaining <= 0:
            raise ProviderFailure("deadline_exceeded")
        return remaining

    def check(self) -> None:
        self.remaining()

    @asynccontextmanager
    async def limit(self) -> AsyncIterator[None]:
        failure = None
        try:
            async with asyncio.timeout(self.remaining()):
                yield
        except TimeoutError as error:
            failure = ProviderFailure.from_exception(error)
        if failure is not None:
            _raise_failure(failure)


@dataclass(frozen=True)
class Reservation:
    number: int
    model: RequestedModel
    amount: Decimal


class SpendBudget(Protocol):
    @property
    def stopped(self) -> bool: ...

    def reserve(self, model: RequestedModel) -> Reservation: ...

    def reconcile(
        self, reservation: Reservation, cost: CostSummary, *, reforecast: bool = False
    ) -> None: ...


class SpendLedger:
    """Atomic in-memory batch reservations, not a durable account or daily limit."""

    def __init__(self, limit: Decimal) -> None:
        if not limit.is_finite() or limit < 0:
            raise ValueError("Batch limit must be finite and nonnegative")
        self._limit = limit
        self._lock = Lock()
        self._charges: dict[int, Decimal] = {}
        self._pending: dict[int, Reservation] = {}
        self._stopped = False

    @property
    def stopped(self) -> bool:
        with self._lock:
            return self._stopped

    @property
    def reforecast_required(self) -> bool:
        return self.stopped

    @property
    def committed_usd(self) -> Decimal:
        with self._lock:
            return sum(self._charges.values(), Decimal(0))

    def reserve(self, model: RequestedModel) -> Reservation:
        amount = cost_for_usage(model, None).reserved_cost_usd
        with self._lock:
            if self._stopped:
                raise ProviderFailure("provider_error")
            if sum(self._charges.values(), Decimal(0)) + amount > self._limit:
                raise ProviderFailure("budget_exhausted")
            ticket = Reservation(len(self._charges) + 1, model, amount)
            self._charges[ticket.number] = amount
            self._pending[ticket.number] = ticket
            return ticket

    def reconcile(
        self, reservation: Reservation, cost: CostSummary, *, reforecast: bool = False
    ) -> None:
        with self._lock:
            if self._pending.get(reservation.number) != reservation:
                raise ProviderFailure("provider_error")
            del self._pending[reservation.number]
            self._charges[reservation.number] = (
                cost.actual_cost_usd if cost.actual_cost_usd is not None else reservation.amount
            )
            if (
                not cost.usage_complete
                or reforecast
                or self._charges[reservation.number] > reservation.amount
            ):
                self._stopped = True


class MessagesPort(Protocol):
    async def count_tokens(self, **kwargs: Any) -> MessageTokensCount: ...

    async def create(self, **kwargs: Any) -> Message: ...


def create_client(*, api_key: str) -> AsyncAnthropic:
    """Create the real client explicitly; importing this module reads no secrets."""
    if not api_key or "ANTHROPIC_CUSTOM_HEADERS" in os.environ:
        raise ProviderFailure("provider_error")
    _disable_provider_logging()
    client = AsyncAnthropic(
        api_key=api_key,
        base_url="https://api.anthropic.com",
        max_retries=0,
        timeout=45.0,
    )
    _disable_provider_logging()
    return client


def _disable_provider_logging() -> None:
    prefixes = ("anthropic", "httpx2", "httpcore2")
    for name in (*prefixes, *tuple(logging.Logger.manager.loggerDict)):
        if any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes):
            logger = logging.getLogger(name)
            # Lazy transport children inherit this level even if created after the client.
            logger.setLevel(logging.CRITICAL + 1)
            logger.disabled = True


def configuration_hash(
    *,
    system: str,
    output_schema: dict[str, Any] | None = None,
    tools: Sequence[ToolParam] | None = None,
    tool_choice: ToolChoiceParam | None = None,
) -> str:
    """Hash behavior configuration without messages, secrets or correlation IDs."""
    config = {
        "models": {
            ACTOR_MODEL: {
                "temperature": 0,
                "thinking": "disabled",
                "max_tokens": MODEL_POLICIES[ACTOR_MODEL].max_output_tokens,
            },
            JUDGE_MODEL: {
                "thinking": "disabled",
                "max_tokens": MODEL_POLICIES[JUDGE_MODEL].max_output_tokens,
            },
        },
        "model_policy": policy_manifest(),
        "deadline_seconds": 45,
        "stream": False,
        "max_retries": 0,
        "service_tier": "standard_only",
        "system": system,
        "output_schema": output_schema,
        "tools": tools,
        "tool_choice": tool_choice,
    }
    return sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _zero_category(value: object) -> bool:
    if value is None or type(value) is int and value == 0:
        return True
    if isinstance(value, dict):
        return all(_zero_category(item) for item in value.values())
    return False


def _usage(message: Message) -> Usage | None:
    raw = getattr(message, "usage", None)
    if not isinstance(raw, SDKUsage):
        return None
    # Disable serializer warnings: malformed SDK data can contain sensitive values.
    values = raw.model_dump(warnings=False)
    if values.get("service_tier") not in (None, "standard"):
        return None
    known = {
        "input_tokens",
        "output_tokens",
        "output_tokens_details",
        "inference_geo",
        "service_tier",
    }
    if any(not _zero_category(value) for name, value in values.items() if name not in known):
        return None
    counts: list[int | None] = []
    for name in ("input_tokens", "output_tokens"):
        value = values.get(name)
        counts.append(value if type(value) is int and value >= 0 else None)
    return Usage(input_tokens=counts[0], output_tokens=counts[1])


def _error_code(error: BaseException) -> ErrorCode:
    return failure_envelope(error).code


def _reject_paid_features(value: object) -> None:
    if isinstance(value, dict):
        if "cache_control" in value:
            raise ProviderFailure("provider_error")
        for item in value.values():
            _reject_paid_features(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_paid_features(item)


class ProviderAdapter:
    def __init__(self, messages: MessagesPort, *, budget: SpendBudget) -> None:
        self._messages = messages
        self._budget = budget

    async def generate(
        self,
        *,
        model: RequestedModel,
        system: str,
        messages: Sequence[MessageParam],
        trace: TraceRecorder,
        deadline: Deadline,
        tools: Sequence[ToolParam] | None = None,
        tool_choice: ToolChoiceParam | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> Message:
        """Count once and generate at most once, preserving usage before validation."""
        try:
            if model not in MODEL_POLICIES or self._budget.stopped:
                raise ProviderFailure("provider_error")
            policy = MODEL_POLICIES[model]
            payload: dict[str, Any] = {
                "model": model,
                "system": system,
                "messages": deepcopy(list(messages)),
                "thinking": {"type": "disabled"},
            }
            if tools is not None:
                if any(tool.get("type", "custom") != "custom" for tool in tools):
                    raise ProviderFailure("provider_error")
                payload["tools"] = deepcopy(list(tools))
            if tool_choice is not None:
                payload["tool_choice"] = deepcopy(tool_choice)
            if output_schema is not None:
                payload["output_config"] = {
                    "format": {"type": "json_schema", "schema": deepcopy(output_schema)}
                }
            _reject_paid_features(payload)
            with trace.stage("preflight"):
                async with deadline.limit():
                    timeout = deadline.remaining()
                    with trace.provider_call(model, operation="count_tokens") as call:
                        try:
                            count = await self._messages.count_tokens(**payload, timeout=timeout)
                            deadline.check()
                            tokens = getattr(count, "input_tokens", None)
                            if type(tokens) is not int or tokens < 0:
                                raise ProviderFailure("provider_error")
                            if tokens > policy.preflight_limit:
                                raise ProviderFailure("budget_exhausted")
                        except BaseException as error:
                            call.response_code = _error_code(error)
                            raise

            generation = dict(
                payload,
                max_tokens=policy.max_output_tokens,
                stream=False,
                service_tier="standard_only",
            )
            if model == ACTOR_MODEL:
                # This locked SDK exposes sampling through its public extra_body seam.
                generation["extra_body"] = {"temperature": 0}
            with trace.stage("generation"):
                async with deadline.limit():
                    timeout = deadline.remaining()
                    ticket = self._budget.reserve(model)
                    usage: Usage | None = Usage(input_tokens=0, output_tokens=0)
                    reforecast = False
                    try:
                        # Deliver pending cancellation before recording or dispatching a call.
                        await asyncio.sleep(0)
                        # A budget implementation may consume time before granting admission.
                        timeout = deadline.remaining()
                        if self._budget.stopped:
                            raise ProviderFailure("provider_error")
                        with trace.provider_call(model) as call:
                            usage = None
                            try:
                                response = await self._messages.create(
                                    **generation, timeout=timeout
                                )
                                with trace.stage("validation"):
                                    try:
                                        call.returned_model_id = TypeAdapter(
                                            ReturnedModel
                                        ).validate_python(
                                            getattr(response, "model", None), strict=True
                                        )
                                    except ValidationError:
                                        usage = None
                                        call.usage = None
                                        reforecast = True
                                        raise ProviderFailure("provider_error") from None
                                    if call.returned_model_id != model:
                                        usage = None
                                        call.usage = None
                                        reforecast = True
                                        raise ProviderFailure("provider_error") from None
                                    usage = _usage(response)
                                    call.usage = usage
                                    cost = cost_for_usage(model, usage)
                                    reforecast = not cost.usage_complete or (
                                        usage is not None
                                        and usage.input_tokens is not None
                                        and usage.input_tokens > policy.reserved_input_tokens
                                    )
                                    if reforecast:
                                        raise ProviderFailure("provider_error")
                                    deadline.check()
                                    if (
                                        not isinstance(response, Message)
                                        or response.role != "assistant"
                                    ):
                                        raise ProviderFailure("provider_error")
                                    if response.stop_reason not in ("end_turn", "tool_use") or (
                                        usage is not None
                                        and usage.output_tokens is not None
                                        and usage.output_tokens > generation["max_tokens"]
                                    ):
                                        raise ProviderFailure("invalid_generated_output")
                                return response
                            except BaseException as error:
                                call.response_code = _error_code(error)
                                raise
                    finally:
                        self._budget.reconcile(
                            ticket, cost_for_usage(model, usage), reforecast=reforecast
                        )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            failure = ProviderFailure.from_exception(error)
        # Raising outside the handler avoids retaining the raw exception in __context__.
        _raise_failure(failure)
