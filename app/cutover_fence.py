"""A small, two-host admission fence for Telegram ``getUpdates``.

The fence is deliberately a file protocol, not a lease service.  The
directory named by ``CUTOVER_FENCE_PATH`` must be an operator-provisioned
shared authority location visible to both hosts.  It contains:

* ``authority.json`` — the one current cutover request and its state; and
* ``hosts/<host>.json`` — a separately written, host-owned observation of
  that host's local consumer state.

An incoming host may claim a request only when the authority record says the
outgoing host is OFF *and* a fresh read of the outgoing host record says OFF
for the same request.  Silence, missing files, expiry, and malformed data
never mean safe.  The local ``SingleInstanceLock`` remains responsible for
two processes on one filesystem; this module is the cross-host hand-off
fence.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

FENCE_SCHEMA_VERSION = 1
FENCE_TTL_SECONDS = 15 * 60
FENCE_REFUSAL_EXIT_CODE = 78

AUTHORITY_KIND = "telegram-getupdates-cutover"
HOST_STATE_KIND = "telegram-getupdates-host-state"
PHASE_REQUESTED = "REQUESTED"
PHASE_OFF_RECORDED = "OFF_RECORDED"
PHASE_ACTIVE = "ACTIVE"

_HOST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class CutoverFenceError(RuntimeError):
    """A loud, stable startup refusal with a reason suitable for tests/logs."""

    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"CUTOVER_FENCE_REFUSED[{code}]: {detail}")


def token_fingerprint(token: str) -> str:
    """Bind a record to a bot token without persisting or displaying the token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("fence timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any, *, code: str) -> datetime:
    if not isinstance(value, str):
        raise CutoverFenceError(code, "timestamp is missing or not a string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CutoverFenceError(code, "timestamp is malformed") from exc
    if parsed.tzinfo is None:
        raise CutoverFenceError(code, "timestamp has no timezone")
    return parsed.astimezone(UTC)


def _require_host_id(value: str, *, field: str = "host id") -> str:
    if not isinstance(value, str) or not _HOST_ID_RE.fullmatch(value):
        raise ValueError(f"{field} must match {_HOST_ID_RE.pattern}")
    return value


def _require_request_id(value: str) -> str:
    if not isinstance(value, str) or not _REQUEST_ID_RE.fullmatch(value):
        raise ValueError(f"request_id must match {_REQUEST_ID_RE.pattern}")
    return value


def _read_json(path: Path, *, missing_code: str, malformed_code: str) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CutoverFenceError(missing_code, f"required evidence is absent: {path.name}") from exc
    except OSError as exc:
        raise CutoverFenceError(malformed_code, f"evidence cannot be read: {path.name}") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CutoverFenceError(malformed_code, f"evidence is not valid JSON: {path.name}") from exc
    if not isinstance(value, dict):
        raise CutoverFenceError(malformed_code, f"evidence must be a JSON object: {path.name}")
    return value


class CutoverFenceStore:
    """Read and atomically transition the one current cutover authority.

    ``now`` is injectable solely for deterministic tests.  A production store
    uses UTC wall-clock time and never treats a timer expiry or an unreadable
    peer as evidence of OFF.
    """

    def __init__(
        self,
        root: Path,
        *,
        token: str,
        host_id: str,
        now: Any = _utc_now,
    ):
        self.root = Path(root)
        self.token_digest = token_fingerprint(token)
        self.host_id = _require_host_id(host_id)
        self._now = now

    @property
    def authority_path(self) -> Path:
        return self.root / "authority.json"

    def host_state_path(self, host_id: str) -> Path:
        return self.root / "hosts" / f"{_require_host_id(host_id)}.json"

    @contextmanager
    def _authority_lock(self, *, create: bool) -> Iterator[None]:
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        elif not self.root.is_dir():
            yield
            return

        lock_path = self.root / "authority.lock"
        with open(lock_path, "a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    @staticmethod
    def _atomic_write(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=True, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)

    def _read_authority(self) -> dict[str, Any]:
        return _read_json(
            self.authority_path,
            missing_code="AUTHORITY_ABSENT",
            malformed_code="AUTHORITY_MALFORMED",
        )

    def _check_common_authority(self, record: dict[str, Any]) -> None:
        if record.get("schema_version") != FENCE_SCHEMA_VERSION:
            raise CutoverFenceError("AUTHORITY_MALFORMED", "unsupported authority schema")
        if record.get("kind") != AUTHORITY_KIND:
            raise CutoverFenceError("AUTHORITY_MALFORMED", "wrong authority record kind")
        if record.get("token_fingerprint") != self.token_digest:
            raise CutoverFenceError("TOKEN_MISMATCH", "authority belongs to another bot token")
        try:
            _require_request_id(record["request_id"])
            _require_host_id(record["outgoing_host"], field="outgoing_host")
            _require_host_id(record["incoming_host"], field="incoming_host")
        except (KeyError, ValueError) as exc:
            raise CutoverFenceError(
                "AUTHORITY_MALFORMED", "authority identity is malformed"
            ) from exc
        if record["outgoing_host"] == record["incoming_host"]:
            raise CutoverFenceError(
                "AUTHORITY_MALFORMED", "outgoing and incoming hosts must differ"
            )
        if record.get("phase") not in {PHASE_REQUESTED, PHASE_OFF_RECORDED, PHASE_ACTIVE}:
            raise CutoverFenceError("AUTHORITY_MALFORMED", "unknown authority phase")
        _parse_timestamp(record.get("issued_at"), code="AUTHORITY_MALFORMED")

    def _check_expiry(self, record: dict[str, Any]) -> None:
        expires_at = _parse_timestamp(record.get("expires_at"), code="AUTHORITY_MALFORMED")
        now = self._now()
        if expires_at <= now:
            raise CutoverFenceError("AUTHORITY_EXPIRED", "cutover evidence is stale")
        if expires_at - _parse_timestamp(
            record["issued_at"], code="AUTHORITY_MALFORMED"
        ) > timedelta(seconds=FENCE_TTL_SECONDS):
            raise CutoverFenceError(
                "AUTHORITY_MALFORMED", "authority lifetime exceeds the fence TTL"
            )

    def _read_host_state(self, host_id: str) -> dict[str, Any]:
        return _read_json(
            self.host_state_path(host_id),
            missing_code="OFF_READBACK_ABSENT",
            malformed_code="OFF_READBACK_MALFORMED",
        )

    def _check_host_state(
        self,
        state: dict[str, Any],
        *,
        expected_host: str,
        expected_request_id: str,
        require_off: bool,
    ) -> None:
        if state.get("schema_version") != FENCE_SCHEMA_VERSION:
            raise CutoverFenceError("OFF_READBACK_MALFORMED", "unsupported host-state schema")
        if state.get("kind") != HOST_STATE_KIND:
            raise CutoverFenceError("OFF_READBACK_MALFORMED", "wrong host-state record kind")
        if state.get("token_fingerprint") != self.token_digest:
            raise CutoverFenceError("TOKEN_MISMATCH", "host-state belongs to another bot token")
        if state.get("host_id") != expected_host:
            raise CutoverFenceError("OFF_READBACK_WRONG_HOST", "read-back came from the wrong host")
        if state.get("request_id") != expected_request_id:
            raise CutoverFenceError(
                "OFF_READBACK_WRONG_REQUEST", "read-back belongs to another request"
            )
        state_value = state.get("state")
        if require_off and state_value != "OFF":
            raise CutoverFenceError(
                "OUTGOING_STILL_ACTIVE",
                f"outgoing host reported {state_value!r}, not positive OFF",
            )
        observed_at = _parse_timestamp(state.get("observed_at"), code="OFF_READBACK_MALFORMED")
        now = self._now()
        if observed_at > now:
            raise CutoverFenceError("OFF_READBACK_FUTURE", "read-back timestamp is in the future")
        if now - observed_at > timedelta(seconds=FENCE_TTL_SECONDS):
            raise CutoverFenceError("OFF_READBACK_STALE", "read-back evidence is stale")

    def request_cutover(
        self,
        *,
        outgoing_host: str,
        incoming_host: str,
        request_id: str | None = None,
    ) -> str:
        """Create a non-authorizing request; it cannot start a consumer."""
        outgoing_host = _require_host_id(outgoing_host, field="outgoing_host")
        incoming_host = _require_host_id(incoming_host, field="incoming_host")
        if outgoing_host == incoming_host:
            raise ValueError("outgoing_host and incoming_host must differ")
        if outgoing_host != self.host_id:
            raise CutoverFenceError(
                "REQUEST_WRONG_OUTGOING_HOST",
                "only the host being taken OFF may issue its cutover request",
            )
        request_id = _require_request_id(request_id or uuid.uuid4().hex)
        now = self._now()
        record = {
            "schema_version": FENCE_SCHEMA_VERSION,
            "kind": AUTHORITY_KIND,
            "request_id": request_id,
            "token_fingerprint": self.token_digest,
            "outgoing_host": outgoing_host,
            "incoming_host": incoming_host,
            "phase": PHASE_REQUESTED,
            "issued_at": _timestamp(now),
            "expires_at": _timestamp(now + timedelta(seconds=FENCE_TTL_SECONDS)),
        }

        with self._authority_lock(create=True):
            if self.authority_path.exists():
                current = self._read_authority()
                self._check_common_authority(current)
                if current.get("request_id") == request_id:
                    raise CutoverFenceError("DUPLICATE_REQUEST", "request_id was already used")
                phase = current["phase"]
                if phase in {PHASE_REQUESTED, PHASE_OFF_RECORDED}:
                    try:
                        self._check_expiry(current)
                    except CutoverFenceError as exc:
                        if exc.code != "AUTHORITY_EXPIRED":
                            raise
                    else:
                        raise CutoverFenceError(
                            "CUTOVER_IN_PROGRESS",
                            "another cutover must finish or be repaired first",
                        )
                elif phase == PHASE_ACTIVE and current.get("active_host") != outgoing_host:
                    raise CutoverFenceError(
                        "AUTHORITY_HELD_BY_OTHER_HOST",
                        "only the current active host may begin rollback",
                    )
            self._atomic_write(self.authority_path, record)
        return request_id

    def publish_active_state(self, *, request_id: str) -> None:
        """Publish a positive ACTIVE observation for an outgoing host.

        This is useful for the pre-cutover host status and for tests.  It is
        not an authority grant: the incoming host still cannot claim it.
        """
        request_id = _require_request_id(request_id)
        with self._authority_lock(create=False):
            record = self._read_authority()
            self._check_common_authority(record)
            if record["request_id"] != request_id:
                raise CutoverFenceError(
                    "REQUEST_NOT_FOUND", "request_id does not name the current request"
                )
            if record["outgoing_host"] != self.host_id:
                raise CutoverFenceError(
                    "REQUEST_WRONG_OUTGOING_HOST", "host is not the outgoing host"
                )
            if record["phase"] != PHASE_REQUESTED:
                raise CutoverFenceError(
                    "CUTOVER_NOT_REQUESTED", "active state can only predate OFF"
                )
            self._write_host_state(record, state="ACTIVE", observed_at=self._now())

    def confirm_off(self, *, request_id: str, confirmed_local_off: bool = False) -> None:
        """Record positive OFF and its separately readable host observation."""
        if not confirmed_local_off:
            raise CutoverFenceError(
                "OFF_CONFIRMATION_REQUIRED",
                "the outgoing operator must positively confirm the local consumer is stopped",
            )
        request_id = _require_request_id(request_id)
        with self._authority_lock(create=False):
            record = self._read_authority()
            self._check_common_authority(record)
            self._check_expiry(record)
            if record["request_id"] != request_id:
                raise CutoverFenceError(
                    "REQUEST_NOT_FOUND", "request_id does not name the current request"
                )
            if record["outgoing_host"] != self.host_id:
                raise CutoverFenceError(
                    "REQUEST_WRONG_OUTGOING_HOST", "host is not the outgoing host"
                )
            if record["phase"] == PHASE_OFF_RECORDED:
                raise CutoverFenceError("OFF_ALREADY_RECORDED", "OFF evidence was already recorded")
            if record["phase"] != PHASE_REQUESTED:
                raise CutoverFenceError("CUTOVER_NOT_REQUESTED", "cutover is not awaiting OFF")

            observed_at = self._now()
            # The host-state file is deliberately separate from authority.json.
            # A crash between either write leaves the request non-claimable.
            self._write_host_state(record, state="OFF", observed_at=observed_at)
            record = dict(record)
            record["phase"] = PHASE_OFF_RECORDED
            record["outgoing_off"] = {
                "host_id": self.host_id,
                "request_id": request_id,
                "observed_at": _timestamp(observed_at),
                "fact": "local getUpdates consumer positively confirmed stopped",
            }
            self._atomic_write(self.authority_path, record)

    def _write_host_state(
        self,
        authority: dict[str, Any],
        *,
        state: str,
        observed_at: datetime,
    ) -> None:
        host_state = {
            "schema_version": FENCE_SCHEMA_VERSION,
            "kind": HOST_STATE_KIND,
            "token_fingerprint": self.token_digest,
            "host_id": self.host_id,
            "request_id": authority["request_id"],
            "state": state,
            "observed_at": _timestamp(observed_at),
        }
        self._atomic_write(self.host_state_path(self.host_id), host_state)

    def read_back_off(self, *, host_id: str, request_id: str) -> dict[str, Any]:
        """Freshly read and validate the outgoing host's independent OFF fact."""
        request_id = _require_request_id(request_id)
        state = self._read_host_state(host_id)
        self._check_host_state(
            state,
            expected_host=host_id,
            expected_request_id=request_id,
            require_off=True,
        )
        return state

    def claim(self) -> dict[str, Any]:
        """Atomically consume a valid hand-off for this host before polling."""
        with self._authority_lock(create=False):
            record = self._read_authority()
            self._check_common_authority(record)
            phase = record["phase"]
            if phase == PHASE_ACTIVE:
                if record.get("active_host") != self.host_id:
                    raise CutoverFenceError(
                        "AUTHORITY_HELD_BY_OTHER_HOST",
                        "another host already owns the active consumer authority",
                    )
                # A same-host restart may reuse durable authority.  The local
                # SingleInstanceLock prevents a second same-filesystem process.
                return record

            if record.get("incoming_host") != self.host_id:
                raise CutoverFenceError(
                    "WRONG_INCOMING_HOST",
                    "this host is not the incoming host named by the request",
                )
            if phase == PHASE_REQUESTED:
                try:
                    state = self._read_host_state(record["outgoing_host"])
                except CutoverFenceError as exc:
                    if exc.code == "OFF_READBACK_ABSENT":
                        raise CutoverFenceError(
                            "CUTOVER_INCOMPLETE",
                            "cutover is interrupted before positive OFF/read-back",
                        ) from exc
                    raise
                # The previous request id is expected during a rollback
                # request: the outgoing host was already ACTIVE before the
                # new request was issued.  A positive ACTIVE state therefore
                # gets its own refusal reason before request-id comparison.
                if (
                    state.get("state") == "ACTIVE"
                    and state.get("host_id") == record["outgoing_host"]
                    and state.get("token_fingerprint") == self.token_digest
                ):
                    raise CutoverFenceError(
                        "OUTGOING_STILL_ACTIVE",
                        "outgoing host has positively reported ACTIVE",
                    )
                self._check_host_state(
                    state,
                    expected_host=record["outgoing_host"],
                    expected_request_id=record["request_id"],
                    require_off=False,
                )
                raise CutoverFenceError(
                    "CUTOVER_INCOMPLETE",
                    "authority is not positive OFF_RECORDED",
                )
            if phase != PHASE_OFF_RECORDED:
                raise CutoverFenceError(
                    "AUTHORITY_MALFORMED", "cannot claim unknown authority phase"
                )

            self._check_expiry(record)
            outgoing_off = record.get("outgoing_off")
            if not isinstance(outgoing_off, dict):
                raise CutoverFenceError(
                    "AUTHORITY_OFF_FACT_MISSING", "authority has no positive OFF fact"
                )
            if outgoing_off.get("host_id") != record["outgoing_host"]:
                raise CutoverFenceError(
                    "AUTHORITY_OFF_FACT_MALFORMED", "OFF fact names the wrong host"
                )
            if outgoing_off.get("request_id") != record["request_id"]:
                raise CutoverFenceError(
                    "AUTHORITY_OFF_FACT_MALFORMED", "OFF fact names the wrong request"
                )
            _parse_timestamp(outgoing_off.get("observed_at"), code="AUTHORITY_OFF_FACT_MALFORMED")

            # This is the independent read-back: a fresh read of a separate
            # host-state record, not a trust decision based on authority.json.
            self.read_back_off(host_id=record["outgoing_host"], request_id=record["request_id"])

            claimed = dict(record)
            claimed["phase"] = PHASE_ACTIVE
            claimed["active_host"] = self.host_id
            claimed["active_since"] = _timestamp(self._now())
            # Authority becomes ACTIVE before the best-effort ACTIVE status
            # write.  If that status write fails, safety is preserved: the
            # other host still sees this durable owner and cannot claim.
            self._atomic_write(self.authority_path, claimed)
            try:
                self._write_host_state(claimed, state="ACTIVE", observed_at=self._now())
            except OSError:
                pass
            return claimed
