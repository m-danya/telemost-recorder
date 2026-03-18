from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import signal
import shutil
import time
from pathlib import Path

from telemost_recorder.config import Settings


class RecordingError(RuntimeError):
    pass


FFMPEG_LOGLEVEL_PRIORITY = {
    "panic": 0,
    "fatal": 0,
    "error": 0,
    "warning": 1,
    "info": 2,
    "verbose": 3,
    "debug": 4,
    "trace": 5,
}
FFMPEG_STDERR_LEVEL_RE = re.compile(r"^\[(?P<level>[a-z]+)\]\s*(?P<message>.*)$")


class FfmpegRecorder:
    def __init__(self, settings: Settings, audio_source: str) -> None:
        self.settings = settings
        self.audio_source = audio_source
        self.logger = logging.getLogger("telemost_recorder.recording")
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._silence_started_at: float | None = None
        self._stop_requested = False

    async def start(self, output_path: Path) -> None:
        command = self._build_record_command(output_path)
        self.logger.info("recording_started output=%s", output_path)
        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self._stderr_task = asyncio.create_task(self._consume_stderr())

    async def wait_until_stop_condition(self) -> str:
        process = self._require_process()
        waiter = asyncio.create_task(process.wait())
        try:
            while True:
                if waiter.done():
                    return_code = waiter.result()
                    if self._is_expected_stop_return_code(return_code):
                        return "stopped"
                    raise RecordingError(f"ffmpeg exited unexpectedly with code {return_code}")
                if self._silence_started_at is not None:
                    silence_for = time.monotonic() - self._silence_started_at
                    if silence_for >= self.settings.silence_timeout_seconds:
                        self.logger.info("silence_timeout_reached seconds=%s", int(silence_for))
                        await self.stop()
                        return "silence_timeout"
                await asyncio.sleep(1)
        finally:
            if not waiter.done():
                waiter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await waiter

    async def stop(self) -> None:
        process = self._process
        if process is None:
            return
        self._stop_requested = True
        if process.returncode is not None:
            await self._finalize_stderr_task()
            return
        await self._request_graceful_stop()
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
        await self._finalize_stderr_task()

    async def _finalize_stderr_task(self) -> None:
        if self._stderr_task is not None:
            await self._stderr_task
            self._stderr_task = None

    async def _consume_stderr(self) -> None:
        process = self._require_process()
        assert process.stderr is not None
        while True:
            line = await process.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").strip()
            level_name, message = self._parse_ffmpeg_stderr_line(text)
            if "silence_start:" in message:
                self._silence_started_at = time.monotonic()
                continue
            if "silence_end:" in message:
                self._silence_started_at = None
                continue
            if message and self._should_log_ffmpeg_line(level_name):
                self._log_ffmpeg_line(level_name, message)

    def _build_record_command(self, output_path: Path) -> list[str]:
        return [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-y",
            "-loglevel",
            "repeat+level+info",
            "-thread_queue_size",
            "1024",
            *self._audio_input_args(),
            "-vn",
            "-af",
            (
                "silencedetect="
                f"noise={self.settings.silence_noise_spec}:"
                f"d={self.settings.silence_min_detect_seconds}"
            ),
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(output_path),
        ]

    def _audio_input_args(self) -> list[str]:
        return [
            "-f",
            self.settings.audio_backend,
            "-i",
            self.audio_source,
        ]

    def _parse_ffmpeg_stderr_line(self, text: str) -> tuple[str, str]:
        match = FFMPEG_STDERR_LEVEL_RE.match(text)
        if match is None:
            return "info", text
        return match.group("level"), match.group("message").strip()

    def _should_log_ffmpeg_line(self, level_name: str) -> bool:
        threshold = FFMPEG_LOGLEVEL_PRIORITY[self.settings.ffmpeg_loglevel]
        current = FFMPEG_LOGLEVEL_PRIORITY.get(level_name, FFMPEG_LOGLEVEL_PRIORITY["info"])
        return current <= threshold

    def _log_ffmpeg_line(self, level_name: str, message: str) -> None:
        if level_name in {"panic", "fatal", "error"}:
            self.logger.error("ffmpeg %s", message)
            return
        if level_name == "warning":
            self.logger.warning("ffmpeg %s", message)
            return
        self.logger.info("ffmpeg %s", message)

    def _require_process(self) -> asyncio.subprocess.Process:
        if self._process is None:
            raise RecordingError("ffmpeg recorder is not running")
        return self._process

    def _is_expected_stop_return_code(self, return_code: int) -> bool:
        if not self._stop_requested:
            return False
        return return_code in {
            0,
            255,
            -signal.SIGINT,
            -signal.SIGTERM,
        }

    async def _request_graceful_stop(self) -> None:
        process = self._require_process()
        stdin = process.stdin
        if stdin is None:
            process.terminate()
            return
        try:
            stdin.write(b"q\n")
            await stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            process.terminate()
            return
        try:
            stdin.close()
        except Exception:  # noqa: BLE001
            pass


async def run_preflight_capture(settings: Settings, audio_source: str) -> None:
    logger = logging.getLogger("telemost_recorder.recording")
    if shutil.which("ffmpeg") is None:
        raise RecordingError("ffmpeg binary is not available in PATH")

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-thread_queue_size",
        "64",
        *[
            "-f",
            settings.audio_backend,
            "-i",
            audio_source,
        ],
        "-t",
        "1",
        "-f",
        "null",
        "-",
    ]
    logger.info("preflight_capture_started")
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        message = stderr.decode("utf-8", errors="replace").strip()
        raise RecordingError(f"ffmpeg preflight failed: {message}")
    logger.info("preflight_capture_ok")


async def probe_recording_duration_seconds(output_path: Path) -> float:
    if shutil.which("ffprobe") is None:
        raise RecordingError("ffprobe binary is not available in PATH")

    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(output_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        message = stderr.decode("utf-8", errors="replace").strip()
        raise RecordingError(f"ffprobe failed for {output_path}: {message or 'unknown error'}")

    duration_raw = stdout.decode("utf-8", errors="replace").strip()
    try:
        return float(duration_raw)
    except ValueError as exc:
        raise RecordingError(
            f"ffprobe returned unexpected duration for {output_path}: {duration_raw!r}"
        ) from exc
