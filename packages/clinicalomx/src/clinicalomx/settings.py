"""``YamlSettings``: a pydantic-settings base that layers config sources.

Precedence, highest first: init kwargs, env vars, ``.env``, YAML file. The env prefix defaults to
``DATASPEC_``; a subclass sets its own (such as ``CROI_``).
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import (
    BaseSettings,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)


class YamlSettings(BaseSettings):
    model_config = SettingsConfigDict(
        extra="forbid",
        env_nested_delimiter="__",  # X__param overrides a nested field
        env_prefix="DATASPEC_",     # a subclass sets its own
        protected_namespaces=(),
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        """Source precedence: init kwargs, env, .env, YAML."""
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )

    @classmethod
    def from_yaml(cls, yaml_file: str | Path, **overrides):
        # Points the class-level yaml path at `yaml_file` for this load, then restores it. Not
        # thread-safe.
        prev = cls.model_config.get("yaml_file")
        cls.model_config["yaml_file"] = str(yaml_file)
        try:
            return cls(**overrides)
        finally:
            cls.model_config["yaml_file"] = prev
