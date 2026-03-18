from __future__ import annotations

import asyncio
import logging
import os
import shutil

from telemost_recorder.config import Settings


class DisplayServerError(RuntimeError):
    pass


class VirtualDisplaySession:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger = logging.getLogger("telemost_recorder.display")
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._display: str | None = None

    async def prepare_env(self, browser_env: dict[str, str]) -> dict[str, str]:
        if browser_env.get("DISPLAY"):
            return self._prepare_x11_env(browser_env, display=browser_env["DISPLAY"])
        if shutil.which("Xvfb") is None:
            raise DisplayServerError(
                "DISPLAY is not set and Xvfb is not available in PATH; "
                "install Xvfb or start the service inside a graphical session"
            )

        self._process = await asyncio.create_subprocess_exec(
            "Xvfb",
            "-displayfd",
            "1",
            "-screen",
            "0",
            f"{self.settings.window_width}x{self.settings.window_height}x24",
            "-nolisten",
            "tcp",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
            start_new_session=True,
        )
        assert self._process.stdout is not None
        try:
            display_number_raw = await asyncio.wait_for(self._process.stdout.readline(), timeout=10)
        except asyncio.TimeoutError as exc:
            raise DisplayServerError("Xvfb did not report a display number in time") from exc

        display_number = display_number_raw.decode("utf-8", errors="replace").strip()
        if not display_number:
            stderr_output = await self._read_stderr()
            raise DisplayServerError(f"Xvfb failed to start: {stderr_output or 'unknown error'}")

        self._display = f":{display_number}"
        self._stderr_task = asyncio.create_task(self._consume_stderr())
        prepared_env = self._prepare_x11_env(browser_env, display=self._display)
        self.logger.info(
            "virtual_display_started display=%s screen=%sx%s",
            self._display,
            self.settings.window_width,
            self.settings.window_height,
        )
        return prepared_env

    async def close(self) -> None:
        if self._process is None:
            return
        if self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
        if self._stderr_task is not None:
            await self._stderr_task
            self._stderr_task = None
        self._process = None
        self._display = None

    async def _consume_stderr(self) -> None:
        if self._process is None or self._process.stderr is None:
            return
        while True:
            line = await self._process.stderr.readline()
            if not line:
                return
            message = line.decode("utf-8", errors="replace").strip()
            if message:
                self.logger.info("xvfb %s", message)

    async def _read_stderr(self) -> str:
        if self._process is None or self._process.stderr is None:
            return ""
        stderr = await self._process.stderr.read()
        return stderr.decode("utf-8", errors="replace").strip()

    def _prepare_x11_env(self, browser_env: dict[str, str], *, display: str) -> dict[str, str]:
        prepared_env = browser_env.copy()
        prepared_env["DISPLAY"] = display
        prepared_env["XDG_SESSION_TYPE"] = "x11"
        prepared_env["GDK_BACKEND"] = "x11"
        prepared_env["OZONE_PLATFORM"] = "x11"
        prepared_env["OZONE_PLATFORM_HINT"] = "x11"
        prepared_env["QT_QPA_PLATFORM"] = "xcb"
        prepared_env["SDL_VIDEODRIVER"] = "x11"
        prepared_env["CLUTTER_BACKEND"] = "x11"
        prepared_env.pop("WAYLAND_DISPLAY", None)
        prepared_env.pop("WAYLAND_SOCKET", None)
        return prepared_env
