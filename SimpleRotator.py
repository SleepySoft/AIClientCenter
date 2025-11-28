import threading
from typing import TypeVar, Generic, List, Optional

T = TypeVar('T')


class SimpleRotator(Generic[T]):
    """
    Generic manager for rotating resources (Models, API Tokens, Proxies, etc.)
    based on usage limits.
    """

    def __init__(self, items: List[T] = None, rotate_per_times: int = 1):
        self._items: List[T] = items or []
        self._rotate_per_times = rotate_per_times
        self._current_idx = 0
        self._current_uses = 0
        self._lock = threading.RLock()

    def set_items(self, items: List[T], rotate_per_times: int = 1):
        """Update the resource pool and reset counters."""
        with self._lock:
            self._items = items
            self._rotate_per_times = max(1, rotate_per_times)
            # Resetting helps avoid index out of bounds if list shrinks
            self._current_idx = 0
            self._current_uses = 0

    def get_next(self) -> Optional[T]:
        """
        Get the current resource and increment usage counter.
        Triggers rotation if usage threshold is reached.
        """
        with self._lock:
            if not self._items:
                return None

            # Rotation Logic
            # Note: We check threshold BEFORE incrementing for the current turn
            # If previous calls exhausted the quota, we switch now.
            if self._current_uses >= self._rotate_per_times:
                self._current_idx = (self._current_idx + 1) % len(self._items)
                self._current_uses = 0

            self._current_uses += 1
            return self._items[self._current_idx]

    def get_current(self) -> Optional[T]:
        """Peek at the current resource without incrementing usage (for logging/dashboard)."""
        with self._lock:
            if not self._items:
                return None
            return self._items[self._current_idx]

    def get_stats(self) -> dict:
        """Helper for your Dashboard to see rotation status."""
        with self._lock:
            return {
                "total_items": len(self._items),
                "current_index": self._current_idx,
                "current_uses": self._current_uses,
                "rotate_threshold": self._rotate_per_times
            }
