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
never mean safe.  The fence-identity ``SingleInstanceLock`` is held for the
consumer's lifetime and also makes the outgoing OFF check observable; this
module supplies both the local identity lock and the cross-host hand-off
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

from app.process_lock import SingleInstanceLock

FENCE_SCHEMA_VERSION = 1
FENCE_TTL_SECONDS = 15 * 60
FENCE_REFUSAL_EXIT_CODE = 78

AUTHORITY_KIND = "telegram-getupdates-cutover"
HOST_STATE_KIND = "telegram-getupdates-host-state"
PHASE_REQUESTED = "REQUESTED"
PHASE_OFF_RECORDED = "OFF_RECORDED"
PHASE_ACTIVE = "ACTIVE"
OFF_CONFIRMATION_FACT = "local getUpdates consumer positively confirmed stopped"

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


def _check_schema_version(value: Any, *, code: str, record_name: str) -> None:
    if type(value) is not int or value != FENCE_SCHEMA_VERSION:
        raise CutoverFenceError(
            code,
            f"unsupported {record_name} schema; schema_version must be integer {FENCE_SCHEMA_VERSION}",
        )


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

    @property
    def consumer_lock_path(self) -> Path:
        """The process-lifetime lock for this token/fence/host identity.

        The fence root is part of the identity because it is the shared
        authority location.  Keeping the token digest and host id in the
        filename makes two deployments with different local database paths
        contend for the same lock, while different bot tokens do not.
        """
        identity = hashlib.sha256(
            f"{self.token_digest}\0{self.host_id}".encode("utf-8")
        ).hexdigest()
        return self.root / "consumers" / f"{self.host_id}-{identity}.lock"

    @contextmanager
    def _authority_lock(self, *, create: bool) -> Iterator[None]:
        try:
            if create:
                self.root.mkdir(parents=True, exist_ok=True)
            elif not self.root.is_dir():
                yield
                return
        except OSError as exc:
            raise CutoverFenceError(
                "FENCE_FILESYSTEM_UNAVAILABLE",
                "the fence directory cannot be created or inspected",
            ) from exc

        lock_path = self.root / "authority.lock"
        try:
            lock_file = open(lock_path, "a+", encoding="utf-8")
        except OSError as exc:
            raise CutoverFenceError(
                "FENCE_LOCK_UNAVAILABLE",
                "the shared authority lock cannot be opened",
            ) from exc
        try:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
            except OSError as exc:
                raise CutoverFenceError(
                    "FENCE_LOCK_UNAVAILABLE",
                    "the shared authority lock cannot be acquired",
                ) from exc
            try:
                yield
            finally:
                try:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)
                except OSError as exc:
                    raise CutoverFenceError(
                        "FENCE_LOCK_UNAVAILABLE",
                        "the shared authority lock cannot be released safely",
                    ) from exc
        finally:
            lock_file.close()

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            directory_fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError as exc:
            raise CutoverFenceError(
                "FENCE_FILESYSTEM_UNAVAILABLE",
                "the fence directory cannot be opened for durability verification",
            ) from exc
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            raise CutoverFenceError(
                "FENCE_FILESYSTEM_UNAVAILABLE",
                "the fence directory does not provide durable rename semantics",
            ) from exc
        finally:
            os.close(directory_fd)

    @staticmethod
    def _atomic_write(path: Path, value: dict[str, Any]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        except OSError as exc:
            raise CutoverFenceError(
                "FENCE_FILESYSTEM_UNAVAILABLE",
                "the fence record cannot be written atomically",
            ) from exc
        temp_path = Path(temp_name)
        try:
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(value, handle, ensure_ascii=True, sort_keys=True)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, path)
                CutoverFenceStore._fsync_directory(path.parent)
            except CutoverFenceError:
                raise
            except (OSError, TypeError, ValueError) as exc:
                raise CutoverFenceError(
                    "FENCE_FILESYSTEM_UNAVAILABLE",
                    "the fence record cannot be committed durably",
                ) from exc
        finally:
            temp_path.unlink(missing_ok=True)

    def verify_filesystem(self) -> None:
        """Verify the local shared-fence operations required by this protocol.

        The probe is intentionally performed under the same advisory lock as
        authority transitions.  It proves that this client can lock, atomically
        replace, read back, and durably commit a file.  Cross-client lock and
        cache coherence still require the deployment contract and a probe run
        from both hosts; no single process can prove another client's mount
        semantics.
        """
        probe_path = self.root / ".fence-filesystem-probe.json"
        probe = {"nonce": uuid.uuid4().hex}
        with self._authority_lock(create=True):
            self._atomic_write(probe_path, probe)
            observed = _read_json(
                probe_path,
                missing_code="FENCE_FILESYSTEM_UNAVAILABLE",
                malformed_code="FENCE_FILESYSTEM_UNAVAILABLE",
            )
            if observed != probe:
                raise CutoverFenceError(
                    "FENCE_FILESYSTEM_UNAVAILABLE",
                    "the shared fence read-back is not current",
                )
            try:
                probe_path.unlink()
            except OSError as exc:
                raise CutoverFenceError(
                    "FENCE_FILESYSTEM_UNAVAILABLE",
                    "the shared fence probe cannot be removed",
                ) from exc
            self._fsync_directory(self.root)

    def _read_authority(self) -> dict[str, Any]:
        return _read_json(
            self.authority_path,
            missing_code="AUTHORITY_ABSENT",
            malformed_code="AUTHORITY_MALFORMED",
        )

    def _check_common_authority(self, record: dict[str, Any]) -> None:
        _check_schema_version(
            record.get("schema_version"),
            code="AUTHORITY_MALFORMED",
            record_name="authority",
        )
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
        issued_at = _parse_timestamp(record.get("issued_at"), code="AUTHORITY_MALFORMED")
        expires_at = _parse_timestamp(record.get("expires_at"), code="AUTHORITY_MALFORMED")
        if expires_at <= issued_at:
            raise CutoverFenceError(
                "AUTHORITY_MALFORMED", "authority lifetime must end after it is issued"
            )
        if expires_at - issued_at > timedelta(seconds=FENCE_TTL_SECONDS):
            raise CutoverFenceError(
                "AUTHORITY_MALFORMED", "authority lifetime exceeds the fence TTL"
            )
        now = self._now()
        if issued_at > now:
            raise CutoverFenceError("AUTHORITY_MALFORMED", "authority was issued in the future")
        if expires_at <= now:
            raise CutoverFenceError("AUTHORITY_EXPIRED", "cutover evidence is stale")

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
        require_fresh: bool = True,
    ) -> None:
        _check_schema_version(
            state.get("schema_version"),
            code="OFF_READBACK_MALFORMED",
            record_name="host-state",
        )
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
        if require_fresh:
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
                elif phase == PHASE_ACTIVE:
                    if current.get("active_host") != outgoing_host:
                        raise CutoverFenceError(
                            "AUTHORITY_HELD_BY_OTHER_HOST",
                            "only the current active host may begin rollback",
                        )
                    self._check_active_artifact(current)
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
        """Record positive OFF after the consumer identity lock is free."""
        if not confirmed_local_off:
            raise CutoverFenceError(
                "OFF_CONFIRMATION_REQUIRED",
                "the outgoing operator must positively confirm the local consumer is stopped",
            )
        request_id = _require_request_id(request_id)
        # Acquire in the same order as run_telegram_bot (consumer identity,
        # then authority) so an operator confirmation cannot deadlock with a
        # process that is entering the polling loop.
        consumer_lock = SingleInstanceLock(self.consumer_lock_path)
        try:
            consumer_lock.acquire()
        except RuntimeError as exc:
            raise CutoverFenceError(
                "LOCAL_CONSUMER_STILL_ACTIVE",
                "the local getUpdates consumer still holds the fence identity lock",
            ) from exc
        try:
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
                    raise CutoverFenceError(
                        "OFF_ALREADY_RECORDED", "OFF evidence was already recorded"
                    )
                if record["phase"] != PHASE_REQUESTED:
                    raise CutoverFenceError("CUTOVER_NOT_REQUESTED", "cutover is not awaiting OFF")
                self._record_off(record, request_id)
        finally:
            consumer_lock.release()

    def _record_off(self, record: dict[str, Any], request_id: str) -> None:
        """Write OFF evidence while the caller holds the identity lock."""
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
            "fact": OFF_CONFIRMATION_FACT,
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

    def _check_off_evidence(self, record: dict[str, Any]) -> datetime:
        """Validate the authority OFF fact and its independent read-back."""
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
        if "fact" not in outgoing_off:
            raise CutoverFenceError(
                "AUTHORITY_OFF_FACT_MISSING", "authority has no positive OFF fact"
            )
        if outgoing_off["fact"] != OFF_CONFIRMATION_FACT:
            raise CutoverFenceError(
                "AUTHORITY_OFF_FACT_MALFORMED", "OFF fact is not a positive confirmation"
            )

        off_observed_at = _parse_timestamp(
            outgoing_off.get("observed_at"), code="AUTHORITY_OFF_FACT_MALFORMED"
        )
        issued_at = _parse_timestamp(record.get("issued_at"), code="AUTHORITY_OFF_FACT_MALFORMED")
        if off_observed_at < issued_at:
            raise CutoverFenceError(
                "AUTHORITY_OFF_FACT_MALFORMED",
                "OFF evidence predates the authority issue time",
            )

        # This is the independent read-back: a fresh read of a separate
        # host-state record, not a trust decision based on authority.json.
        outgoing_state = self.read_back_off(
            host_id=record["outgoing_host"], request_id=record["request_id"]
        )
        readback_observed_at = _parse_timestamp(
            outgoing_state.get("observed_at"), code="OFF_READBACK_MALFORMED"
        )
        if readback_observed_at != off_observed_at:
            raise CutoverFenceError(
                "OFF_READBACK_TIMESTAMP_MISMATCH",
                "authority OFF and independent OFF read-back timestamps differ",
            )
        return off_observed_at

    def _check_active_artifact(self, record: dict[str, Any]) -> None:
        """Validate every durable fact required for same-host restart."""
        try:
            active_host = _require_host_id(record["active_host"], field="active_host")
        except (KeyError, ValueError) as exc:
            raise CutoverFenceError(
                "ACTIVE_ARTIFACT_MALFORMED", "ACTIVE authority has no valid active host"
            ) from exc
        if active_host != record["incoming_host"]:
            raise CutoverFenceError(
                "ACTIVE_ARTIFACT_MALFORMED",
                "ACTIVE authority active_host does not match incoming_host",
            )

        issued_at = _parse_timestamp(record.get("issued_at"), code="ACTIVE_ARTIFACT_MALFORMED")
        expires_at = _parse_timestamp(record.get("expires_at"), code="ACTIVE_ARTIFACT_MALFORMED")
        if expires_at <= issued_at or expires_at - issued_at > timedelta(seconds=FENCE_TTL_SECONDS):
            raise CutoverFenceError(
                "ACTIVE_ARTIFACT_MALFORMED", "ACTIVE authority lifetime is inconsistent"
            )
        active_since = _parse_timestamp(
            record.get("active_since"), code="ACTIVE_ARTIFACT_MALFORMED"
        )
        if active_since < issued_at or active_since > expires_at or active_since > self._now():
            raise CutoverFenceError(
                "ACTIVE_ARTIFACT_MALFORMED", "ACTIVE authority timestamp is inconsistent"
            )

        outgoing_off = record.get("outgoing_off")
        if not isinstance(outgoing_off, dict):
            raise CutoverFenceError(
                "ACTIVE_ARTIFACT_MALFORMED", "ACTIVE authority has no positive OFF fact"
            )
        if (
            outgoing_off.get("host_id") != record["outgoing_host"]
            or outgoing_off.get("request_id") != record["request_id"]
            or outgoing_off.get("fact") != OFF_CONFIRMATION_FACT
        ):
            raise CutoverFenceError(
                "ACTIVE_ARTIFACT_MALFORMED", "ACTIVE authority OFF fact is inconsistent"
            )
        off_observed_at = _parse_timestamp(
            outgoing_off.get("observed_at"), code="ACTIVE_ARTIFACT_MALFORMED"
        )
        if off_observed_at < issued_at or off_observed_at > active_since:
            raise CutoverFenceError(
                "ACTIVE_ARTIFACT_MALFORMED", "ACTIVE authority OFF timestamp is inconsistent"
            )

        try:
            outgoing_state = self._read_host_state(record["outgoing_host"])
            incoming_state = self._read_host_state(record["incoming_host"])
        except CutoverFenceError as exc:
            raise CutoverFenceError(
                "ACTIVE_ARTIFACT_INCOMPLETE",
                "ACTIVE authority is missing a host-state record",
            ) from exc
        self._check_host_state(
            outgoing_state,
            expected_host=record["outgoing_host"],
            expected_request_id=record["request_id"],
            require_off=True,
            require_fresh=False,
        )
        if _parse_timestamp(outgoing_state["observed_at"], code="ACTIVE_ARTIFACT_MALFORMED") != off_observed_at:
            raise CutoverFenceError(
                "ACTIVE_ARTIFACT_MALFORMED", "ACTIVE authority does not match OFF read-back"
            )
        self._check_host_state(
            incoming_state,
            expected_host=record["incoming_host"],
            expected_request_id=record["request_id"],
            require_off=False,
            require_fresh=False,
        )
        if incoming_state.get("state") != "ACTIVE":
            raise CutoverFenceError(
                "ACTIVE_ARTIFACT_MALFORMED", "incoming host did not record ACTIVE"
            )
        if _parse_timestamp(incoming_state["observed_at"], code="ACTIVE_ARTIFACT_MALFORMED") != active_since:
            raise CutoverFenceError(
                "ACTIVE_ARTIFACT_MALFORMED", "ACTIVE authority does not match active read-back"
            )
        # The same expiry and issue-time checks apply to a restart as to the
        # initial claim.  The active timestamp check above supplies the
        # remaining adjacent relation in the transition chronology.
        self._check_expiry(record)

    def claim(self) -> dict[str, Any]:
        """Atomically consume a valid hand-off for this host before polling."""
        self.verify_filesystem()
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
                # A same-host restart may reuse durable authority, but only
                # after validating the complete transition artifact.  The
                # process-lifetime identity lock is held by run_telegram_bot.
                self._check_active_artifact(record)
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
            off_observed_at = self._check_off_evidence(record)

            claimed = dict(record)
            claimed["phase"] = PHASE_ACTIVE
            claimed["active_host"] = self.host_id
            claimed["active_since"] = _timestamp(self._now())
            active_since = _parse_timestamp(
                claimed["active_since"], code="ACTIVE_ARTIFACT_MALFORMED"
            )
            if active_since < off_observed_at:
                raise CutoverFenceError(
                    "AUTHORITY_OFF_FACT_MALFORMED",
                    "claim time predates the outgoing OFF evidence",
                )
            # Publish the incoming ACTIVE read-back before the authority
            # transition.  A durable ACTIVE record therefore always has both
            # host-state records required by the same-host restart branch.
            self._write_host_state(
                claimed,
                state="ACTIVE",
                observed_at=active_since,
            )
            # Validate the exact artifact that will be acknowledged as ACTIVE;
            # this keeps entry and same-host restart validation canonical.
            self._check_active_artifact(claimed)
            self._atomic_write(self.authority_path, claimed)
            return claimed
