from __future__ import annotations

from refactor.app.middleware.idempotency import IdempotencyMiddleware
from refactor.app.middleware.request_validation import InboundValidationError, RequestValidationMiddleware
from refactor.app.policy import RuntimePolicyEngine
from refactor.app.runtime.context import InboundSMSMessage, RuntimeServices
from refactor.app.runtime.orchestration_facade import OrchestrationFacade


def _inbound(phone: str = "+61412345678", body: str = "hello") -> InboundSMSMessage:
    return InboundSMSMessage(
        phone_number=phone,
        body=body,
        message_data={},
        request_payload={},
        request_id="req-1",
    )


def test_orchestration_facade_rejects_invalid_phone_number() -> None:
    runtime = RuntimeServices(
        state_manager=object(),
        db_service=object(),
        legacy_processor=lambda _phone, _body: ["ok"],
    )
    facade = OrchestrationFacade(
        runtime_services=runtime,
        middlewares=[RequestValidationMiddleware()],
    )

    try:
        facade.process_sms(_inbound(phone="+61412"))
        assert False, "Expected InboundValidationError"
    except InboundValidationError as exc:
        assert str(exc) == "Invalid phone number"


def test_orchestration_facade_skips_duplicates_before_legacy_processing() -> None:
    called = {"value": False}

    def _legacy(_phone: str, _body: str) -> list[str]:
        called["value"] = True
        return ["legacy"]

    runtime = RuntimeServices(
        state_manager=object(),
        db_service=object(),
        legacy_processor=_legacy,
    )
    dedup = IdempotencyMiddleware(
        key_builder=lambda _msg, _payload, _phone, _body: "id-123",
        key_claimer=lambda _db, _key: False,
    )
    facade = OrchestrationFacade(
        runtime_services=runtime,
        middlewares=[RequestValidationMiddleware(), dedup],
    )

    outcome = facade.process_sms(_inbound())
    assert outcome.duplicate is True
    assert outcome.messages == []
    assert called["value"] is False


def test_orchestration_facade_delegates_to_legacy_when_claimed() -> None:
    runtime = RuntimeServices(
        state_manager=object(),
        db_service=object(),
        legacy_processor=lambda _phone, _body: ["first", "", "second"],
    )
    dedup = IdempotencyMiddleware(
        key_builder=lambda _msg, _payload, _phone, _body: "id-123",
        key_claimer=lambda _db, _key: True,
    )
    facade = OrchestrationFacade(
        runtime_services=runtime,
        middlewares=[RequestValidationMiddleware(), dedup],
    )

    outcome = facade.process_sms(_inbound())
    assert outcome.duplicate is False
    assert outcome.messages == ["first", "second"]
    assert outcome.metadata["idempotency_claimed"] is True


def test_orchestration_facade_reuses_compiled_pipeline_across_requests() -> None:
    runtime = RuntimeServices(
        state_manager=object(),
        db_service=object(),
        legacy_processor=lambda _phone, _body: ["ok"],
    )
    facade = OrchestrationFacade(
        runtime_services=runtime,
        middlewares=[RequestValidationMiddleware()],
    )

    compiled_handler_id = id(facade._pipeline_handler)
    facade.process_sms(_inbound(body="first"))
    facade.process_sms(_inbound(body="second"))

    assert id(facade._pipeline_handler) == compiled_handler_id


def test_orchestration_facade_falls_back_when_policy_provider_errors() -> None:
    called = {"value": False}

    def _legacy(_phone: str, _body: str) -> list[str]:
        called["value"] = True
        return ["legacy"]

    class _ExplodingPolicy:
        name = "exploding_policy"

        def evaluate(self, _policy_input):
            raise RuntimeError("boom")

    runtime = RuntimeServices(
        state_manager=object(),
        db_service=object(),
        legacy_processor=_legacy,
        policy_engine=RuntimePolicyEngine(providers=[_ExplodingPolicy()]),
    )
    facade = OrchestrationFacade(
        runtime_services=runtime,
        middlewares=[RequestValidationMiddleware()],
    )

    outcome = facade.process_sms(_inbound())

    assert called["value"] is True
    assert outcome.messages == ["legacy"]
    assert outcome.metadata["policy_fallback_used"] is True
    assert outcome.metadata["policy_details"]["errored_policy_providers"] == ("exploding_policy",)
