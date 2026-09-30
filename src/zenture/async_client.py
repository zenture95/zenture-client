"""Asynchronous public client for the zenture SDK."""

from __future__ import annotations

from typing import TYPE_CHECKING

from zenture._resources import (
    AsyncChatResource,
    AsyncEvaluationsResource,
    AsyncHelloworldResource,
    AsyncInputWizardResource,
    AsyncLimitsResource,
    AsyncModelsResource,
    AsyncOperationsResource,
    AsyncRunsResource,
    AsyncUsageResource,
    AsyncWalletResource,
)
from zenture._transport import AsyncTransport
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


class AsyncZentureClient:
    """Asynchronous client for the zenture Public API."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        http_client: httpx.AsyncClient | None = None,
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
        self._transport = AsyncTransport(
            config=self._config,
            client=http_client,
            user_agent=user_agent,
        )
        self.operations = AsyncOperationsResource(self._transport)
        self.runs = AsyncRunsResource(self._transport)
        self.chat = AsyncChatResource(self._transport)
        self.input_wizard = AsyncInputWizardResource(self._transport)
        self.evaluations = AsyncEvaluationsResource(self._transport)
        self.wallet = AsyncWalletResource(self._transport)
        self.usage = AsyncUsageResource(self._transport)
        self.limits = AsyncLimitsResource(self._transport)
        self.models = AsyncModelsResource(self._transport)
        self._helloworld = AsyncHelloworldResource(self._transport)

    @classmethod
    def from_env(
        cls,
        *,
        http_client: httpx.AsyncClient | None = None,
        user_agent: str | None = None,
        connect_timeout: float | None = None,
        read_timeout: float | None = None,
        write_timeout: float | None = None,
        pool_timeout: float | None = None,
        max_retries: int | None = None,
        initial_retry_backoff: float | None = None,
        max_retry_backoff: float | None = None,
    ) -> AsyncZentureClient:
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

    async def helloworld(self) -> str:
        """Return public quickstart markdown."""

        return await self._helloworld.get()

    async def aclose(self) -> None:
        """Close SDK-owned transport resources."""

        await self._transport.aclose()

    async def __aenter__(self) -> AsyncZentureClient:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()

    def __repr__(self) -> str:
        return f"AsyncZentureClient(api_key='{REDACTED}', base_url={self._config.api_base_url!r})"
