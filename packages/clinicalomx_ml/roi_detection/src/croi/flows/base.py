"""Base flow or job for high-level operations."""
import abc
import typing as T

from pydantic import BaseModel

from croi.io.services import PrefectLoggerService, Service


class Flow(abc.ABC, BaseModel, strict=True, frozen=True, extra="forbid"):
    """Base class for Prefect Flows."""

    KIND: str

    logger_service: T.ClassVar[type[Service]] = PrefectLoggerService
