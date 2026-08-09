from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import yaml
from dotenv import load_dotenv

from .schema import Settings, SymbolsFile


class ConfigError(RuntimeError):
    """
    Raised when configuration files or required runtime environment
    variables are invalid.
    """


def load_settings(
    settings_path: str | Path = "config/settings.yaml",
    symbols_path: str | Path = "config/symbols.yaml",
) -> Settings:
    """
    Load and validate application configuration.

    Settings are loaded from settings.yaml.
    Symbols are loaded from symbols.yaml.
    Secrets are loaded from environment variables via .env.
    """

    load_dotenv()

    settings_raw = _read_yaml_file(settings_path)
    symbols_raw = _read_yaml_file(symbols_path)

    if not isinstance(settings_raw, dict):
        raise ConfigError(
            f"settings file must contain a YAML mapping: {settings_path}"
        )

    if not isinstance(symbols_raw, dict):
        raise ConfigError(
            f"symbols file must contain a YAML mapping: {symbols_path}"
        )

    if "symbols" in settings_raw:
        raise ConfigError(
            "symbols must be defined only in symbols.yaml, "
            "not in settings.yaml"
        )

    try:
        symbols_file = SymbolsFile.model_validate(symbols_raw)
    except Exception as exc:
        raise ConfigError(f"invalid symbols file: {symbols_path}: {exc}") from exc

    settings_raw["symbols"] = symbols_file.symbols

    try:
        settings = Settings.model_validate(settings_raw)
    except Exception as exc:
        raise ConfigError(f"invalid settings file: {settings_path}: {exc}") from exc

    _validate_runtime_secrets(settings)

    return settings


def _read_yaml_file(path: str | Path) -> Dict[str, Any]:
    """
    Read YAML file and return parsed content.
    """

    file_path = Path(path).expanduser()

    if not file_path.exists():
        raise ConfigError(f"config file not found: {file_path}")

    try:
        with file_path.open("r", encoding="utf-8") as file:
            content = yaml.safe_load(file)
            return content or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"failed to parse YAML file: {file_path}: {exc}") from exc


def _validate_runtime_secrets(settings: Settings) -> None:
    """
    Validate that required secret environment variables exist.

    Secrets themselves are not stored in settings.yaml.
    Only environment variable names are stored there.
    """

    if not settings.telegram.enabled:
        return

    missing = []

    token_env = settings.telegram.token_env
    chat_id_env = settings.telegram.chat_id_env

    token = os.getenv(token_env, "").strip()
    chat_id = os.getenv(chat_id_env, "").strip()

    if not token:
        missing.append(token_env)

    if not chat_id:
        missing.append(chat_id_env)

    if missing:
        raise ConfigError(
            "telegram is enabled but required environment variables are missing: "
            + ", ".join(missing)
        )
