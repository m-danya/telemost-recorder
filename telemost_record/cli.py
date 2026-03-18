from __future__ import annotations

import argparse
import asyncio
import logging

from pydantic import ValidationError

from telemost_record.config import Settings
from telemost_record.logging_utils import setup_logging
from telemost_record.service import TelemostService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Record Yandex Telemost meetings on schedule")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("run", help="run the scheduler service")

    once_parser = subparsers.add_parser("once", help="start one recording immediately")
    once_parser.add_argument(
        "--start-now",
        action="store_true",
        help="accepted for compatibility; one-off mode always starts immediately",
    )

    subparsers.add_parser("check", help="verify Chromium, ffmpeg, and audio capture")
    return parser


async def _async_main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    setup_logging()
    logger = logging.getLogger("telemost_record")

    try:
        settings = Settings()
    except ValidationError as exc:
        logger.error("invalid configuration: %s", exc)
        return 2

    service = TelemostService(settings)

    try:
        if args.command == "run":
            await service.run()
        elif args.command == "once":
            await service.run_once()
        elif args.command == "check":
            await service.check_environment()
        else:
            parser.error(f"unsupported command: {args.command}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("command_failed: %s", exc)
        return 1
    return 0


def main() -> int:
    return asyncio.run(_async_main())
