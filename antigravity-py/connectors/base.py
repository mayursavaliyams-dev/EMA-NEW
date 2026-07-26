"""Abstract market connector — swap brokers (Upstox live; Dhan/Kotak future)."""
from abc import ABC, abstractmethod


class MarketConnector(ABC):
    name = "base"

    @abstractmethod
    def ltp(self, instrument_keys: list[str]) -> dict: ...

    @abstractmethod
    def expiries(self, underlying_key: str) -> list[str]: ...

    @abstractmethod
    def option_chain(self, underlying_key: str, expiry: str) -> list[dict]: ...

    @abstractmethod
    def place_order(self, instrument_key: str, side: str, qty: int, **kw) -> dict:
        """In PAPER mode this MUST NOT hit the broker — return a simulated ack."""
        ...
