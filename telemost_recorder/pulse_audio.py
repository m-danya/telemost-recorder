from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
import uuid

from telemost_recorder.config import Settings


class PulseAudioError(RuntimeError):
    pass


class ChromiumAudioSink:
    _PACTL_RETRY_ATTEMPTS = 3
    _PACTL_RETRY_DELAY_SECONDS = 0.5

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger = logging.getLogger("telemost_recorder.pulse_audio")
        suffix = uuid.uuid4().hex[:8]
        self.sink_name = f"{self.settings.audio_sink_name}_{suffix}"
        self._module_id: int | None = None

    @property
    def monitor_source(self) -> str:
        return f"{self.sink_name}.monitor"

    @property
    def browser_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["PULSE_SINK"] = self.sink_name
        return env

    async def start(self) -> None:
        self._validate_requirements()
        self._module_id = await self._load_null_sink()
        await self._wait_for_monitor_source()
        self.logger.info("pulse_sink_ready sink=%s source=%s", self.sink_name, self.monitor_source)

    async def close(self) -> None:
        if self._module_id is None:
            return
        try:
            await self._run_pactl("unload-module", str(self._module_id))
        except PulseAudioError:
            self.logger.exception("pulse_sink_unload_failed sink=%s", self.sink_name)
        finally:
            self._module_id = None

    async def _load_null_sink(self) -> int:
        output = await self._run_pactl_with_retries(
            "load-module",
            "module-null-sink",
            f"sink_name={self.sink_name}",
            f"sink_properties=device.description={self.sink_name}",
        )
        try:
            return int(output.strip())
        except ValueError as exc:
            raise PulseAudioError(f"unexpected pactl module id: {output!r}") from exc

    async def _wait_for_monitor_source(self) -> None:
        deadline = asyncio.get_running_loop().time() + 5
        while True:
            sources = await self._run_pactl_with_retries("list", "short", "sources")
            for line in sources.splitlines():
                columns = line.split("\t")
                if len(columns) >= 2 and columns[1] == self.monitor_source:
                    return
            if asyncio.get_running_loop().time() >= deadline:
                raise PulseAudioError(
                    f"monitor source did not appear for sink {self.sink_name}: expected {self.monitor_source}"
                )
            await asyncio.sleep(0.2)

    async def _run_pactl(self, *args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "pactl",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            stdout_message = stdout.decode("utf-8", errors="replace").strip()
            stderr_message = stderr.decode("utf-8", errors="replace").strip()
            detail = stderr_message or stdout_message or "unknown error"
            command = " ".join(shlex.quote(arg) for arg in args)
            raise PulseAudioError(
                f"pactl {command} failed with exit code {process.returncode}: {detail}"
            )
        return stdout.decode("utf-8", errors="replace")

    async def _run_pactl_with_retries(self, *args: str) -> str:
        last_error: PulseAudioError | None = None
        for attempt in range(1, self._PACTL_RETRY_ATTEMPTS + 1):
            try:
                return await self._run_pactl(*args)
            except PulseAudioError as exc:
                last_error = exc
                if attempt >= self._PACTL_RETRY_ATTEMPTS:
                    break
                self.logger.warning(
                    "pactl_retry attempt=%s command=%s error=%s",
                    attempt,
                    " ".join(shlex.quote(arg) for arg in args),
                    exc,
                )
                await asyncio.sleep(self._PACTL_RETRY_DELAY_SECONDS * attempt)
        assert last_error is not None
        raise last_error

    def _validate_requirements(self) -> None:
        if self.settings.audio_backend != "pulse":
            raise PulseAudioError("dedicated Chromium audio capture requires TELEMOST_AUDIO_BACKEND=pulse")
        if shutil.which("pactl") is None:
            raise PulseAudioError("pactl is required for dedicated Chromium audio capture")
