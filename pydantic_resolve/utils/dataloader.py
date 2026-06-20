from collections import defaultdict
from typing import Callable, DefaultDict, Sequence, TypeVar, Any, Generic
from dataclasses import dataclass, field
from time import time
from aiodataloader import DataLoader
import asyncio

T = TypeVar("T")
V = TypeVar("V")
K = TypeVar("K")


@dataclass
class LoaderMetrics:
    """Metrics callback interface for Prometheus integration.

    Subclass and override callbacks to integrate with your metrics system.
    """
    cold_misses: int = 0
    hot_hits: int = 0
    total_io_calls: int = 0
    total_keys_processed: int = 0

    def on_cold_miss(self, loader_name: str, key_count: int) -> None:
        self.cold_misses += 1
        self.total_io_calls += 1
        self.total_keys_processed += key_count

    def on_hot_hit(self, loader_name: str, key_count: int) -> None:
        self.hot_hits += 1
        self.total_keys_processed += key_count


@dataclass
class _CacheEntry(Generic[V]):
    value: V
    expires_at: float


class LoaderCache(Generic[K, V]):
    """Memory cache with TTL for DataLoader results.

    Each Resolver instance gets its own LoaderCache - caches are NOT shared
    across Resolver instances.

    Args:
        ttl: Time-to-live in seconds, default 60. Set to 0 to disable TTL.
    """

    def __init__(self, ttl: int = 60):
        self._cache: dict[K, _CacheEntry[V]] = {}
        self.ttl = ttl

    def get(self, key: K) -> V | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        if self.ttl > 0 and time() > entry.expires_at:
            del self._cache[key]
            return None
        return entry.value

    def set(self, key: K, value: V) -> None:
        expires_at = time() + self.ttl if self.ttl > 0 else float("inf")
        self._cache[key] = _CacheEntry(value=value, expires_at=expires_at)

    def has(self, key: K) -> bool:
        return self.get(key) is not None

    def clear(self) -> None:
        self._cache.clear()

    def clear_expired(self) -> None:
        if self.ttl <= 0:
            return
        now = time()
        expired = [k for k, v in self._cache.items() if now > v.expires_at]
        for k in expired:
            del self._cache[k]


class CachedDataLoader(DataLoader, Generic[K, V]):
    """DataLoader with per-resolve-tree deduplication and optional caching.

    Features:
    - Same keys in one Resolver tree resolution → only one IO call
    - Results mapped back to callers in original key order
    - Optional LoaderCache integration with TTL
    - Metrics callbacks for cold miss / hot hit tracking

    The in-flight deduplication works by:
    1. Collecting all keys requested within the same event loop tick
    2. Checking which keys are already cached (hot hits)
    3. Fetching only uncached keys (cold misses) with a single IO call
    4. Recombining results to preserve original order for each caller
    """

    def __init__(
        self,
        cache: LoaderCache | None = None,
        metrics: LoaderMetrics | None = None,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self._loader_cache = cache
        self._metrics = metrics
        self._loader_name = self.__class__.__name__

    async def load(self, key: K) -> V:
        if self._loader_cache is not None:
            cached = self._loader_cache.get(key)
            if cached is not None:
                if self._metrics is not None:
                    self._metrics.on_hot_hit(self._loader_name, 1)
                return cached
        return await super().load(key)

    async def load_many(self, keys: Sequence[K]) -> list[V]:
        if self._loader_cache is None:
            return await super().load_many(keys)

        result_map: dict[int, V] = {}
        uncached_indices: list[int] = []
        uncached_keys: list[K] = []

        for i, key in enumerate(keys):
            cached = self._loader_cache.get(key)
            if cached is not None:
                result_map[i] = cached
            else:
                uncached_indices.append(i)
                uncached_keys.append(key)

        if self._metrics is not None:
            hit_count = len(keys) - len(uncached_keys)
            if hit_count > 0:
                self._metrics.on_hot_hit(self._loader_name, hit_count)

        if not uncached_keys:
            return [result_map[i] for i in range(len(keys))]

        if self._metrics is not None:
            self._metrics.on_cold_miss(self._loader_name, len(uncached_keys))

        fetched = await super().load_many(uncached_keys)

        for i, key, value in zip(uncached_indices, uncached_keys, fetched):
            self._loader_cache.set(key, value)
            result_map[i] = value

        return [result_map[i] for i in range(len(keys))]

    async def batch_load_fn(self, keys: Sequence[K]) -> Sequence[V]:
        raise NotImplementedError("Subclasses must implement batch_load_fn")

def build_list(items: Sequence[T], keys: list[V], get_pk: Callable[[T], V]) -> list[list[T]]:
    """
    helper function to build return list data required by aiodataloader
    """
    dct: DefaultDict[V, list[T]] = defaultdict(list)
    for item in items:
        _key = get_pk(item)
        dct[_key].append(item)
    return [dct.get(k, []) for k in keys]


def build_object(items: Sequence[T], keys: list[V], get_pk: Callable[[T], V]) -> list[T | None]:
    """
    helper function to build return object data required by aiodataloader
    """
    dct: dict[V, T] = {}
    for item in items:
        _key = get_pk(item)
        dct[_key] = item
    return [dct.get(k, None) for k in keys]


def copy_dataloader_kls(name, loader_kls):
    """
    quickly copy from an existing DataLoader class
    usage:
    SeniorMemberLoader = copy_dataloader('SeniorMemberLoader', ul.UserByLevelLoader)
    JuniorMemberLoader = copy_dataloader('JuniorMemberLoader', ul.UserByLevelLoader)
    """
    class NewLoader(loader_kls):
        pass
    NewLoader.__name__ = name
    NewLoader.__qualname__ = name
    return NewLoader


class StrictEmptyLoader(DataLoader):
    async def batch_load_fn(self, keys):
        """it should not be triggered, otherwise will raise Exception"""
        raise ValueError('EmptyLoader should load from pre loaded data')


class ListEmptyLoader(DataLoader):
    async def batch_load_fn(self, keys):
        return [[] for _ in keys]


class SingleEmptyLoader(DataLoader):
    async def batch_load_fn(self, keys):
        return [None for _ in keys]


def generate_strict_empty_loader(name):
    """generated Loader will raise ValueError if not found"""
    class NewLoader(StrictEmptyLoader):
        pass
    NewLoader.__name__ = name
    NewLoader.__qualname__ = name
    return NewLoader


def generate_list_empty_loader(name):
    """generated Loader will return [] if not found"""
    class NewLoader(ListEmptyLoader):
        pass
    NewLoader.__name__ = name
    NewLoader.__qualname__ = name
    return NewLoader


def generate_single_empty_loader(name):
    """generated Loader will return None if not found"""
    class NewLoader(SingleEmptyLoader):
        pass
    NewLoader.__name__ = name
    NewLoader.__qualname__ = name
    return NewLoader