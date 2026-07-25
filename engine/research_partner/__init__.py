from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .proposal_bundle import materialize_proposal_bundle

__all__ = ["materialize_proposal_bundle"]


def __getattr__(name: str) -> Any:
    if name != "materialize_proposal_bundle":
        raise AttributeError(name)
    from .proposal_bundle import materialize_proposal_bundle

    return materialize_proposal_bundle
