"""Phone book + presence logic (directory.py), driven by a fake clock."""
import directory as d
from directory import Contact, Directory, Presence


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# --- presence -------------------------------------------------------------
def test_legacy_client_without_pings_is_online_while_connected():
    clk = Clock()
    p = Presence(clk)
    p.open("ss-a")
    clk.t += 3600
    assert p.status("ss-a") == d.STATUS_ONLINE
    assert p.dead_sessions() == []


def test_silent_heartbeat_session_goes_zombie_then_is_reaped():
    clk = Clock()
    p = Presence(clk)
    s = p.open("ss-a")
    p.touch(s.sid, heartbeat=True)
    clk.t += d.ONLINE_WINDOW - 1
    assert p.status("ss-a") == d.STATUS_ONLINE
    clk.t += 2
    assert p.status("ss-a") == d.STATUS_ZOMBIE
    clk.t += d.ZOMBIE_TIMEOUT
    assert [x.sid for x in p.dead_sessions()] == [s.sid]
    p.close(s.sid)  # reaped: a clean kill, no grace
    assert p.status("ss-a") == d.STATUS_OFFLINE


def test_one_live_device_keeps_person_online():
    clk = Clock()
    p = Presence(clk)
    phone = p.open("ss-a")
    laptop = p.open("ss-a")
    p.touch(phone.sid, heartbeat=True)
    p.touch(laptop.sid, heartbeat=True)
    clk.t += 100
    p.touch(laptop.sid, heartbeat=True)
    assert p.status("ss-a") == d.STATUS_ONLINE


def test_abnormal_drop_is_zombie_for_grace_then_offline():
    clk = Clock()
    p = Presence(clk)
    s = p.open("ss-a")
    p.close(s.sid, abnormal=True)
    assert p.status("ss-a") == d.STATUS_ZOMBIE
    clk.t += d.LOST_GRACE + 1
    assert p.status("ss-a") == d.STATUS_OFFLINE
    p.prune()
    assert p.lost == {}


def test_clean_close_is_offline_immediately():
    p = Presence(Clock())
    s = p.open("ss-a")
    p.close(s.sid, abnormal=False)
    assert p.status("ss-a") == d.STATUS_OFFLINE


def test_reconnect_clears_zombie():
    p = Presence(Clock())
    s = p.open("ss-a")
    p.close(s.sid, abnormal=True)
    p.open("ss-a")
    assert p.status("ss-a") == d.STATUS_ONLINE


def test_changes_report_only_diffs_and_offline_transitions():
    clk = Clock()
    p = Presence(clk)
    s = p.open("ss-a")
    assert p.changes() == {"ss-a": d.STATUS_ONLINE}
    assert p.changes() == {}
    p.close(s.sid)
    assert p.changes() == {"ss-a": d.STATUS_OFFLINE}
    assert p.changes() == {}


# --- mesh directory -------------------------------------------------------
def _local(node, *ids):
    return [Contact(i, i.upper(), node_id=node) for i in ids]


def test_neighbour_of_neighbour_is_remote_with_hops():
    clk = Clock()
    a, b = Directory("node-a", clk), Directory("node-b", clk)
    # C tells B about its person (hop 0 at C → 1 at B).
    b.ingest_hello("node-c", "http://c", [Contact("ss-c1", "C1", "node-c", status="online").public()])
    # B tells A: its own person + what it learned from C.
    a.ingest_hello("node-b", "http://b", b.advert_for("node-a", _local("node-b", "ss-b1")),
                   b.neighbors_advert())
    assert a.route("ss-b1").hops == 1
    far = a.route("ss-c1")
    assert (far.hops, far.via, far.node_id, far.status) == (2, "node-b", "node-c", "online")
    assert a.known_nodes()["node-c"]["hops"] == 2


def test_split_horizon_does_not_echo_routes_back():
    clk = Clock()
    a = Directory("node-a", clk)
    a.ingest_hello("node-b", "http://b", [{"ss_id": "ss-b1", "display_name": "B1"}])
    advert = a.advert_for("node-b", _local("node-a", "ss-a1"))
    assert [c["ss_id"] for c in advert] == ["ss-a1"]
    # but another neighbour does hear about ss-b1
    assert {c["ss_id"] for c in a.advert_for("node-x", [])} == {"ss-b1"}


def test_own_contacts_coming_back_are_ignored_and_hops_capped():
    a = Directory("node-a", Clock())
    a.ingest_hello("node-b", "http://b", [
        {"ss_id": "ss-mine", "node_id": "node-a", "hops": 1},
        {"ss_id": "ss-far", "node_id": "node-z", "hops": d.MAX_HOPS},
        {"ss_id": "<script>", "display_name": "x"},
    ])
    assert a.remote_contacts() == {}


def test_hello_replaces_previous_advert():
    a = Directory("node-a", Clock())
    a.ingest_hello("node-b", "http://b", [{"ss_id": "ss-1"}, {"ss_id": "ss-2"}])
    a.ingest_hello("node-b", "http://b", [{"ss_id": "ss-2"}])
    assert set(a.remote_contacts()) == {"ss-2"}


def test_shortest_route_wins():
    a = Directory("node-a", Clock())
    a.ingest_hello("node-b", "http://b", [{"ss_id": "ss-x", "node_id": "node-z", "hops": 2}])
    a.ingest_hello("node-c", "http://c", [{"ss_id": "ss-x", "node_id": "node-z", "hops": 1}])
    assert a.route("ss-x").via == "node-c"


def test_silent_peer_turns_its_people_into_zombies_then_vanishes():
    clk = Clock()
    a = Directory("node-a", clk)
    a.ingest_hello("node-b", "http://b", [{"ss_id": "ss-1", "status": "online"},
                                          {"ss_id": "ss-2", "status": "offline"}])
    clk.t += d.PEER_ZOMBIE_AFTER + 1
    assert a.peer_status("node-b") == d.STATUS_ZOMBIE
    assert a.route("ss-1").status == d.STATUS_ZOMBIE
    assert a.route("ss-2").status == d.STATUS_OFFLINE
    clk.t += d.PEER_DEAD_AFTER
    assert a.drop_dead_peers() == ["node-b"]
    assert a.route("ss-1") is None


def test_phonebook_groups_favorites_nearby_remote():
    clk = Clock()
    a = Directory("node-a", clk)
    a.ingest_hello("node-b", "http://b", [
        {"ss_id": "ss-b1", "display_name": "Bob", "status": "online"},
        {"ss_id": "ss-c1", "display_name": "Cat", "node_id": "node-c", "hops": 1, "kind": "athlete"},
    ], [{"node_id": "node-c", "url": "http://c"}])
    local = [Contact("ss-me", "Me", "node-a"),
             Contact("ss-a1", "Ann", "node-a", status=d.STATUS_ZOMBIE),
             Contact("ss-a2", "Al", "node-a", status=d.STATUS_ONLINE)]
    book = a.phonebook("ss-me", local, {"ss-c1": "Cat", "ss-gone": "Ghost"})

    favs = book["favorites"]["contacts"]
    assert [c["ss_id"] for c in favs] == ["ss-c1", "ss-gone"]  # unreachable favourite kept, offline
    assert favs[1]["status"] == d.STATUS_OFFLINE and favs[1]["display_name"] == "Ghost"

    nearby = book["nearby"]["nodes"]
    assert nearby[0]["node_id"] == "node-a" and nearby[0]["self"] is True
    # viewer is not in their own phone book; online first, then zombie
    assert [c["ss_id"] for c in nearby[0]["contacts"]] == ["ss-a2", "ss-a1"]
    assert [g["node_id"] for g in nearby[1:]] == ["node-b"]
    assert book["nearby"]["counts"] == {"online": 2, "zombie": 1, "offline": 0}

    remote = book["remote"]["nodes"]
    assert [g["node_id"] for g in remote] == ["node-c"]
    cat = remote[0]["contacts"][0]
    assert (cat["hops"], cat["kind"], cat["favorite"]) == (2, "athlete", True)
