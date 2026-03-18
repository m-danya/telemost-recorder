from __future__ import annotations

import asyncio
import logging
import shutil
import signal
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from telemost_record.browser import TelemostBrowserSession
from telemost_record.config import Settings
from telemost_record.pulse_audio import ChromiumAudioSink
from telemost_record.recording import (
    FfmpegRecorder,
    RecordingError,
    run_preflight_capture,
)


class TelemostService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger = logging.getLogger("telemost_record.service")
        self._recording_lock = asyncio.Lock()
        self._shutdown_event = asyncio.Event()
        self._active_session_done = asyncio.Event()
        self._active_session_done.set()
        self._signal_handlers_installed = False

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
        self.logger.info("scheduler_started schedule=%s", self.settings.schedule)
        await self._shutdown_event.wait()
        self.logger.info("shutdown_requested")
        scheduler.shutdown(wait=False)
        await self._wait_for_active_session_to_finish()

    async def run_once(self) -> None:
        self._install_signal_handlers()
        await self.check_environment()
        await self._run_single_session()

    async def check_environment(self) -> None:
        self._validate_binaries()
        self.settings.recordings_dir_resolved.mkdir(parents=True, exist_ok=True)
        self.settings.chromium_profile_dir_resolved.mkdir(parents=True, exist_ok=True)
        audio_sink = ChromiumAudioSink(self.settings)
        try:
            await audio_sink.start()
            await run_preflight_capture(self.settings, audio_sink.monitor_source)
        finally:
            await audio_sink.close()
        self.logger.info("preflight_ok")

    async def _run_job_if_idle(self) -> None:
        if self._recording_lock.locked():
            self.logger.warning("job_skipped reason=active_session")
            return
        await self._run_single_session()

    async def _run_single_session(self) -> None:
        async with self._recording_lock:
            self._active_session_done.clear()
            output_path = self._build_output_path()
            audio_sink = ChromiumAudioSink(self.settings)
            browser = TelemostBrowserSession(self.settings, browser_env=audio_sink.browser_env)
            recorder: FfmpegRecorder | None = None
            stop_reason = "failed"
            try:
                if self._shutdown_event.is_set():
                    self.logger.info("session_skipped reason=shutdown_requested")
                    return
                await audio_sink.start()
                await browser.start()
                if self._shutdown_event.is_set():
                    self.logger.info("session_cancelled reason=shutdown_requested stage=browser_started")
                    return
                await browser.join_meeting()
                if self._shutdown_event.is_set():
                    self.logger.info("session_cancelled reason=shutdown_requested stage=joined")
                    return
                recorder = FfmpegRecorder(self.settings, audio_sink.monitor_source)
                await recorder.start(output_path)
                stop_reason = await self._wait_for_recording_or_shutdown(recorder)
                self.logger.info("recording_stopped reason=%s output=%s", stop_reason, output_path)
            finally:
                if recorder is not None:
                    await recorder.stop()
                await browser.close()
                await audio_sink.close()
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
        self._signal_handlers_installed = True

    def _validate_binaries(self) -> None:
        _ = self.settings.schedule_times
        _ = self.settings.window_width
        _ = self.settings.window_height
        _ = self.settings.window_x
        _ = self.settings.window_y
        if not self.settings.chromium_path.exists():
            raise FileNotFoundError(f"chromium not found: {self.settings.chromium_path}")
        if shutil.which("ffmpeg") is None:
            raise FileNotFoundError("ffmpeg binary is not available in PATH")
        if shutil.which("pactl") is None:
            raise FileNotFoundError("pactl binary is not available in PATH")

    async def _wait_for_active_session_to_finish(self) -> None:
        if self._active_session_done.is_set():
            return
        self.logger.info("waiting_for_active_session_to_finish")
        await self._active_session_done.wait()

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
