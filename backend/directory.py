"""Phone book and presence for a pager node.

Pure logic, no network or disk I/O — `server.py` feeds events in and renders
what comes out, so everything here is unit-testable with a fake clock.

Presence (one person = one SSID, possibly several devices/sessions):

* ``online``  — at least one live session that is answering heartbeats
  (a client that never sends app-level pings, e.g. the Android app, counts
  as online for as long as its socket is open: uvicorn's protocol pings
  close dead TCP for us).
* ``zombie``  — "looks alive, isn't answering": every session is silent for
  ``ONLINE_WINDOW``..``ZOMBIE_TIMEOUT`` (tab asleep, phone in a tunnel), or
  the last session dropped abnormally less than ``LOST_GRACE`` ago and may
  come back. For contacts on other nodes: the route to them goes through a
  peer that stopped answering hellos.
* ``offline`` — no session. A zombie session silent for ``ZOMBIE_TIMEOUT``
  is reaped by the server (socket closed) and the person becomes offline —
  this is what keeps dead sessions from piling up.

Phone book (per viewer):

* ``favorites`` — contacts the viewer starred (stored on the node).
* ``nearby``    — this node's own contacts (0 hops) and the contacts of the
  nodes we talk to directly (1 hop), grouped by node.
* ``remote``    — contacts discovered as neighbours of neighbours (2+ hops),
  learned by distance-vector adverts in ``/mesh/hello``.
"""
from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

STATUS_ONLINE = "online"
STATUS_ZOMBIE = "zombie"
STATUS_OFFLINE = "offline"
_STATUS_ORDER = {STATUS_ONLINE: 0, STATUS_ZOMBIE: 1, STATUS_OFFLINE: 2}

# Web client pings every 10 s; a background tab may stretch that to ~60 s.
ONLINE_WINDOW = 45
# Silent this long → the session is dead: the server closes it.
ZOMBIE_TIMEOUT = 300
# After an abnormal disconnect (1006 etc.) the person stays a zombie this long.
LOST_GRACE = 120

# Peers (other nodes): hello every MESH_PING_INTERVAL.
PEER_ZOMBIE_AFTER = 90
PEER_DEAD_AFTER = 300
# Routes further than this are not advertised (bounds the phone book and
# guarantees count-to-infinity stops quickly).
MAX_HOPS = 4

_SSID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_NODE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def valid_ssid(ss_id: str) -> bool:
    return bool(ss_id) and bool(_SSID_RE.match(ss_id))


def valid_node_id(node_id: str) -> bool:
    return bool(node_id) and bool(_NODE_RE.match(node_id))


def _clean_name(name: Any, fallback: str) -> str:
    name = str(name or "").strip()
    return name[:64] if name else fallback


def status_rank(status: str) -> int:
    return _STATUS_ORDER.get(status, 3)


# --------------------------------------------------------------------------
# Presence
# --------------------------------------------------------------------------
@dataclass
class Session:
    sid: str
    ss_id: str
    ws: Any
    connected_at: float
    last_seen: float
    heartbeat: bool = False  # the client sends app-level pings
    client: str = ""


class Presence:
    """Live sessions on this node and the status they add up to per SSID."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.sessions: dict[str, Session] = {}
        self.lost: dict[str, float] = {}  # ss_id -> when the last session dropped abnormally
        self._reported: dict[str, str] = {}

    # --- events -----------------------------------------------------------
    def open(self, ss_id: str, ws: Any = None, client: str = "") -> Session:
        now = self.clock()
        s = Session(secrets.token_hex(8), ss_id, ws, now, now, client=client)
        self.sessions[s.sid] = s
        self.lost.pop(ss_id, None)
        return s

    def touch(self, sid: str, *, heartbeat: bool = False) -> None:
        s = self.sessions.get(sid)
        if s is None:
            return
        s.last_seen = self.clock()
        if heartbeat:
            s.heartbeat = True

    def close(self, sid: str, *, abnormal: bool = False) -> Optional[Session]:
        s = self.sessions.pop(sid, None)
        if s is None:
            return None
        if abnormal and not self.sessions_of(s.ss_id):
            self.lost[s.ss_id] = self.clock()
        return s

    # --- queries ----------------------------------------------------------
    def sessions_of(self, ss_id: str) -> list[Session]:
        return [s for s in self.sessions.values() if s.ss_id == ss_id]

    def is_connected(self, ss_id: str) -> bool:
        return any(s.ss_id == ss_id for s in self.sessions.values())

    def status(self, ss_id: str) -> str:
        now = self.clock()
        mine = self.sessions_of(ss_id)
        if mine:
            if any(not s.heartbeat or now - s.last_seen < ONLINE_WINDOW for s in mine):
                return STATUS_ONLINE
            return STATUS_ZOMBIE
        t = self.lost.get(ss_id)
        if t is not None and now - t < LOST_GRACE:
            return STATUS_ZOMBIE
        return STATUS_OFFLINE

    def last_seen(self, ss_id: str) -> Optional[float]:
        mine = self.sessions_of(ss_id)
        if mine:
            return max(s.last_seen for s in mine)
        return self.lost.get(ss_id)

    def snapshot(self) -> dict[str, str]:
        ids = {s.ss_id for s in self.sessions.values()} | set(self.lost)
        return {sid: self.status(sid) for sid in ids}

    def dead_sessions(self) -> list[Session]:
        """Heartbeat sessions silent for ZOMBIE_TIMEOUT — the server reaps these."""
        now = self.clock()
        return [s for s in self.sessions.values()
                if s.heartbeat and now - s.last_seen >= ZOMBIE_TIMEOUT]

    def prune(self) -> None:
        now = self.clock()
        self.lost = {k: t for k, t in self.lost.items() if now - t < LOST_GRACE}

    def changes(self) -> dict[str, str]:
        """Statuses that changed since the previous call (offline included)."""
        current = self.snapshot()
        diff = {k: v for k, v in current.items() if self._reported.get(k) != v}
        for gone in set(self._reported) - set(current):
            if self._reported[gone] != STATUS_OFFLINE:
                diff[gone] = STATUS_OFFLINE
        self._reported = {k: v for k, v in current.items() if v != STATUS_OFFLINE}
        return diff

    def stats(self) -> dict:
        snap = self.snapshot()
        return {
            "sessions": len(self.sessions),
            "online": sum(1 for v in snap.values() if v == STATUS_ONLINE),
            "zombie": sum(1 for v in snap.values() if v == STATUS_ZOMBIE),
        }


# --------------------------------------------------------------------------
# Mesh directory (distance-vector over /mesh/hello)
# --------------------------------------------------------------------------
@dataclass
class Contact:
    ss_id: str
    display_name: str
    node_id: str          # home node (where the person's sessions live)
    hops: int = 0         # 0 = this node, 1 = a direct neighbour, 2+ = further
    via: str = ""         # direct neighbour we route through ("" for local)
    status: str = STATUS_OFFLINE
    kind: str = "user"    # user | athlete
    athlete_id: str = ""

    def public(self) -> dict:
        d = {
            "ss_id": self.ss_id,
            "display_name": self.display_name,
            "node_id": self.node_id,
            "hops": self.hops,
            "status": self.status,
            "kind": self.kind,
        }
        if self.athlete_id:
            d["athlete_id"] = self.athlete_id
        return d


@dataclass
class Peer:
    node_id: str
    url: str
    last_seen: float
    first_seen: float = field(default=0.0)


class Directory:
    """What this node knows about other nodes and the people behind them."""

    def __init__(self, node_id: str, clock: Callable[[], float] = time.time):
        self.node_id = node_id
        self.clock = clock
        self.peers: dict[str, Peer] = {}
        # via (direct neighbour) -> ss_id -> Contact as that neighbour advertised it (+1 hop)
        self.adverts: dict[str, dict[str, Contact]] = {}
        # via -> nodes that neighbour talks to: node_id -> url
        self.peer_neighbors: dict[str, dict[str, str]] = {}

    # --- peers ------------------------------------------------------------
    def peer_status(self, node_id: str) -> str:
        p = self.peers.get(node_id)
        if p is None:
            return STATUS_OFFLINE
        age = self.clock() - p.last_seen
        if age < PEER_ZOMBIE_AFTER:
            return STATUS_ONLINE
        if age < PEER_DEAD_AFTER:
            return STATUS_ZOMBIE
        return STATUS_OFFLINE

    def live_peer_ids(self) -> list[str]:
        return [nid for nid in self.peers if self.peer_status(nid) == STATUS_ONLINE]

    def url_for(self, node_id: str) -> Optional[str]:
        p = self.peers.get(node_id)
        if p and p.url.startswith("http"):
            return p.url
        return None

    def ingest_hello(self, node_id: str, url: str, contacts: Iterable[dict],
                     neighbors: Iterable[dict] = ()) -> None:
        """A neighbour told us who it reaches. Replaces its previous advert."""
        if not valid_node_id(node_id) or node_id == self.node_id:
            return
        now = self.clock()
        prev = self.peers.get(node_id)
        if not url.startswith("http") and prev is not None:
            url = prev.url  # keep a real URL learned earlier
        self.peers[node_id] = Peer(node_id, url, now, prev.first_seen if prev else now)

        table: dict[str, Contact] = {}
        for c in contacts or []:
            if not isinstance(c, dict):
                continue
            ss_id = str(c.get("ss_id", ""))
            if not valid_ssid(ss_id):
                continue
            home = str(c.get("node_id") or node_id)
            if home == self.node_id:
                continue  # our own contact coming back around
            hops = int(c.get("hops", 0) or 0) + 1
            if hops > MAX_HOPS:
                continue
            status = str(c.get("status", STATUS_OFFLINE))
            if status not in _STATUS_ORDER:
                status = STATUS_OFFLINE
            kind = "athlete" if c.get("kind") == "athlete" else "user"
            existing = table.get(ss_id)
            if existing is not None and existing.hops <= hops:
                continue
            table[ss_id] = Contact(
                ss_id=ss_id,
                display_name=_clean_name(c.get("display_name"), ss_id),
                node_id=home if valid_node_id(home) else node_id,
                hops=hops, via=node_id, status=status, kind=kind,
                athlete_id=str(c.get("athlete_id", "") or "")[:64],
            )
        self.adverts[node_id] = table

        nbrs: dict[str, str] = {}
        for n in neighbors or []:
            if not isinstance(n, dict):
                continue
            nid = str(n.get("node_id", ""))
            if valid_node_id(nid) and nid not in (self.node_id, node_id):
                nbrs[nid] = str(n.get("url", ""))[:256]
        self.peer_neighbors[node_id] = nbrs

    def drop_dead_peers(self) -> list[str]:
        dead = [nid for nid in self.peers if self.peer_status(nid) == STATUS_OFFLINE]
        for nid in dead:
            self.peers.pop(nid, None)
            self.adverts.pop(nid, None)
            self.peer_neighbors.pop(nid, None)
        return dead

    # --- routes -----------------------------------------------------------
    def _effective(self, c: Contact) -> Contact:
        """A contact as seen from here: a zombie peer turns its people into zombies."""
        peer_state = self.peer_status(c.via)
        if peer_state == STATUS_ONLINE or c.status == STATUS_OFFLINE:
            return c
        return Contact(**{**c.__dict__, "status": STATUS_ZOMBIE})

    def remote_contacts(self) -> dict[str, Contact]:
        """Best (fewest hops, then best status) route per ss_id through live peers."""
        best: dict[str, Contact] = {}
        for via, table in self.adverts.items():
            if self.peer_status(via) == STATUS_OFFLINE:
                continue
            for ss_id, c in table.items():
                eff = self._effective(c)
                cur = best.get(ss_id)
                if cur is None or (eff.hops, status_rank(eff.status)) < (cur.hops, status_rank(cur.status)):
                    best[ss_id] = eff
        return best

    def route(self, ss_id: str) -> Optional[Contact]:
        return self.remote_contacts().get(ss_id)

    def vias_for(self, via: str) -> list[str]:
        return list(self.adverts.get(via, {}).keys())

    def advert_for(self, peer_node_id: str, local: Iterable[Contact]) -> list[dict]:
        """What we tell `peer_node_id`: our people + what we reach not through it."""
        out = [c.public() for c in local]
        for c in self.remote_contacts().values():
            if c.via == peer_node_id or c.node_id == peer_node_id or c.hops >= MAX_HOPS:
                continue  # split horizon
            out.append(c.public())
        return out

    def neighbors_advert(self, for_node: str = "") -> list[dict]:
        return [{"node_id": p.node_id, "url": p.url, "status": self.peer_status(p.node_id)}
                for p in self.peers.values()
                if p.node_id != for_node and self.peer_status(p.node_id) != STATUS_OFFLINE]

    def known_nodes(self) -> dict[str, dict]:
        """Every node we know of: direct peers, their neighbours, contacts' home nodes."""
        nodes: dict[str, dict] = {}
        for p in self.peers.values():
            nodes[p.node_id] = {"node_id": p.node_id, "url": p.url, "hops": 1,
                                "via": p.node_id, "status": self.peer_status(p.node_id)}
        for via, nbrs in self.peer_neighbors.items():
            if self.peer_status(via) == STATUS_OFFLINE:
                continue
            for nid, url in nbrs.items():
                if nid not in nodes:
                    nodes[nid] = {"node_id": nid, "url": url, "hops": 2, "via": via,
                                  "status": self.peer_status(via)}
        for c in self.remote_contacts().values():
            n = nodes.get(c.node_id)
            if n is None or n["hops"] > c.hops:
                nodes[c.node_id] = {"node_id": c.node_id, "url": (n or {}).get("url", ""),
                                    "hops": c.hops, "via": c.via,
                                    "status": self.peer_status(c.via)}
        return nodes

    # --- phone book -------------------------------------------------------
    def phonebook(self, viewer: str, local: list[Contact], favorites: dict[str, str]) -> dict:
        """The hierarchical phone book for `viewer`.

        `favorites` maps ss_id -> display name snapshot (shown when the
        contact is out of reach).
        """
        everyone: dict[str, Contact] = {c.ss_id: c for c in local}
        for ss_id, c in self.remote_contacts().items():
            everyone.setdefault(ss_id, c)
        everyone.pop(viewer, None)

        def sort_key(c: Contact):
            return (status_rank(c.status), 0 if c.kind == "athlete" else 1, c.display_name.lower())

        def entry(c: Contact) -> dict:
            d = c.public()
            d["favorite"] = c.ss_id in favorites
            return d

        fav_list = []
        for ss_id, name in favorites.items():
            if ss_id == viewer:
                continue
            c = everyone.get(ss_id) or Contact(ss_id, name or ss_id, node_id="", hops=-1,
                                               status=STATUS_OFFLINE)
            fav_list.append(c)
        fav_list.sort(key=sort_key)

        nodes = self.known_nodes()
        by_node: dict[str, list[Contact]] = {}
        for c in everyone.values():
            by_node.setdefault(c.node_id, []).append(c)

        def node_group(nid: str, hops: int) -> dict:
            people = sorted(by_node.get(nid, []), key=sort_key)
            info = nodes.get(nid, {})
            if nid == self.node_id:
                node_status = STATUS_ONLINE
            else:
                node_status = info.get("status", STATUS_OFFLINE)
            return {
                "node_id": nid,
                "self": nid == self.node_id,
                "hops": hops,
                "status": node_status,
                "url": info.get("url", ""),
                "counts": _counts(people),
                "contacts": [entry(c) for c in people],
            }

        nearby = [node_group(self.node_id, 0)]
        remote = []
        node_hops = {nid: n["hops"] for nid, n in nodes.items()}
        for nid in set(by_node) | set(nodes):
            if nid == self.node_id:
                continue
            hops = node_hops.get(nid)
            if hops is None:
                hops = min(c.hops for c in by_node[nid])
            (nearby if hops <= 1 else remote).append(node_group(nid, hops))
        nearby[1:] = sorted(nearby[1:], key=lambda g: (status_rank(g["status"]), g["node_id"]))
        remote.sort(key=lambda g: (g["hops"], status_rank(g["status"]), g["node_id"]))

        def group_counts(groups: list[dict]) -> dict:
            total = {STATUS_ONLINE: 0, STATUS_ZOMBIE: 0, STATUS_OFFLINE: 0}
            for g in groups:
                for k in total:
                    total[k] += g["counts"][k]
            return total

        return {
            "node_id": self.node_id,
            "viewer": viewer,
            "favorites": {"title": "Избранное", "counts": _counts(fav_list),
                          "contacts": [entry(c) for c in fav_list]},
            "nearby": {"title": "Ближайшие ноды", "counts": group_counts(nearby), "nodes": nearby},
            "remote": {"title": "Удалённые контакты", "counts": group_counts(remote), "nodes": remote},
        }


def _counts(people: Iterable[Contact]) -> dict:
    out = {STATUS_ONLINE: 0, STATUS_ZOMBIE: 0, STATUS_OFFLINE: 0}
    for c in people:
        out[c.status if c.status in out else STATUS_OFFLINE] += 1
    return out
