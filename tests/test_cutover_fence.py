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
