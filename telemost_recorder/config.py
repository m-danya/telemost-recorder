from __future__ import annotations

from datetime import time
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _parse_clock(value: str) -> time:
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"invalid time value: {value!r}")
    hour, minute = (int(part) for part in parts)
    return time(hour=hour, minute=minute)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TELEMOST_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    url: str
    display_name: str
    schedule: str
    recordings_dir: Path
    audio_backend: Literal["pulse"]
    audio_sink_name: str = Field(min_length=1)
    window_size: str
    window_position: str
    silence_timeout_seconds: int
    silence_min_detect_seconds: int
    silence_noise_db: float
    chromium_path: Path
    chromium_profile_dir: Path
    browser_launch_timeout_seconds: int
    join_timeout_seconds: int
    post_join_delay_seconds: int
    ffmpeg_loglevel: Literal["error", "warning", "info"]

    @field_validator("audio_sink_name")
    @classmethod
    def validate_audio_sink_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("audio sink name cannot be empty")
        return cleaned

    @property
    def schedule_times(self) -> tuple[time, ...]:
        return tuple(_parse_clock(raw_value) for raw_value in self.schedule.split(",") if raw_value.strip())

    @property
    def window_width(self) -> int:
        width, _ = self._parse_window_size()
        return width

    @property
    def window_height(self) -> int:
        _, height = self._parse_window_size()
        return height

    @property
    def window_x(self) -> int:
        x, _ = self._parse_window_position()
        return x

    @property
    def window_y(self) -> int:
        _, y = self._parse_window_position()
        return y

    @property
    def recordings_dir_resolved(self) -> Path:
        return self.recordings_dir.expanduser().resolve()

    @property
    def chromium_profile_dir_resolved(self) -> Path:
        return self.chromium_profile_dir.expanduser().resolve()

    @property
    def silence_noise_spec(self) -> str:
        return f"{self.silence_noise_db}dB"

    def _parse_window_size(self) -> tuple[int, int]:
        parts = self.window_size.lower().split("x")
        if len(parts) != 2:
            raise ValueError(f"invalid window size: {self.window_size!r}")
        width, height = (int(part) for part in parts)
        if width <= 0 or height <= 0:
            raise ValueError("window size must be positive")
        return width, height

    def _parse_window_position(self) -> tuple[int, int]:
        parts = self.window_position.split(",")
        if len(parts) != 2:
            raise ValueError(f"invalid window position: {self.window_position!r}")
        return int(parts[0]), int(parts[1])
