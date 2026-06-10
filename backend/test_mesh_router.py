"""Tests for the transport-agnostic mesh router."""
from mesh_router import MeshMessage, MeshRouter, SeenCache, new_message_id


def test_message_ids_are_unique():
    ids = {new_message_id() for _ in range(1000)}
    assert len(ids) == 1000


def test_local_delivery_when_target_is_local():
    r = MeshRouter("node-a")
    r.set_local_ssids(["ss-1111-pager"])
    msg = MeshMessage.create("ss-2222-pager", "ss-1111-pager", "hi")
    d = r.handle(msg, available_peers=["node-b"])
    assert d.deliver_locally is True
    assert d.forward_to == []


def test_duplicate_is_dropped():
    r = MeshRouter("node-a")
    msg = MeshMessage.create("ss-x", "ss-y", "hi")
    r.handle(msg, available_peers=["node-b"])
    again = r.handle(msg, available_peers=["node-b"])
    assert again.dropped is True
    assert again.reason == "duplicate"


def test_flood_to_unvisited_neighbours():
    r = MeshRouter("node-a")
    msg = MeshMessage.create("ss-x", "ss-unknown", "hi")
    d = r.handle(msg, available_peers=["node-b", "node-c"])
    assert d.deliver_locally is False
    assert set(d.forward_to) == {"node-b", "node-c"}


def test_loop_avoidance_skips_visited_nodes():
    r = MeshRouter("node-a")
    msg = MeshMessage.create("ss-x", "ss-unknown", "hi")
    msg.path = ["node-b"]  # already passed through node-b
    d = r.handle(msg, available_peers=["node-b", "node-c"])
    assert set(d.forward_to) == {"node-c"}


def test_directed_routing_prefers_known_route():
    r = MeshRouter("node-a")
    r.learn_route("ss-target", "node-c")
    msg = MeshMessage.create("ss-x", "ss-target", "hi")
    d = r.handle(msg, available_peers=["node-b", "node-c"])
    assert d.forward_to == ["node-c"]
    assert d.reason == "directed"


def test_ttl_expiry_stops_forwarding():
    r = MeshRouter("node-a")
    msg = MeshMessage.create("ss-x", "ss-unknown", "hi")
    msg.ttl = 0
    d = r.handle(msg, available_peers=["node-b"])
    assert d.forward_to == []
    assert d.dropped is True
    assert d.reason == "ttl_expired"


def test_ttl_expiry_still_delivers_local():
    r = MeshRouter("node-a")
    r.set_local_ssids(["ss-me"])
    msg = MeshMessage.create("ss-x", "ss-me", "hi")
    msg.ttl = 0
    d = r.handle(msg, available_peers=["node-b"])
    assert d.deliver_locally is True


def test_no_route_when_no_neighbours():
    r = MeshRouter("node-a")
    msg = MeshMessage.create("ss-x", "ss-unknown", "hi")
    d = r.handle(msg, available_peers=[])
    assert d.dropped is True
    assert d.reason == "no_route"


def test_forget_node_clears_routes():
    r = MeshRouter("node-a")
    r.learn_route("ss-target", "node-c")
    r.forget_node("node-c")
    msg = MeshMessage.create("ss-x", "ss-target", "hi")
    d = r.handle(msg, available_peers=["node-b"])
    # falls back to flood, node-c gone
    assert set(d.forward_to) == {"node-b"}


def test_path_records_current_node():
    r = MeshRouter("node-a")
    msg = MeshMessage.create("ss-x", "ss-unknown", "hi")
    r.handle(msg, available_peers=["node-b"])
    assert "node-a" in msg.path


def test_roundtrip_serialization():
    msg = MeshMessage.create("ss-a", "ss-b", "payload", kind="file")
    restored = MeshMessage.from_dict(msg.to_dict())
    assert restored.msg_id == msg.msg_id
    assert restored.kind == "file"
    assert restored.target_ss == "ss-b"


def test_from_dict_tolerates_missing_fields():
    msg = MeshMessage.from_dict({"from_ss": "ss-a", "target_ss": "ss-b", "text": "x"})
    assert msg.ttl > 0
    assert msg.msg_id


def test_seen_cache_eviction_bounds_memory():
    cache = SeenCache(ttl_seconds=0.0)
    for _ in range(2000):
        cache.add(new_message_id())
    # eviction triggers above 1024 with a zero TTL → stays bounded
    assert len(cache) <= 1024
