"""Synchronous public client for the zenture SDK."""

from __future__ import annotations

from typing import TYPE_CHECKING

from zenture._resources import (
    ChatResource,
    EvaluationsResource,
    HelloworldResource,
    InputWizardResource,
    LimitsResource,
    ModelsResource,
    OperationsResource,
    RunsResource,
    UsageResource,
    WalletResource,
)
from zenture._transport import SyncTransport
from zenture.config import (
    DEFAULT_BASE_URL,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_INITIAL_RETRY_BACKOFF,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_RETRY_BACKOFF,
    DEFAULT_POOL_TIMEOUT,
    DEFAULT_READ_TIMEOUT,
    DEFAULT_WRITE_TIMEOUT,
    ZentureConfig,
)
from zenture.redaction import REDACTED

if TYPE_CHECKING:
    import httpx


class ZentureClient:
    """Synchronous client for the zenture Public API."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        http_client: httpx.Client | None = None,
        user_agent: str | None = None,
        connect_timeout: float | None = None,
        read_timeout: float | None = None,
        write_timeout: float | None = None,
        pool_timeout: float | None = None,
        max_retries: int | None = None,
        initial_retry_backoff: float | None = None,
        max_retry_backoff: float | None = None,
    ) -> None:
        self._config = ZentureConfig(
            api_key=api_key,
            base_url=base_url if base_url is not None else DEFAULT_BASE_URL,
            connect_timeout=connect_timeout
            if connect_timeout is not None
            else DEFAULT_CONNECT_TIMEOUT,
            read_timeout=read_timeout if read_timeout is not None else DEFAULT_READ_TIMEOUT,
            write_timeout=write_timeout if write_timeout is not None else DEFAULT_WRITE_TIMEOUT,
            pool_timeout=pool_timeout if pool_timeout is not None else DEFAULT_POOL_TIMEOUT,
            max_retries=max_retries if max_retries is not None else DEFAULT_MAX_RETRIES,
            initial_retry_backoff=initial_retry_backoff
            if initial_retry_backoff is not None
            else DEFAULT_INITIAL_RETRY_BACKOFF,
            max_retry_backoff=max_retry_backoff
            if max_retry_backoff is not None
            else DEFAULT_MAX_RETRY_BACKOFF,
        )
        self._transport = SyncTransport(
            config=self._config,
            client=http_client,
            user_agent=user_agent,
        )
        self.operations = OperationsResource(self._transport)
        self.runs = RunsResource(self._transport)
        self.chat = ChatResource(self._transport)
        self.input_wizard = InputWizardResource(self._transport)
        self.evaluations = EvaluationsResource(self._transport)
        self.wallet = WalletResource(self._transport)
        self.usage = UsageResource(self._transport)
        self.limits = LimitsResource(self._transport)
        self.models = ModelsResource(self._transport)
        self._helloworld = HelloworldResource(self._transport)

    @classmethod
    def from_env(
        cls,
        *,
        http_client: httpx.Client | None = None,
        user_agent: str | None = None,
        connect_timeout: float | None = None,
        read_timeout: float | None = None,
        write_timeout: float | None = None,
        pool_timeout: float | None = None,
        max_retries: int | None = None,
        initial_retry_backoff: float | None = None,
        max_retry_backoff: float | None = None,
    ) -> ZentureClient:
        """Create a client from supported environment variables."""

        config = ZentureConfig.from_env()
        return cls(
            api_key=config.api_key_value,
            base_url=config.base_url,
            http_client=http_client,
            user_agent=user_agent,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            write_timeout=write_timeout,
            pool_timeout=pool_timeout,
            max_retries=max_retries,
            initial_retry_backoff=initial_retry_backoff,
            max_retry_backoff=max_retry_backoff,
        )

    @property
    def is_closed(self) -> bool:
        """Return whether the underlying HTTP client is closed."""

        return self._transport.is_closed

    def helloworld(self) -> str:
        """Return public quickstart markdown."""

        return self._helloworld.get()

    def close(self) -> None:
        """Close SDK-owned transport resources."""

        self._transport.close()

    def __enter__(self) -> ZentureClient:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"ZentureClient(api_key='{REDACTED}', base_url={self._config.api_base_url!r})"
