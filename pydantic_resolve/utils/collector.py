import abc
from dataclasses import dataclass
from typing import Any, Iterator
from enum import Enum
import pydantic_resolve.constant as const
from pydantic_resolve.utils.field_metadata import iter_fields_with_marker


class MergeMode(str, Enum):
    """Merge strategies for collectors from parallel branches.

    Attributes:
        CONCAT: Default. Concatenate results in encounter order.
        UNION: Return unique values from all branches (preserves order).
        INTERSECT: Return values present in all branches.
        FIRST: Return only values from the first encountered branch.
        LAST: Return only values from the last encountered branch.
    """
    CONCAT = "concat"
    UNION = "union"
    INTERSECT = "intersect"
    FIRST = "first"
    LAST = "last"

@dataclass
class SendToInfo:
    collector_name: str | tuple[str]

def SendTo(name: str| tuple[str]) -> SendToInfo:
    return SendToInfo(collector_name=name)


def pre_generate_collector_config(kls):
    """
    iterate kls fields, check and collect field who's annotated metadata for SendTo exists
    if kls's const.COLLECTOR_CONFIGURATION exists and the fields is not empty, raise exception
    group those field name based on collector_name, if single, leave it as str, else make it tuple
    then generate the configuration such as
    { (field_a, field_b): collector_name } or ( field_a: collector_name })
    and set it into kls's const.COLLECTOR_CONFIGURATION
    """
    fields = list(_get_pydantic_field_items_with_send_to(kls))
    if not fields:
        return

    if hasattr(kls, const.COLLECTOR_CONFIGURATION):
        raise AttributeError(
            f"{const.COLLECTOR_CONFIGURATION} already exists; cannot use SendTo annotations at the same time"
        )

    grouped: dict[object, list[str]] = {}
    for field_name, meta in fields:
        grouped.setdefault(meta.collector_name, []).append(field_name)

    collect_dict: dict[object, object] = {}
    for collector_name, field_names in grouped.items():
        key: object = field_names[0] if len(field_names) == 1 else tuple(field_names)
        collect_dict[key] = collector_name

    setattr(kls, const.COLLECTOR_CONFIGURATION, collect_dict)

def _get_pydantic_field_items_with_send_to(kls) -> Iterator[tuple[str, SendToInfo]]:
    for name, _field_info, meta in iter_fields_with_marker(kls, SendToInfo):
        yield name, meta

class ICollector(metaclass=abc.ABCMeta):
    @abc.abstractmethod
    def __init__(self, alias: str):
        self.alias = alias

    @abc.abstractmethod
    def add(self, val):
        """how to add new element(s)"""

    @abc.abstractmethod
    def values(self) -> Any:
        """get result"""

    @abc.abstractmethod
    def merge(self, other: "ICollector") -> None:
        """merge another collector's values into this one"""


class Collector(ICollector):
    def __init__(self, alias: str, flat: bool = False, merge_mode: MergeMode = MergeMode.CONCAT):
        super().__init__(alias)
        self.flat = flat
        self.merge_mode = merge_mode
        self.val = []

    def add(self, val: Any | list[Any]) -> None:
        if self.flat:
            if isinstance(val, list):
                self.val.extend(val)
            else:
                raise TypeError('if flat, target should be list')
        else:
            self.val.append(val)

    def values(self) -> list[Any]:
        return self.val

    def merge(self, other: "Collector") -> None:
        """Merge another collector's values into this one according to merge_mode.

        Values from ``other`` are merged into ``self.val`` directly, not stored
        as source references, to avoid double-counting when the parent collector
        already received values via the ancestor_collectors path.
        """
        if not other.val:
            return
        self.val = self._apply_merge(self.val, other.val)

    def _apply_merge(self, current: list[Any], incoming: list[Any]) -> list[Any]:
        if self.merge_mode == MergeMode.CONCAT:
            result = list(current)
            result.extend(incoming)
            return result

        elif self.merge_mode == MergeMode.UNION:
            seen = set()
            result = []
            for v in current:
                try:
                    if v not in seen:
                        seen.add(v)
                        result.append(v)
                except TypeError:
                    if v not in result:
                        result.append(v)
            for v in incoming:
                try:
                    if v not in seen:
                        seen.add(v)
                        result.append(v)
                except TypeError:
                    if v not in result:
                        result.append(v)
            return result

        elif self.merge_mode == MergeMode.INTERSECT:
            return [v for v in current if v in incoming]

        elif self.merge_mode == MergeMode.FIRST:
            return list(current)

        elif self.merge_mode == MergeMode.LAST:
            return list(incoming)

        else:
            result = list(current)
            result.extend(incoming)
            return result