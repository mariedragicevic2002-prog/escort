from __future__ import annotations

import threading

from app.config_governance.contracts import ConfigRegistryContract


class TypedConfigRegistry:
    def __init__(self) -> None:
        self._contracts: dict[str, ConfigRegistryContract] = {}
        self._lock = threading.Lock()

    def register(self, contract: ConfigRegistryContract, *, replace: bool = False) -> None:
        with self._lock:
            if not replace and contract.namespace in self._contracts:
                raise ValueError(f"contract already registered for namespace={contract.namespace}")
            self._contracts[contract.namespace] = contract

    def get(self, namespace: str) -> ConfigRegistryContract:
        with self._lock:
            if namespace not in self._contracts:
                raise KeyError(f"unknown config namespace={namespace}")
            return self._contracts[namespace]
