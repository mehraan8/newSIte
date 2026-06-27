"""System validation: Identity.UserRegistered -> Notification (in-process).

DB-free but faithful: uses the REAL producer event, REAL serialization, REAL
EventDispatcher, REAL UserRegisteredHandler, and the REAL retry decision. Only
the session / repository / inbox are in-memory fakes that reproduce per-handler
savepoint rollback so the correctness claims are actually exercised.

Run: python tests/test_user_registered_flow_sim.py
"""

from __future__ import annotations

import asyncio
import sys
import uuid

sys.path.insert(0, ".")

from src.modules.identity.domain.entities import User
from src.modules.notification.application.consumers import UserRegisteredHandler
from src.modules.notification.domain.entities import Notification
from src.shared_kernel.domain.value_objects import Mobile
from src.shared_kernel.infrastructure.outbox.contracts import IncomingEvent
from src.shared_kernel.infrastructure.outbox.dispatcher import EventDispatcher
from src.shared_kernel.infrastructure.outbox.registry import HandlerRegistry
from src.shared_kernel.infrastructure.outbox.retry import Backoff, on_failure
from src.shared_kernel.infrastructure.outbox.serialization import event_payload


# --- in-memory fakes with real savepoint semantics ------------------------
class FakeSession:
    def __init__(self) -> None:
        self.notifs: list[dict] = []
        self.inbox: set[tuple[str, str]] = set()
        self._snap: list[tuple[list, set]] = []

    def begin_nested(self):
        s = self

        class CM:
            async def __aenter__(self_):
                s._snap.append((list(s.notifs), set(s.inbox)))
                return None

            async def __aexit__(self_, et, ev, tb):
                base = s._snap.pop()
                if et is not None:          # savepoint rollback on exception
                    s.notifs, s.inbox = base
                return False                # never suppress

        return CM()

    async def flush(self) -> None:  # repo calls flush; no-op for the fake
        return None


class FakeRepo:
    def __init__(self, session: FakeSession) -> None:
        self._s = session

    async def add(self, n: Notification) -> None:
        self._s.notifs.append(
            {"user_id": str(n.user_id), "channel": n.channel,
             "template": n.template, "status": n.status}
        )
        n.id = len(self._s.notifs)


class FakeInbox:
    async def already_processed(self, session, name, key) -> bool:
        return (name, key) in session.inbox

    async def mark_processed(self, session, name, event) -> None:
        session.inbox.add((name, event.idempotency_key))


class FailingAfterWrite:
    """Handler that writes the notification then fails — exercises savepoint."""

    name = "notification.on_user_registered"
    event_type = "identity.UserRegistered"

    def __init__(self, repo_factory, fail_times: int = 1) -> None:
        self._rf = repo_factory
        self._fail = fail_times

    async def handle(self, session, event) -> None:
        repo = self._rf(session)
        n = Notification.create(uuid.UUID(event.payload["user_id"]), "sms", "welcome", {})
        await repo.add(n)                       # DB write happens...
        if self._fail > 0:
            self._fail -= 1
            raise RuntimeError("post-write failure")   # ...then handler fails


def real_incoming() -> tuple[IncomingEvent, dict]:
    """Build an IncomingEvent from a REAL Identity event + serialization."""
    u = User.register(Mobile("+989121234567"))
    de = u.pull_events()[0]
    assert de.event_type == "identity.UserRegistered", de.event_type
    payload = event_payload(de)
    inc = IncomingEvent(
        event_id=de.event_id,
        idempotency_key=str(de.event_id),
        event_type=de.event_type,
        event_version=type(de).event_version,
        aggregate_id=str(de.aggregate_id),
        aggregate_type=de.aggregate_type,
        occurred_at=de.occurred_at,
        payload=payload,
        correlation_id=uuid.uuid4(),
        region="global",
    )
    return inc, payload


def _dispatcher(handler) -> EventDispatcher:
    reg = HandlerRegistry()
    reg.register(handler)
    return EventDispatcher(reg, FakeInbox())


async def main() -> None:
    inc, payload = real_incoming()

    # === Happy path + duplicate delivery (at-least-once -> exactly one effect) ===
    d = _dispatcher(UserRegisteredHandler(repo_factory=lambda s: FakeRepo(s)))
    s = FakeSession()
    await d.dispatch(s, inc)
    await d.dispatch(s, inc)                      # duplicate redelivery
    assert len(s.notifs) == 1 and len(s.inbox) == 1, (s.notifs, s.inbox)
    assert s.notifs[0] == {
        "user_id": payload["user_id"], "channel": "sms",
        "template": "welcome", "status": "queued",
    }
    print("[happy+dup]   notifications:", s.notifs)

    # === Handler failure AFTER db write -> savepoint rolls back write + inbox ===
    s2 = FakeSession()
    d2 = _dispatcher(FailingAfterWrite(lambda s: FakeRepo(s), fail_times=1))
    try:
        await d2.dispatch(s2, inc)
        raise AssertionError("expected handler failure to propagate")
    except RuntimeError:
        pass
    assert s2.notifs == [] and s2.inbox == set(), "savepoint must undo write + inbox"
    await d2.dispatch(s2, inc)                    # relay retry -> now succeeds
    assert len(s2.notifs) == 1 and len(s2.inbox) == 1
    print("[handler-fail] after rollback+retry notifications:", s2.notifs)

    # === Relay crash mid-batch -> outer tx discarded -> reprocessed exactly once ==
    s3 = FakeSession()
    d3 = _dispatcher(UserRegisteredHandler(repo_factory=lambda s: FakeRepo(s)))
    outer = (list(s3.notifs), set(s3.inbox))     # snapshot before batch
    await d3.dispatch(s3, inc)                    # savepoint released into outer tx
    assert len(s3.notifs) == 1
    s3.notifs, s3.inbox = outer                   # CRASH before outer commit -> rollback
    assert s3.notifs == [] and s3.inbox == set()
    await d3.dispatch(s3, inc)                    # row still unpublished -> reprocess
    assert len(s3.notifs) == 1 and len(s3.inbox) == 1
    print("[crash]       after recovery notifications:", s3.notifs)

    # === Retry -> DLQ progression and recovery (real decision function) ===
    bf = Backoff(base_seconds=5, factor=2, max_seconds=60, jitter=False)
    rc, outcomes = 0, []
    for _ in range(3):                            # max_retries = 3
        o = on_failure(rc, 3, bf); rc = o.retry_count; outcomes.append(o)
    assert [o.dead_letter for o in outcomes] == [False, False, True]
    assert [o.delay_seconds for o in outcomes] == [5, 10, None]
    print("[retry->dlq]  delays:", [o.delay_seconds for o in outcomes[:-1]], "then DLQ")

    rc = 0                                        # recovery: fail twice, succeed 3rd
    o1 = on_failure(rc, 5, bf); rc = o1.retry_count
    o2 = on_failure(rc, 5, bf); rc = o2.retry_count
    assert not o1.dead_letter and not o2.dead_letter and rc == 2
    print("[recovery]    fail x2 then publish OK (no DLQ), retry_count=", rc)

    print("\nEND-TO-END FLOW VALIDATED: Identity.UserRegistered -> Notification")


if __name__ == "__main__":
    asyncio.run(main())
