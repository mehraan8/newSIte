"""Proof that raw PII cannot leak into events and context cannot be forgotten.

DB-free: exercises the real guard, the real serialization path, the real
UserRegistered event, and the real context-enforcement function.

Run: python tests/test_event_pii_guard.py
"""

from __future__ import annotations

import sys
import uuid
from dataclasses import dataclass

sys.path.insert(0, ".")

from src.modules.identity.domain.entities import User, UserRegistered
from src.shared_kernel.application.event_context import (
    EventContext,
    EventContextError,
    require_event_context,
    reset_event_context,
    set_event_context,
    system_event_context,
)
from src.shared_kernel.application.pii import (
    PiiLeakageError,
    assert_clean_payload,
    assert_clean_pii_refs,
)
from src.shared_kernel.domain.base import DomainEvent
from src.shared_kernel.domain.value_objects import Mobile
from src.shared_kernel.infrastructure.outbox.serialization import event_payload


def expect(exc, fn, *a, **k):
    try:
        fn(*a, **k)
    except exc:
        return True
    raise AssertionError(f"expected {exc.__name__} from {getattr(fn,'__name__',fn)}")


# A deliberately leaking event (someone adds raw PII fields).
@dataclass(frozen=True)
class LeakyEvent(DomainEvent):
    user_id: uuid.UUID | None = None
    mobile: str = ""
    email: str = ""


def run() -> None:
    # === 1. forbidden KEYS rejected (top-level + nested) ===
    for bad in ({"mobile": "x"}, {"email": "x"}, {"full_name": "x"},
                {"profile": {"first_name": "Ali"}}, {"items": [{"national_id": "1"}]}):
        assert expect(PiiLeakageError, assert_clean_payload, bad)
    print("[keys]   forbidden keys rejected (incl. nested)")

    # === 2. value HEURISTICS — PII under an innocent key ===
    assert expect(PiiLeakageError, assert_clean_payload, {"note": "ali@example.com"})
    assert expect(PiiLeakageError, assert_clean_payload, {"ref": "+989121234567"})
    assert expect(PiiLeakageError, assert_clean_payload, {"ref": "09121234567"})
    print("[values] email/IR-phone values rejected even under safe keys")

    # === 3. clean payloads pass (no false positives on domain data) ===
    for ok in ({"user_id": str(uuid.uuid4())},
               {"old_amount": 100, "new_amount": 120, "currency": "IRR"},
               {"template": "welcome", "name_fa": "کرم پودر"}):  # product name OK
        assert assert_clean_payload(ok) is ok
    print("[clean]  legitimate domain payloads pass")

    # === 4. serialization enforces the guard for real events ===
    leaky = LeakyEvent(mobile="+989121234567", email="a@b.com")
    assert expect(PiiLeakageError, event_payload, leaky)
    # The real UserRegistered now carries NO mobile — payload is clean.
    u = User.register(Mobile("+989121234567"))
    ev = u.pull_events()[0]
    assert isinstance(ev, UserRegistered)
    payload = event_payload(ev)
    assert payload == {"user_id": str(ev.user_id)}, payload
    assert "mobile" not in payload and "email" not in payload
    # ...the number survives only as an opaque token in pii_refs.
    assert ev.pii_refs == (f"vault://identity/user/{ev.aggregate_id}/mobile",), ev.pii_refs
    print("[event]  UserRegistered payload clean; mobile -> pii_ref token:", ev.pii_refs[0])

    # === 5. pii_refs must be opaque tokens, never raw values ===
    assert_clean_pii_refs(("vault://identity/user/x/mobile",))     # ok
    assert expect(PiiLeakageError, assert_clean_pii_refs, ("a@b.com",))
    assert expect(PiiLeakageError, assert_clean_pii_refs, ("+989121234567",))
    assert expect(PiiLeakageError, assert_clean_pii_refs, ("just-a-string",))
    print("[refs]   pii_refs enforced to be reference tokens")

    # === 6. context cannot be forgotten ===
    assert expect(EventContextError, require_event_context, EventContext())  # empty default
    tok = set_event_context(EventContext(correlation_id=uuid.uuid4()))
    try:
        assert require_event_context().correlation_id is not None
    finally:
        reset_event_context(tok)
    with system_event_context(region="eu") as ctx:
        assert require_event_context().correlation_id == ctx.correlation_id
    print("[context] missing context raises; request/system context accepted")

    print("\nPII CANNOT LEAK + CONTEXT CANNOT BE FORGOTTEN — VALIDATED")


if __name__ == "__main__":
    run()
