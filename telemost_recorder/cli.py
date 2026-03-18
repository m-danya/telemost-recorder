from __future__ import annotations

import argparse
import asyncio
import logging

from pydantic import ValidationError

from telemost_recorder.config import Settings
from telemost_recorder.logging_utils import setup_logging
from telemost_recorder.service import TelemostService

MONITORING_SERVICE_NAME = "telemost-recorder-monitoring.service"


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

    trigger_parser = subparsers.add_parser(
        "trigger",
        help="ask the monitoring service to start recording immediately",
    )
    trigger_parser.add_argument(
        "--service-name",
        default=MONITORING_SERVICE_NAME,
        help="systemd --user service name to signal",
    )

    subparsers.add_parser("check", help="verify Chromium, ffmpeg, and audio capture")
    return parser


async def _run_command(*command: str) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    return process.returncode, stdout.decode().strip(), stderr.decode().strip()


async def trigger_monitoring_service(service_name: str) -> int:
    logger = logging.getLogger("telemost_recorder")

    try:
        status_code, _, status_stderr = await _run_command(
            "systemctl",
            "--user",
            "is-active",
            "--quiet",
            service_name,
        )
    except FileNotFoundError:
        logger.error("systemctl binary is not available in PATH")
        return 1

    if status_code != 0:
        logger.error(
            "monitoring service is not active: %s%s",
            service_name,
            f" ({status_stderr})" if status_stderr else "",
        )
        return 1

    trigger_code, _, trigger_stderr = await _run_command(
        "systemctl",
        "--user",
        "kill",
        "-s",
        "SIGUSR1",
        service_name,
    )
    if trigger_code != 0:
        logger.error(
            "failed to signal monitoring service: %s%s",
            service_name,
            f" ({trigger_stderr})" if trigger_stderr else "",
        )
        return 1

    logger.info("manual_trigger_sent service=%s", service_name)
    return 0


async def _async_main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    setup_logging()
    logger = logging.getLogger("telemost_recorder")

    if args.command == "trigger":
        return await trigger_monitoring_service(args.service_name)

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
