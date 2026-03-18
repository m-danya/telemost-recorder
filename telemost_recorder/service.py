from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
from datetime import datetime
from pathlib import Path
from types import TracebackType

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from telemost_recorder.browser import TelemostBrowserSession
from telemost_recorder.config import Settings
from telemost_recorder.pulse_audio import ChromiumAudioSink
from telemost_recorder.recording import (
    FfmpegRecorder,
    RecordingError,
    run_preflight_capture,
)
from telemost_recorder.session_lock import SessionFileLock


class TelemostService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger = logging.getLogger("telemost_recorder.service")
        self._recording_lock = asyncio.Lock()
        self._shutdown_event = asyncio.Event()
        self._active_session_done = asyncio.Event()
        self._active_session_done.set()
        self._signal_handlers_installed = False
        self._manual_trigger_enabled = False
        self._manual_trigger_pending = False
        self._session_requested = False
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def run(self) -> None:
        self._install_signal_handlers()
        await self.check_environment()
        scheduler = AsyncIOScheduler()
        for schedule_time in self.settings.schedule_times:
            scheduler.add_job(
                self._run_job_if_idle,
                trigger=CronTrigger(
                    hour=schedule_time.hour,
                    minute=schedule_time.minute,
                ),
                id=f"telemost-{schedule_time.hour:02d}-{schedule_time.minute:02d}",
                replace_existing=True,
            )
        scheduler.start()
        self._manual_trigger_enabled = True
        self.logger.info("scheduler_started schedule=%s", self.settings.schedule)
        self._replay_pending_manual_trigger()
        try:
            await self._shutdown_event.wait()
            self.logger.info("shutdown_requested")
        finally:
            self._manual_trigger_enabled = False
            self._manual_trigger_pending = False
            scheduler.shutdown(wait=False)
            await self._wait_for_active_session_to_finish()
            await self._wait_for_background_tasks()

    async def run_once(self) -> None:
        self._install_signal_handlers()
        await self.check_environment()
        await self._request_session(trigger="once")

    async def check_environment(self) -> None:
        self._validate_binaries()
        self.settings.recordings_dir_resolved.mkdir(parents=True, exist_ok=True)
        self.settings.chromium_profile_dir_resolved.mkdir(parents=True, exist_ok=True)
        audio_sink = ChromiumAudioSink(self.settings)
        browser = TelemostBrowserSession(self.settings, browser_env=audio_sink.browser_env)
        try:
            await audio_sink.start()
            await run_preflight_capture(self.settings, audio_sink.monitor_source)
            await browser.start()
        finally:
            await browser.close()
            await audio_sink.close()
        self.logger.info("preflight_ok")

    async def _run_job_if_idle(self) -> None:
        await self._request_session(trigger="scheduled")

    async def _request_session(self, *, trigger: str) -> None:
        if self._shutdown_event.is_set():
            self.logger.info("session_skipped reason=shutdown_requested trigger=%s", trigger)
            return
        if self._session_requested:
            self.logger.warning("session_skipped reason=active_session trigger=%s", trigger)
            return

        self._session_requested = True
        try:
            await self._run_single_session(trigger=trigger)
        finally:
            self._session_requested = False

    async def _run_single_session(self, *, trigger: str) -> None:
        async with self._recording_lock:
            self._active_session_done.clear()
            session_lock = SessionFileLock(self.settings.session_lock_path)
            audio_sink = ChromiumAudioSink(self.settings)
            browser = TelemostBrowserSession(self.settings, browser_env=audio_sink.browser_env)
            recorder: FfmpegRecorder | None = None
            stop_reason = "failed"
            try:
                if not session_lock.acquire(trigger=trigger):
                    self.logger.warning(
                        "session_skipped reason=lock_held trigger=%s lock=%s",
                        trigger,
                        self.settings.session_lock_path,
                    )
                    return
                if self._shutdown_event.is_set():
                    self.logger.info("session_skipped reason=shutdown_requested trigger=%s", trigger)
                    return
                output_path = self._build_output_path()
                self.logger.info("session_started trigger=%s output=%s", trigger, output_path)
                await audio_sink.start()
                await browser.start()
                if self._shutdown_event.is_set():
                    self.logger.info(
                        "session_cancelled reason=shutdown_requested trigger=%s stage=browser_started",
                        trigger,
                    )
                    return
                await browser.join_meeting()
                if self._shutdown_event.is_set():
                    self.logger.info(
                        "session_cancelled reason=shutdown_requested trigger=%s stage=joined",
                        trigger,
                    )
                    return
                recorder = FfmpegRecorder(self.settings, audio_sink.monitor_source)
                await recorder.start(output_path)
                stop_reason = await self._wait_for_recording_or_shutdown(recorder)
                self.logger.info(
                    "recording_stopped reason=%s trigger=%s output=%s",
                    stop_reason,
                    trigger,
                    output_path,
                )
            finally:
                try:
                    if recorder is not None:
                        await recorder.stop()
                    await browser.close()
                    await audio_sink.close()
                finally:
                    try:
                        session_lock.release()
                    finally:
                        self._active_session_done.set()

    def _build_output_path(self) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return self.settings.recordings_dir_resolved / f"telemost-{timestamp}.m4a"

    def _install_signal_handlers(self) -> None:
        if self._signal_handlers_installed:
            return
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._shutdown_event.set)
            except NotImplementedError:
                return
        try:
            loop.add_signal_handler(signal.SIGUSR1, self._handle_manual_trigger_signal)
        except (AttributeError, NotImplementedError):
            pass
        self._signal_handlers_installed = True

    def _handle_manual_trigger_signal(self) -> None:
        if self._shutdown_event.is_set():
            self.logger.info("manual_trigger_skipped reason=shutdown_requested")
            return
        if not self._manual_trigger_enabled:
            self._manual_trigger_pending = True
            self.logger.info("manual_trigger_queued reason=service_not_ready")
            return
        self._dispatch_manual_trigger(source="signal")

    def _replay_pending_manual_trigger(self) -> None:
        if not self._manual_trigger_pending:
            return
        if not self._manual_trigger_enabled or self._shutdown_event.is_set():
            return
        self._manual_trigger_pending = False
        self._dispatch_manual_trigger(source="queued")

    def _dispatch_manual_trigger(self, *, source: str) -> None:
        self.logger.info("manual_trigger_received source=%s", source)
        task = asyncio.create_task(self._request_session(trigger="manual"))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        task.add_done_callback(self._log_background_task_failure)

    def _validate_binaries(self) -> None:
        _ = self.settings.schedule_times
        _ = self.settings.window_width
        _ = self.settings.window_height
        if not self.settings.chromium_path.is_file():
            raise FileNotFoundError(f"chromium not found: {self.settings.chromium_path}")
        if not os.access(self.settings.chromium_path, os.X_OK):
            raise PermissionError(f"chromium is not executable: {self.settings.chromium_path}")
        if "DISPLAY" not in os.environ and shutil.which("Xvfb") is None:
            raise FileNotFoundError(
                "DISPLAY is not set and Xvfb binary is not available in PATH; "
                "install Xvfb for headless console usage"
            )
        if shutil.which("ffmpeg") is None:
            raise FileNotFoundError("ffmpeg binary is not available in PATH")
        if shutil.which("pactl") is None:
            raise FileNotFoundError("pactl binary is not available in PATH")

    async def _wait_for_active_session_to_finish(self) -> None:
        if self._active_session_done.is_set():
            return
        self.logger.info("waiting_for_active_session_to_finish")
        await self._active_session_done.wait()

    async def _wait_for_background_tasks(self) -> None:
        if not self._background_tasks:
            return
        await asyncio.gather(*list(self._background_tasks), return_exceptions=True)

    def _log_background_task_failure(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exception = task.exception()
        if exception is None:
            return
        exc_info: tuple[type[BaseException], BaseException, TracebackType | None] = (
            type(exception),
            exception,
            exception.__traceback__,
        )
        self.logger.error("background_task_failed", exc_info=exc_info)

    async def _wait_for_recording_or_shutdown(self, recorder: FfmpegRecorder) -> str:
        recording_task = asyncio.create_task(recorder.wait_until_stop_condition())
        shutdown_task = asyncio.create_task(self._shutdown_event.wait())
        try:
            done, pending = await asyncio.wait(
                {recording_task, shutdown_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if shutdown_task in done:
                self.logger.info("shutdown_requested stopping_active_recording")
                await recorder.stop()
                try:
                    await recording_task
                except RecordingError:
                    raise
                return "shutdown"
            return await recording_task
        finally:
            for task in (recording_task, shutdown_task):
                if task.done():
                    continue
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
