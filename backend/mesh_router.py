"""Mesh routing core for Safarancho Pager.

This module is deliberately transport-agnostic and side-effect free: it decides
*what* should happen to a message (deliver locally, forward to which peers,
drop as duplicate) but never performs network or disk I/O itself. The server
(or any transport bridge — internet, Wi-Fi, BLE) feeds messages in and acts on
the returned :class:`RoutingDecision`.

Keeping the logic pure makes the mesh testable without a running cluster and
lets the same routing rules drive every transport.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

# A message may traverse at most this many hops before being dropped. This
# bounds flooding on large meshes and guarantees termination.
DEFAULT_TTL = 8

# How long a message id stays in the dedup cache. Long enough to absorb
# multi-path arrivals, short enough to bound memory.
SEEN_TTL_SECONDS = 600


def new_message_id() -> str:
    """Globally-unique-ish id for a mesh message (128 bits of entropy)."""
    return secrets.token_hex(16)


@dataclass
class MeshMessage:
    """A message travelling across the mesh.

    `path` is the ordered list of node ids the message has already visited; it
    is used both for loop avoidance and for building a return route.
    """

    msg_id: str
    from_ss: str
    target_ss: str
    text: str
    ttl: int = DEFAULT_TTL
    path: list[str] = field(default_factory=list)
    kind: str = "text"  # text | file | ack
    created_at: float = field(default_factory=time.time)

    @classmethod
    def create(cls, from_ss: str, target_ss: str, text: str, *, kind: str = "text") -> "MeshMessage":
        return cls(
            msg_id=new_message_id(),
            from_ss=from_ss,
            target_ss=target_ss,
            text=text,
            kind=kind,
        )

    def to_dict(self) -> dict:
        return {
            "msg_id": self.msg_id,
            "from_ss": self.from_ss,
            "target_ss": self.target_ss,
            "text": self.text,
            "ttl": self.ttl,
            "path": list(self.path),
            "kind": self.kind,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MeshMessage":
        """Build from a wire dict, ignoring unknown fields and filling defaults."""
        return cls(
            msg_id=data.get("msg_id") or new_message_id(),
            from_ss=str(data.get("from_ss", "")),
            target_ss=str(data.get("target_ss", "")),
            text=str(data.get("text", "")),
            ttl=int(data.get("ttl", DEFAULT_TTL)),
            path=list(data.get("path", [])),
            kind=str(data.get("kind", "text")),
            created_at=float(data.get("created_at", time.time())),
        )


@dataclass
class RoutingDecision:
    """What the caller should do with a message after `MeshRouter.handle`."""

    deliver_locally: bool
    forward_to: list[str]  # node_ids to forward to (caller resolves to URLs/transports)
    dropped: bool = False
    reason: str = ""


class SeenCache:
    """Time-bounded set of message ids for duplicate suppression."""

    def __init__(self, ttl_seconds: float = SEEN_TTL_SECONDS):
        self.ttl = ttl_seconds
        self._seen: dict[str, float] = {}

    def seen(self, msg_id: str) -> bool:
        self._evict()
        return msg_id in self._seen

    def add(self, msg_id: str) -> None:
        self._seen[msg_id] = time.time()
        self._evict()

    def _evict(self) -> None:
        if len(self._seen) < 1024:
            return
        cutoff = time.time() - self.ttl
        self._seen = {mid: ts for mid, ts in self._seen.items() if ts >= cutoff}

    def __len__(self) -> int:
        return len(self._seen)


class MeshRouter:
    """Decides delivery and forwarding for mesh messages.

    The router knows this node's id and which contacts (ss_ids) are reachable
    locally. It does not know peer transport details — it returns node ids and
    lets the caller map them to a concrete transport.
    """

    def __init__(self, node_id: str):
        self.node_id = node_id
        self.seen = SeenCache()
        # ss_id -> set of neighbour node_ids known to reach that contact
        self.routes: dict[str, set[str]] = {}
        self.local_ssids: set[str] = set()
        self.peer_node_ids: set[str] = set()

    # --- topology updates -------------------------------------------------
    def set_local_ssids(self, ssids: Iterable[str]) -> None:
        self.local_ssids = {s for s in ssids if s}

    def learn_route(self, ss_id: str, via_node_id: str) -> None:
        if ss_id and via_node_id and via_node_id != self.node_id:
            self.routes.setdefault(ss_id, set()).add(via_node_id)
            self.peer_node_ids.add(via_node_id)

    def forget_node(self, node_id: str) -> None:
        self.peer_node_ids.discard(node_id)
        for via in self.routes.values():
            via.discard(node_id)
        self.routes = {ss: via for ss, via in self.routes.items() if via}

    # --- routing ----------------------------------------------------------
    def handle(self, msg: MeshMessage, *, available_peers: Iterable[str]) -> RoutingDecision:
        """Decide what to do with an incoming/outgoing message.

        `available_peers` is the set of node_ids currently reachable from this
        node (online neighbours). The router never forwards back to a node that
        already appears in the message path (loop avoidance) nor to the message
        origin.
        """
        available = {p for p in available_peers if p and p != self.node_id}

        if self.seen.seen(msg.msg_id):
            return RoutingDecision(False, [], dropped=True, reason="duplicate")
        self.seen.add(msg.msg_id)

        if msg.ttl <= 0:
            # Still deliver locally if it's for us; just don't forward further.
            return RoutingDecision(
                deliver_locally=msg.target_ss in self.local_ssids,
                forward_to=[],
                dropped=msg.target_ss not in self.local_ssids,
                reason="ttl_expired",
            )

        # Record that we handled this hop.
        if self.node_id not in msg.path:
            msg.path.append(self.node_id)

        deliver_locally = msg.target_ss in self.local_ssids

        # If the target is local we still gossip onward only when other nodes
        # might also hold the same ss_id (multi-device); for simplicity we stop
        # forwarding once delivered locally.
        if deliver_locally:
            return RoutingDecision(True, [], reason="local")

        visited = set(msg.path)
        # Prefer directed routing if we know which neighbour reaches the target.
        directed = self.routes.get(msg.target_ss, set()) & available - visited
        if directed:
            return RoutingDecision(False, sorted(directed), reason="directed")

        # Otherwise flood to every available neighbour we haven't visited.
        flood = sorted(available - visited)
        if not flood:
            return RoutingDecision(False, [], dropped=True, reason="no_route")
        return RoutingDecision(False, flood, reason="flood")

    def prepare_outgoing(self, msg: MeshMessage) -> None:
        """Stamp an outgoing message with this node as the first hop."""
        if self.node_id not in msg.path:
            msg.path.append(self.node_id)
        self.seen.add(msg.msg_id)
