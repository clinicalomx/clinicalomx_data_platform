import abc
from typing import Any

from prefect import get_run_logger
from pydantic import BaseModel


class Service(abc.ABC, BaseModel, strict=True, frozen=True, extra="forbid"):
    """Base for services that manage global contexts such as loggers and clients."""

    @abc.abstractmethod
    def start(self) -> None:
        """Start the service or connect to an existing one."""

    def stop(self) -> None:
        """Stop the service. Does nothing by default."""


class LoggerService(Service):
    """Basic python logging service."""

    def start(self) -> None:
        pass


class PrefectLoggerService(Service):
    """Existing logger within a Prefect task or flow."""

    def start(self) -> None:
        """Prefect starts the run logger itself."""

    def logger(self) -> Any:
        return get_run_logger()
