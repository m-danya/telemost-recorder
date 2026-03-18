from __future__ import annotations

import asyncio
import logging
import os
from urllib.parse import urlparse

from playwright.async_api import (
    BrowserContext,
    Error as PlaywrightError,
    Locator,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from telemost_recorder.config import Settings
from telemost_recorder.display import VirtualDisplaySession


class BrowserAutomationError(RuntimeError):
    pass


class TelemostBrowserSession:
    def __init__(self, settings: Settings, browser_env: dict[str, str] | None = None) -> None:
        self.settings = settings
        self.logger = logging.getLogger("telemost_recorder.browser")
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._display = VirtualDisplaySession(settings)
        self._browser_env = browser_env or os.environ.copy()

    async def start(self) -> None:
        self.settings.chromium_profile_dir_resolved.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        browser_env = await self._display.prepare_env(self._browser_env)
        executable_path = self._resolve_chromium_executable_path()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.settings.chromium_profile_dir_resolved),
            executable_path=executable_path,
            headless=False,
            viewport=None,
            handle_sigint=False,
            handle_sigterm=False,
            handle_sighup=False,
            timeout=self.settings.browser_launch_timeout_seconds * 1000,
            env=browser_env,
            args=self._build_browser_args(),
        )
        self._context.set_default_timeout(self.settings.join_timeout_seconds * 1000)
        origin = _origin_from_url(self.settings.url)
        await self._context.grant_permissions(["camera", "microphone"], origin=origin)
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        self.logger.info(
            "browser_started headless=false display=%s window=%sx%s executable=%s",
            browser_env.get("DISPLAY", "<none>"),
            self.settings.window_width,
            self.settings.window_height,
            executable_path,
        )

    async def join_meeting(self) -> None:
        page = self.page
        self.logger.info("join_started url=%s", self.settings.url)
        await page.goto(self.settings.url, wait_until="domcontentloaded")
        await self._wait_for_page_settle()
        await self._dismiss_understood_banners()
        await self._fill_display_name()
        await self._ensure_media_disabled_before_join()
        await self._click_connect()
        await self._wait_for_join_confirmation()
        self.logger.info("join_succeeded")

    async def close(self) -> None:
        try:
            if self._context is not None:
                await self._context.close()
                self._context = None
            if self._playwright is not None:
                await self._playwright.stop()
                self._playwright = None
        finally:
            await self._display.close()
            self._page = None

    @property
    def page(self) -> Page:
        if self._page is None:
            raise BrowserAutomationError("browser session is not started")
        return self._page

    def _build_browser_args(self) -> list[str]:
        return [
            f"--window-size={self.settings.window_width},{self.settings.window_height}",
            "--ozone-platform=x11",
            "--ozone-platform-hint=x11",
            "--disable-gpu",
            "--disable-notifications",
            "--autoplay-policy=no-user-gesture-required",
            "--disable-default-apps",
            "--disable-features=Translate,MediaRouter",
            "--disable-infobars",
            "--no-first-run",
            "--password-store=basic",
            "--use-fake-ui-for-media-stream",
        ]

    def _resolve_chromium_executable_path(self) -> str:
        configured_path = self.settings.chromium_path.expanduser().resolve()
        if configured_path.suffix != ".sh":
            return str(configured_path)
        try:
            with configured_path.open("rb") as file:
                if file.read(2) != b"#!":
                    return str(configured_path)
        except OSError:
            return str(configured_path)

        direct_binary = configured_path.with_suffix("")
        if direct_binary.is_file() and os.access(direct_binary, os.X_OK):
            self.logger.info(
                "chromium_wrapper_resolved wrapper=%s executable=%s",
                configured_path,
                direct_binary,
            )
            return str(direct_binary)
        return str(configured_path)

    async def _wait_for_page_settle(self) -> None:
        page = self.page
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            self.logger.info("page_networkidle_timeout")

    async def _dismiss_understood_banners(self) -> None:
        page = self.page
        remaining = 2
        while remaining > 0:
            button = page.get_by_text("Понятно", exact=True).first
            try:
                if not await button.is_visible(timeout=2_000):
                    return
                await button.click()
                remaining -= 1
                self.logger.info("dismissed_banner")
                await asyncio.sleep(0.5)
            except PlaywrightTimeoutError:
                return
            except PlaywrightError:
                return

    async def _fill_display_name(self) -> None:
        field = await self._locate_name_input()
        try:
            await field.click()
            await field.fill(self.settings.display_name)
        except PlaywrightError as exc:
            raise BrowserAutomationError("failed to set display name") from exc

    async def _click_connect(self) -> None:
        page = self.page
        try:
            await page.get_by_role("button", name="Подключиться").click()
        except PlaywrightError as exc:
            raise BrowserAutomationError("failed to click connect button") from exc

    async def _ensure_media_disabled_before_join(self) -> None:
        await self._ensure_toggle_is_off(
            turn_off_test_id="turn-off-mic-button",
            turn_on_test_id="turn-on-mic-button",
            off_titles=("Включить микрофон",),
            on_titles=("Выключить микрофон",),
            label="microphone",
        )
        await self._ensure_toggle_is_off(
            turn_off_test_id="turn-off-camera-button",
            turn_on_test_id="turn-on-camera-button",
            off_titles=("Включить камеру",),
            on_titles=("Выключить камеру",),
            label="camera",
        )

    async def _ensure_toggle_is_off(
        self,
        *,
        turn_off_test_id: str,
        turn_on_test_id: str,
        off_titles: tuple[str, ...],
        on_titles: tuple[str, ...],
        label: str,
    ) -> None:
        page = self.page
        on_locator = await self._first_visible_locator(
            [
                page.locator(f'[data-testid="{turn_off_test_id}"]').first,
                *[page.locator(f'button[title="{title}"]').first for title in on_titles],
                *[page.locator(f'button[aria-label="{title}"]').first for title in on_titles],
            ]
        )
        if on_locator is not None:
            try:
                await on_locator.click()
                self.logger.info("%s_disabled", label)
                await asyncio.sleep(0.5)
                return
            except PlaywrightError as exc:
                raise BrowserAutomationError(f"failed to disable {label}") from exc

        off_locator = await self._first_visible_locator(
            [
                page.locator(f'[data-testid="{turn_on_test_id}"]').first,
                *[page.locator(f'button[title="{title}"]').first for title in off_titles],
                *[page.locator(f'button[aria-label="{title}"]').first for title in off_titles],
            ]
        )
        if off_locator is not None:
            self.logger.info("%s_already_disabled", label)
            return

        raise BrowserAutomationError(f"{label} toggle was not found on the prejoin screen")

    async def _wait_for_join_confirmation(self) -> None:
        page = self.page
        deadline = asyncio.get_running_loop().time() + self.settings.join_timeout_seconds
        while True:
            if await self._looks_like_connected():
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise BrowserAutomationError("meeting join confirmation timed out")
            await asyncio.sleep(1)
            await self._dismiss_understood_banners()

    async def _looks_like_connected(self) -> bool:
        page = self.page
        try:
            connect_button = page.get_by_role("button", name="Подключиться").first
            if await connect_button.count() > 0 and await connect_button.is_visible():
                return False
        except PlaywrightError:
            pass

        meeting_signals = [
            page.locator("video"),
            page.get_by_text("Покинуть", exact=False),
            page.get_by_text("Выйти", exact=False),
            page.get_by_text("Завершить", exact=False),
        ]
        for locator in meeting_signals:
            try:
                if await locator.count() > 0 and await locator.first.is_visible(timeout=1_000):
                    return True
            except PlaywrightError:
                continue

        try:
            textbox = await self._locate_name_input(raise_on_missing=False)
            if textbox is None:
                return True
            return not await textbox.is_visible(timeout=1_000)
        except PlaywrightError:
            return True

    async def _locate_name_input(self, raise_on_missing: bool = True):
        page = self.page
        candidates = [
            page.locator('input[data-testid="orb-textinput-input"]').first,
            page.get_by_role("textbox").first,
            page.locator('input[type="text"]').first,
        ]
        for locator in candidates:
            try:
                if await locator.count() == 0:
                    continue
                if await locator.is_visible(timeout=2_000):
                    return locator
            except PlaywrightError:
                continue
        if not raise_on_missing:
            return None
        raise BrowserAutomationError("display name input was not found")

    async def _first_visible_locator(self, candidates: list[Locator]) -> Locator | None:
        for locator in candidates:
            try:
                if await locator.count() == 0:
                    continue
            except PlaywrightError:
                continue
            try:
                if await locator.is_visible(timeout=1_000):
                    return locator
            except PlaywrightError:
                continue
        return None


def _origin_from_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"
