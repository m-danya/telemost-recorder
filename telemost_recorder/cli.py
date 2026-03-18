from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from pathlib import Path

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


def _read_process_argv(pid: int) -> tuple[str, ...] | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    parts = [part for part in raw.decode("utf-8", errors="ignore").split("\x00") if part]
    if not parts:
        return None
    return tuple(parts)


def _looks_like_monitoring_runtime(argv: tuple[str, ...]) -> bool:
    if "run" not in argv:
        return False
    names = {Path(part).name for part in argv}
    return bool({"telemost-recorder", "telemost_recorder"} & names)


def _select_runtime_pid(service_pids: list[int], *, main_pid: int) -> int | None:
    unique_pids = sorted(set(service_pids))

    for pid in unique_pids:
        if pid == main_pid:
            continue
        argv = _read_process_argv(pid)
        if argv is None or not _looks_like_monitoring_runtime(argv):
            continue
        if Path(argv[0]).name != "uv":
            return pid

    if main_pid > 0:
        argv = _read_process_argv(main_pid)
        if argv is not None and _looks_like_monitoring_runtime(argv) and Path(argv[0]).name != "uv":
            return main_pid

    return None


async def _resolve_monitoring_runtime_pid(service_name: str) -> tuple[int | None, str | None]:
    try:
        main_code, main_stdout, main_stderr = await _run_command(
            "systemctl",
            "--user",
            "show",
            "-P",
            "MainPID",
            service_name,
        )
        cgroup_code, cgroup_stdout, cgroup_stderr = await _run_command(
            "systemctl",
            "--user",
            "show",
            "-P",
            "ControlGroup",
            service_name,
        )
    except FileNotFoundError:
        return None, "systemctl binary is not available in PATH"

    if main_code != 0:
        return None, main_stderr or "failed to read MainPID"
    if cgroup_code != 0:
        return None, cgroup_stderr or "failed to read ControlGroup"

    try:
        main_pid = int(main_stdout.strip())
    except ValueError:
        return None, f"invalid MainPID value: {main_stdout!r}"
    if main_pid <= 0:
        return None, f"service has no main pid: {service_name}"

    control_group = cgroup_stdout.strip()
    if not control_group:
        return None, f"service has no control group: {service_name}"

    cgroup_procs_path = Path("/sys/fs/cgroup") / control_group.lstrip("/") / "cgroup.procs"
    try:
        service_pids = [
            int(line.strip())
            for line in cgroup_procs_path.read_text().splitlines()
            if line.strip()
        ]
    except OSError as exc:
        runtime_pid = _select_runtime_pid([main_pid], main_pid=main_pid)
        if runtime_pid is not None:
            return runtime_pid, None
        return None, f"failed to inspect cgroup {cgroup_procs_path}: {exc}"

    runtime_pid = _select_runtime_pid(service_pids, main_pid=main_pid)
    if runtime_pid is None:
        return None, f"could not find monitoring runtime pid in cgroup {control_group}"
    return runtime_pid, None


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

    runtime_pid, resolve_error = await _resolve_monitoring_runtime_pid(service_name)
    if runtime_pid is None:
        logger.error(
            "failed to resolve monitoring service pid: %s%s",
            service_name,
            f" ({resolve_error})" if resolve_error else "",
        )
        return 1

    try:
        os.kill(runtime_pid, signal.SIGUSR1)
    except OSError as exc:
        logger.error("failed to signal monitoring service: %s (pid=%s, %s)", service_name, runtime_pid, exc)
        return 1

    logger.info("manual_trigger_sent service=%s pid=%s", service_name, runtime_pid)
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
