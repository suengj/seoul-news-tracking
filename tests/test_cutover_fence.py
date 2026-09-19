from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.cutover_fence import (
    FENCE_TTL_SECONDS,
    PHASE_ACTIVE,
    CutoverFenceError,
    CutoverFenceStore,
)
from app.process_lock import SingleInstanceLock

TOKEN = "fence-test-token"
BASE_TIME = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def fence_pair(tmp_path):
    clock = [BASE_TIME]
    root = tmp_path / "shared-cutover-authority"
    mac = CutoverFenceStore(root, token=TOKEN, host_id="mac", now=lambda: clock[0])
    linux = CutoverFenceStore(root, token=TOKEN, host_id="linux", now=lambda: clock[0])
    return mac, linux, clock


def assert_refused(call, code: str):
    with pytest.raises(CutoverFenceError) as caught:
        call()
    assert caught.value.code == code
    assert f"[{code}]" in str(caught.value)


def test_positive_cutover_requires_off_fact_and_independent_readback(fence_pair):
    mac, linux, _clock = fence_pair
    request_id = mac.request_cutover(
        outgoing_host="mac", incoming_host="linux", request_id="mac-to-linux-1"
    )

    mac.confirm_off(request_id=request_id, confirmed_local_off=True)
    claimed = linux.claim()

    assert claimed["phase"] == PHASE_ACTIVE
    assert claimed["active_host"] == "linux"
    assert claimed["outgoing_off"]["host_id"] == "mac"
    assert claimed["outgoing_off"]["observed_at"]
    # The artifact acknowledged at entry must also be accepted by the
    # same-host restart validator.
    assert linux.claim() == claimed


def test_same_host_restart_refuses_active_timestamp_after_expiry(fence_pair):
    mac, linux, clock = fence_pair
    request_id = mac.request_cutover(
        outgoing_host="mac", incoming_host="linux", request_id="active-after-expiry"
    )
    mac.confirm_off(request_id=request_id, confirmed_local_off=True)
    linux.claim()

    authority_path = linux.authority_path
    authority = json.loads(authority_path.read_text(encoding="utf-8"))
    late_active = BASE_TIME + timedelta(seconds=FENCE_TTL_SECONDS + 40)
    authority["active_since"] = late_active.isoformat()
    authority_path.write_text(json.dumps(authority), encoding="utf-8")
    incoming_state_path = linux.host_state_path("linux")
    incoming_state = json.loads(incoming_state_path.read_text(encoding="utf-8"))
    incoming_state["observed_at"] = late_active.isoformat()
    incoming_state_path.write_text(json.dumps(incoming_state), encoding="utf-8")
    clock[0] = late_active + timedelta(seconds=1)

    assert_refused(linux.claim, "ACTIVE_ARTIFACT_MALFORMED")


@pytest.mark.parametrize("schema_value", [True, 1.0])
@pytest.mark.parametrize(
    "target, expected_code",
    [
        ("authority", "AUTHORITY_MALFORMED"),
        ("outgoing", "OFF_READBACK_MALFORMED"),
        ("incoming", "OFF_READBACK_MALFORMED"),
    ],
)
def test_schema_version_must_be_an_integer_on_authority_and_host_states(
    fence_pair, schema_value, target, expected_code
):
    mac, linux, _clock = fence_pair
    request_id = mac.request_cutover(
        outgoing_host="mac", incoming_host="linux", request_id="typed-schema"
    )
    mac.confirm_off(request_id=request_id, confirmed_local_off=True)
    linux.claim()

    if target == "authority":
        path = linux.authority_path
    elif target == "outgoing":
        path = linux.host_state_path("mac")
    else:
        path = linux.host_state_path("linux")
    value = json.loads(path.read_text(encoding="utf-8"))
    value["schema_version"] = schema_value
    path.write_text(json.dumps(value), encoding="utf-8")

    assert_refused(linux.claim, expected_code)


@pytest.mark.parametrize(
    "mutation, expected_code",
    [
        ("missing_fact", "AUTHORITY_OFF_FACT_MISSING"),
        ("wrong_fact", "AUTHORITY_OFF_FACT_MALFORMED"),
        ("timestamp_disagreement", "OFF_READBACK_TIMESTAMP_MISMATCH"),
        ("inverted_lifetime", "AUTHORITY_MALFORMED"),
        ("off_before_issue", "AUTHORITY_OFF_FACT_MALFORMED"),
        ("claim_before_issue", "AUTHORITY_MALFORMED"),
    ],
)
def test_first_claim_rejects_inconsistent_off_and_time_evidence(
    fence_pair, mutation, expected_code
):
    mac, linux, clock = fence_pair
    request_id = mac.request_cutover(
        outgoing_host="mac", incoming_host="linux", request_id=f"bad-entry-{mutation}"
    )
    mac.confirm_off(request_id=request_id, confirmed_local_off=True)
    authority_path = linux.authority_path
    authority = json.loads(authority_path.read_text(encoding="utf-8"))
    state_path = linux.host_state_path("mac")
    state = json.loads(state_path.read_text(encoding="utf-8"))

    if mutation == "missing_fact":
        authority["outgoing_off"].pop("fact")
    elif mutation == "wrong_fact":
        authority["outgoing_off"]["fact"] = "consumer stopped (unverified)"
    elif mutation == "timestamp_disagreement":
        state["observed_at"] = (BASE_TIME + timedelta(seconds=1)).isoformat()
        clock[0] = BASE_TIME + timedelta(seconds=2)
    elif mutation == "inverted_lifetime":
        authority["issued_at"] = (BASE_TIME + timedelta(seconds=20)).isoformat()
        authority["expires_at"] = (BASE_TIME + timedelta(seconds=10)).isoformat()
    elif mutation == "off_before_issue":
        earlier = (BASE_TIME - timedelta(seconds=1)).isoformat()
        authority["outgoing_off"]["observed_at"] = earlier
        state["observed_at"] = earlier
        clock[0] = BASE_TIME + timedelta(seconds=1)
    elif mutation == "claim_before_issue":
        clock[0] = BASE_TIME - timedelta(seconds=1)
    else:  # pragma: no cover - the parameter list is exhaustive
        raise AssertionError(mutation)

    authority_path.write_text(json.dumps(authority), encoding="utf-8")
    state_path.write_text(json.dumps(state), encoding="utf-8")

    assert_refused(linux.claim, expected_code)


def test_chronology_rejected_on_restart_is_also_rejected_at_entry(fence_pair, tmp_path):
    mac, linux, clock = fence_pair
    request_id = mac.request_cutover(
        outgoing_host="mac", incoming_host="linux", request_id="restart-chronology"
    )
    mac.confirm_off(request_id=request_id, confirmed_local_off=True)
    linux.claim()

    authority_path = linux.authority_path
    authority = json.loads(authority_path.read_text(encoding="utf-8"))
    late_active = BASE_TIME + timedelta(seconds=FENCE_TTL_SECONDS + 5)
    authority["active_since"] = late_active.isoformat()
    authority_path.write_text(json.dumps(authority), encoding="utf-8")
    incoming_state_path = linux.host_state_path("linux")
    incoming_state = json.loads(incoming_state_path.read_text(encoding="utf-8"))
    incoming_state["observed_at"] = late_active.isoformat()
    incoming_state_path.write_text(json.dumps(incoming_state), encoding="utf-8")
    clock[0] = late_active
    assert_refused(linux.claim, "ACTIVE_ARTIFACT_MALFORMED")

    entry_root = tmp_path / "entry-chronology"
    entry_clock = [BASE_TIME]
    entry_mac = CutoverFenceStore(
        entry_root, token=TOKEN, host_id="mac", now=lambda: entry_clock[0]
    )
    entry_linux = CutoverFenceStore(
        entry_root, token=TOKEN, host_id="linux", now=lambda: entry_clock[0]
    )
    entry_request_id = entry_mac.request_cutover(
        outgoing_host="mac", incoming_host="linux", request_id="entry-chronology"
    )
    entry_mac.confirm_off(request_id=entry_request_id, confirmed_local_off=True)
    entry_authority_path = entry_linux.authority_path
    entry_authority = json.loads(entry_authority_path.read_text(encoding="utf-8"))
    entry_state_path = entry_linux.host_state_path("mac")
    entry_state = json.loads(entry_state_path.read_text(encoding="utf-8"))
    earlier = (BASE_TIME - timedelta(seconds=1)).isoformat()
    entry_authority["outgoing_off"]["observed_at"] = earlier
    entry_state["observed_at"] = earlier
    entry_authority_path.write_text(json.dumps(entry_authority), encoding="utf-8")
    entry_state_path.write_text(json.dumps(entry_state), encoding="utf-8")
    entry_clock[0] = BASE_TIME + timedelta(seconds=1)

    assert_refused(entry_linux.claim, "AUTHORITY_OFF_FACT_MALFORMED")


def test_same_host_restart_refuses_malformed_active_artifact(fence_pair):
    mac, linux, _clock = fence_pair
    request_id = mac.request_cutover(
        outgoing_host="mac", incoming_host="linux", request_id="active-artifact"
    )
    mac.confirm_off(request_id=request_id, confirmed_local_off=True)
    linux.claim()

    authority_path = linux.authority_path
    authority = json.loads(authority_path.read_text(encoding="utf-8"))
    authority.pop("active_since")
    authority_path.write_text(json.dumps(authority), encoding="utf-8")

    assert_refused(linux.claim, "ACTIVE_ARTIFACT_MALFORMED")


def test_same_host_restart_refuses_active_outgoing_host_state(fence_pair):
    mac, linux, _clock = fence_pair
    request_id = mac.request_cutover(
        outgoing_host="mac", incoming_host="linux", request_id="active-state-mismatch"
    )
    mac.confirm_off(request_id=request_id, confirmed_local_off=True)
    linux.claim()

    state_path = mac.host_state_path("mac")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["state"] = "ACTIVE"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    assert_refused(linux.claim, "OUTGOING_STILL_ACTIVE")


def test_ancient_incomplete_same_host_active_is_refused(fence_pair):
    _mac, linux, _clock = fence_pair
    linux.authority_path.parent.mkdir(parents=True, exist_ok=True)
    linux.authority_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "telegram-getupdates-cutover",
                "request_id": "ancient-active",
                "token_fingerprint": linux.token_digest,
                "outgoing_host": "mac",
                "incoming_host": "linux",
                "phase": "ACTIVE",
                "issued_at": "2000-01-01T00:00:00+00:00",
                "active_host": "linux",
            }
        ),
        encoding="utf-8",
    )

    assert_refused(linux.claim, "ACTIVE_ARTIFACT_MALFORMED")


def test_authority_off_without_independent_readback_is_refused(fence_pair):
    mac, linux, _clock = fence_pair
    request_id = mac.request_cutover(
        outgoing_host="mac", incoming_host="linux", request_id="missing-readback"
    )
    mac.confirm_off(request_id=request_id, confirmed_local_off=True)

    # The authority record alone is not enough.  Removing the separately
    # readable host fact makes a fresh claim fail closed.
    linux.host_state_path("mac").unlink()
    assert_refused(linux.claim, "OFF_READBACK_ABSENT")


def test_wrong_host_but_otherwise_valid_off_readback_is_refused(fence_pair):
    mac, linux, _clock = fence_pair
    request_id = mac.request_cutover(
        outgoing_host="mac", incoming_host="linux", request_id="wrong-host-readback"
    )
    mac.confirm_off(request_id=request_id, confirmed_local_off=True)
    state_path = mac.host_state_path("mac")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["host_id"] = "linux"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    assert_refused(linux.claim, "OFF_READBACK_WRONG_HOST")


def test_stale_host_readback_is_refused_even_with_unexpired_authority(fence_pair):
    mac, linux, clock = fence_pair
    request_id = mac.request_cutover(
        outgoing_host="mac", incoming_host="linux", request_id="stale-readback"
    )
    mac.confirm_off(request_id=request_id, confirmed_local_off=True)
    state_path = mac.host_state_path("mac")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["observed_at"] = (BASE_TIME - timedelta(seconds=FENCE_TTL_SECONDS + 1)).isoformat()
    state_path.write_text(json.dumps(state), encoding="utf-8")

    # Keep the authority within its TTL while making only the independent
    # host-state read-back stale.
    assert clock[0] < BASE_TIME + timedelta(seconds=FENCE_TTL_SECONDS)
    assert_refused(linux.claim, "OFF_READBACK_STALE")


def test_mac_active_linux_start_is_refused_for_active_outgoing_reason(fence_pair):
    mac, linux, _clock = fence_pair
    request_id = mac.request_cutover(
        outgoing_host="mac", incoming_host="linux", request_id="mac-to-linux-active"
    )
    mac.publish_active_state(request_id=request_id)

    assert_refused(linux.claim, "OUTGOING_STILL_ACTIVE")


def test_absent_fence_evidence_is_refused(fence_pair):
    _mac, linux, _clock = fence_pair

    assert_refused(linux.claim, "AUTHORITY_ABSENT")


def test_stale_fence_evidence_is_refused(fence_pair):
    mac, linux, clock = fence_pair
    request_id = mac.request_cutover(
        outgoing_host="mac", incoming_host="linux", request_id="mac-to-linux-stale"
    )
    mac.confirm_off(request_id=request_id, confirmed_local_off=True)
    clock[0] += timedelta(seconds=FENCE_TTL_SECONDS + 1)

    assert_refused(linux.claim, "AUTHORITY_EXPIRED")


def test_interrupted_cutover_does_not_infer_authority_from_peer_silence(fence_pair):
    mac, linux, _clock = fence_pair
    request_id = mac.request_cutover(
        outgoing_host="mac", incoming_host="linux", request_id="mac-to-linux-interrupted"
    )

    # The request exists, but the outgoing OFF/read-back step never happened.
    assert_refused(linux.claim, "CUTOVER_INCOMPLETE")
    # The outgoing host is not the incoming host named by the request either;
    # restarting it cannot turn an interrupted request into authority.
    assert_refused(mac.claim, "WRONG_INCOMING_HOST")
    assert request_id == "mac-to-linux-interrupted"


def test_linux_active_mac_rollback_requires_linux_off_readback(fence_pair):
    mac, linux, _clock = fence_pair
    first_request = mac.request_cutover(
        outgoing_host="mac", incoming_host="linux", request_id="mac-to-linux-rollback"
    )
    mac.confirm_off(request_id=first_request, confirmed_local_off=True)
    linux.claim()

    rollback_request = linux.request_cutover(
        outgoing_host="linux", incoming_host="mac", request_id="linux-to-mac-rollback"
    )
    assert_refused(mac.claim, "OUTGOING_STILL_ACTIVE")

    linux.confirm_off(request_id=rollback_request, confirmed_local_off=True)
    claimed = mac.claim()
    assert claimed["phase"] == PHASE_ACTIVE
    assert claimed["active_host"] == "mac"


def test_duplicate_request_id_is_refused_and_cannot_grant_second_consumer(fence_pair):
    mac, linux, _clock = fence_pair
    request_id = mac.request_cutover(
        outgoing_host="mac", incoming_host="linux", request_id="duplicate-request"
    )
    assert_refused(
        lambda: mac.request_cutover(
            outgoing_host="mac", incoming_host="linux", request_id=request_id
        ),
        "DUPLICATE_REQUEST",
    )

    mac.confirm_off(request_id=request_id, confirmed_local_off=True)
    linux.claim()
    # Replaying the old request after it was consumed cannot replace the
    # durable ACTIVE owner or authorize the former outgoing host.
    assert_refused(
        lambda: mac.request_cutover(
            outgoing_host="mac", incoming_host="linux", request_id=request_id
        ),
        "DUPLICATE_REQUEST",
    )
    assert_refused(mac.claim, "AUTHORITY_HELD_BY_OTHER_HOST")


def test_malformed_independent_readback_is_refused(fence_pair):
    mac, linux, _clock = fence_pair
    request_id = mac.request_cutover(
        outgoing_host="mac", incoming_host="linux", request_id="malformed-readback"
    )
    mac.confirm_off(request_id=request_id, confirmed_local_off=True)
    state_path = mac.host_state_path("mac")
    state_path.write_text(json.dumps({"state": "OFF"}), encoding="utf-8")

    assert_refused(linux.claim, "OFF_READBACK_MALFORMED")


def test_unreadable_authority_lock_is_a_terminal_fence_refusal(fence_pair):
    _mac, linux, _clock = fence_pair
    linux.root.mkdir(parents=True, exist_ok=True)
    (linux.root / "authority.lock").mkdir()

    assert_refused(linux.claim, "FENCE_LOCK_UNAVAILABLE")


def test_directory_fsync_failure_refuses_to_publish_authority(fence_pair, monkeypatch):
    mac, _linux, _clock = fence_pair

    def fail_fsync(_path):
        raise OSError("directory fsync unavailable")

    monkeypatch.setattr(CutoverFenceStore, "_fsync_directory", staticmethod(fail_fsync))

    assert_refused(
        lambda: mac.request_cutover(
            outgoing_host="mac", incoming_host="linux", request_id="no-durable-rename"
        ),
        "FENCE_FILESYSTEM_UNAVAILABLE",
    )
    # os.replace may already have happened when the directory fsync fails;
    # the caller still receives no successful transition acknowledgement.


def test_confirm_off_refuses_while_identity_consumer_lock_is_held(fence_pair):
    mac, _linux, _clock = fence_pair
    request_id = mac.request_cutover(
        outgoing_host="mac", incoming_host="linux", request_id="live-consumer"
    )
    held = SingleInstanceLock(mac.consumer_lock_path)
    held.acquire()
    try:
        assert_refused(
            lambda: mac.confirm_off(request_id=request_id, confirmed_local_off=True),
            "LOCAL_CONSUMER_STILL_ACTIVE",
        )
    finally:
        held.release()


def test_distinct_local_state_paths_share_one_fence_identity_lock(fence_pair, tmp_path):
    mac, _linux, _clock = fence_pair
    first_local_database = tmp_path / "state-a" / "bot.db"
    second_local_database = tmp_path / "state-b" / "bot.db"
    assert first_local_database.parent != second_local_database.parent

    first = SingleInstanceLock(mac.consumer_lock_path)
    second = SingleInstanceLock(mac.consumer_lock_path)
    first.acquire()
    try:
        with pytest.raises(RuntimeError):
            second.acquire()
    finally:
        first.release()
