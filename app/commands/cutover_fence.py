"""Operator-only commands for the two-host Telegram cutover fence.

These commands never start, stop, enable, or disable a service.  The
operator performs that local lifecycle action separately, then uses
``confirm-off`` to publish the positive OFF fact.  The incoming
``run_telegram_bot`` startup performs the independent read-back and claims
the authority before its first Telegram request.
"""

from __future__ import annotations

import argparse
import sys

from app.config import load_settings
from app.cutover_fence import CutoverFenceError, CutoverFenceStore


def _store() -> CutoverFenceStore:
    settings = load_settings()
    if settings.cutover_fence_path is None:
        raise CutoverFenceError(
            "FENCE_NOT_CONFIGURED",
            "CUTOVER_FENCE_PATH is not configured",
        )
    if settings.cutover_host_id == "unconfigured":
        raise CutoverFenceError(
            "HOST_ID_NOT_CONFIGURED",
            "CUTOVER_HOST_ID is not configured",
        )
    return CutoverFenceStore(
        settings.cutover_fence_path,
        token=settings.telegram_bot_token,
        host_id=settings.cutover_host_id,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the Telegram cutover authority record")
    subparsers = parser.add_subparsers(dest="action", required=True)

    request = subparsers.add_parser(
        "request", help="record a new, non-authorizing hand-off request"
    )
    request.add_argument("--outgoing-host", required=True)
    request.add_argument("--incoming-host", required=True)
    request.add_argument("--request-id")

    confirm = subparsers.add_parser(
        "confirm-off", help="record positive OFF after the local consumer is stopped"
    )
    confirm.add_argument("--request-id", required=True)
    confirm.add_argument(
        "--confirmed-local-off",
        action="store_true",
        help="assert that the local getUpdates consumer was checked and is stopped",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        store = _store()
        if args.action == "request":
            request_id = store.request_cutover(
                outgoing_host=args.outgoing_host,
                incoming_host=args.incoming_host,
                request_id=args.request_id,
            )
            print(f"CUTOVER_REQUESTED request_id={request_id}")
        elif args.action == "confirm-off":
            store.confirm_off(
                request_id=args.request_id,
                confirmed_local_off=args.confirmed_local_off,
            )
            print(f"CUTOVER_OFF_RECORDED request_id={args.request_id}")
        else:  # pragma: no cover - argparse enforces the subcommands
            raise ValueError(f"unknown action: {args.action}")
    except (CutoverFenceError, OSError, ValueError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
