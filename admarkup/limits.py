"""Validated resource budgets for AdMarkup."""

from dataclasses import dataclass, fields
from typing import Mapping

MIB = 1_048_576


@dataclass(frozen=True)
class ResourceLimits:
    """Resource limits applied by the service."""

    max_input_bytes: int = 10 * MIB
    max_json_depth: int = 64
    max_json_nodes: int = 100_000
    max_extracted_children: int = 50
    max_payload_bytes: int = 5 * MIB
    max_total_child_bytes: int = 20 * MIB

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{field.name} must be a positive integer.")

    @classmethod
    def from_config(cls, config: Mapping[str, object]) -> "ResourceLimits":
        """Create limits from all required manifest configuration values.

        Returns:
            Validated resource limits.

        Raises:
            ValueError: If a limit is missing or invalid.
        """
        names = [field.name for field in fields(cls)]
        missing = [name for name in names if name not in config]
        if missing:
            raise ValueError(f"Missing resource limit configuration: {', '.join(missing)}.")
        values = {name: config[name] for name in names}
        return cls(**values)
