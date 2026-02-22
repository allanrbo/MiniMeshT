import asyncio
import base64
import hashlib
import json
import os
import time

from mesht_device import MeshtDevice, FROMRADIO_SCHEMA, TORADIO_SCHEMA, NAMES_TO_PORTNUMS, USER_SCHEMA
import pb
from mesht_db import MeshtDb

ROUTING_SCHEMA = [
    ("int32", "error_reason", 3),
]


class FakeTransport:
    def __init__(self):
        self._recv_q = asyncio.Queue()
        self.started = False
        self.closed = False
        self.sent = []

    async def start(self):
        self.started = True

    async def close(self):
        self.closed = True

    async def send(self, data):
        self.sent.append(data)
        tr = pb.decode(data, TORADIO_SCHEMA)
        assert isinstance(tr, dict)

        want_config_id = tr.get("want_config_id")
        if want_config_id:
            packets = [
                {"channel": {"index": 0, "settings": {"name": "General"}}},
                {"my_info": {"my_node_num": 1}},
                {"node_info": {"num": 1, "user": {"long_name": "NodeOne", "short_name": "n1"}}},
                {"config_complete_id": want_config_id},
            ]
            for fr in packets:
                await self._recv_q.put(pb.encode(fr, FROMRADIO_SCHEMA))

    async def recv(self):
        return await self._recv_q.get()


def test_meshtdb_loads_nodeinfo_metrics_from_disk(tmp_path):
    #
    # Arrange
    #
    node_path = os.path.join(str(tmp_path), "nodeinfo.jsonl")
    with open(node_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "ID": "00000001",
                "short_name": "n1",
                "long_name": "Node One",
                "user_id": "!abc",
                "hops_away": 3,
                "rx_snr": 5.5,
                "rx_rssi": -97,
                "last_heard": 1700000000,
                "battery_level": 88,
                "voltage": 3.91,
            },
            f,
            separators=(",", ":"),
        )
        f.write("\n")

    ft = FakeTransport()
    dev = MeshtDevice(ft)

    #
    # Act
    #
    db = MeshtDb(dev, str(tmp_path))
    node = db.node_info.get("00000001")

    #
    # Assert
    #
    assert node is not None
    assert node.short_name == "n1"
    assert node.long_name == "Node One"
    assert node.user_id == "!abc"
    assert node.hops_away == 3
    assert node.rx_snr == 5.5
    assert node.rx_rssi == -97
    assert node.last_heard == 1700000000
    assert node.battery_level == 88
    assert node.voltage == 3.91


def test_radio_db_persists_ingested_text(tmp_path):
    async def _run():
        #
        # Arrange
        #
        ft = FakeTransport()
        dev = MeshtDevice(ft)
        db = MeshtDb(dev, str(tmp_path))

        await db.start()

        # Inject one text FromRadio
        fr = {"packet": {"from": 1, "channel": 0, "rx_time": int(time.time()), "decoded": {"portnum": 1, "payload": b"hi"}}}
        await ft._recv_q.put(pb.encode(fr, FROMRADIO_SCHEMA))

        # Drain DB until we see that text frame
        async def _drain_until_text():
            while True:
                frm = await db.next_fromradio()
                pkt = (frm or {}).get("packet") or {}
                dec = pkt.get("decoded") or {}
                if dec.get("portnum") == 1 and (pkt.get("channel") or 0) == 0:
                    break
        await asyncio.wait_for(_drain_until_text(), timeout=1.0)

        #
        # Assert
        #
        # Messages persisted (per-channel file)
        msgs_path = os.path.join(str(tmp_path), "messages.0.jsonl")
        assert os.path.exists(msgs_path)
        with open(msgs_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        assert any('"type":"FromRadio"' in ln for ln in lines)

        await db.close()

    asyncio.run(_run())


def test_radio_db_persists_direct_text(tmp_path):
    async def _run():
        #
        # Arrange
        #
        ft = FakeTransport()
        dev = MeshtDevice(ft)
        db = MeshtDb(dev, str(tmp_path))

        await db.start()

        async def _drain_startup():
            while True:
                fr = await asyncio.wait_for(db.next_fromradio(), timeout=1.0)
                if isinstance(fr, dict) and fr.get("config_complete_id"):
                    return

        await _drain_startup()

        # Inject one direct text FromRadio (to our node 1)
        fr = {
            "packet": {
                "from": 2,
                "to": 1,
                "channel": 0,
                "rx_time": int(time.time()),
                "decoded": {"portnum": 1, "payload": b"hi"},
            }
        }
        await ft._recv_q.put(pb.encode(fr, FROMRADIO_SCHEMA))

        async def _drain_until_text():
            while True:
                frm = await db.next_fromradio()
                pkt = (frm or {}).get("packet") or {}
                dec = pkt.get("decoded") or {}
                if dec.get("portnum") == 1 and pkt.get("to") == 1:
                    break

        await asyncio.wait_for(_drain_until_text(), timeout=1.0)

        #
        # Assert
        #
        msgs_path = os.path.join(str(tmp_path), "messages.dm.00000002.jsonl")
        assert os.path.exists(msgs_path)
        with open(msgs_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        assert any('"type":"FromRadio"' in ln for ln in lines)

        await db.close()

    asyncio.run(_run())


def test_meshtdb_send_direct_text_persists(tmp_path):
    async def _run():
        #
        # Arrange
        #
        ft = FakeTransport()
        dev = MeshtDevice(ft)
        db = MeshtDb(dev, str(tmp_path))

        await db.start()

        async def _drain_startup():
            while True:
                fr = await asyncio.wait_for(db.next_fromradio(), timeout=1.0)
                if isinstance(fr, dict) and fr.get("config_complete_id"):
                    return

        await _drain_startup()

        #
        # Act
        #
        await db.send_direct_text("hello", 2)

        #
        # Assert
        #
        msgs_path = os.path.join(str(tmp_path), "messages.dm.00000002.jsonl")
        assert os.path.exists(msgs_path)
        with open(msgs_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        assert any('"type":"ToRadio"' in ln for ln in lines)

        await db.close()

    asyncio.run(_run())


def test_meshtdb_direct_messages_include_key_events(tmp_path):
    async def _run():
        #
        # Arrange
        #
        ft = FakeTransport()
        dev = MeshtDevice(ft)
        db = MeshtDb(dev, str(tmp_path))

        await db.start()

        async def _drain_startup():
            while True:
                fr = await asyncio.wait_for(db.next_fromradio(), timeout=1.0)
                if isinstance(fr, dict) and fr.get("config_complete_id"):
                    return

        await _drain_startup()

        node_num = 0x465413D7
        frame_a = {"node_info": {"num": node_num, "user": {"public_key": b"a"}}}
        frame_b = {"node_info": {"num": node_num, "user": {"public_key": b"b"}}}
        await ft._recv_q.put(pb.encode(frame_a, FROMRADIO_SCHEMA))
        await ft._recv_q.put(pb.encode(frame_b, FROMRADIO_SCHEMA))

        #
        # Act
        #
        await asyncio.wait_for(db.next_fromradio(), timeout=1.0)
        await asyncio.wait_for(db.next_fromradio(), timeout=1.0)
        node_hex = f"{node_num & 0xFFFFFFFF:08x}"
        messages = db.get_direct_messages(node_hex)

        #
        # Assert
        #
        info = None
        warning = None
        for entry in messages:
            if entry.get("type") == "KeyInfo":
                info = entry
            if entry.get("type") == "Warning":
                warning = entry
        assert info is not None
        assert warning is not None
        expected_info_fp = hashlib.sha256(b"a").hexdigest().upper()
        expected_warn_fp = hashlib.sha256(b"b").hexdigest().upper()
        assert f"Public key SHA-256: {expected_info_fp}" in info.get("text", "")
        assert f"NEW KEY SHA-256: {expected_warn_fp}" in warning.get("text", "")

        await db.close()

    asyncio.run(_run())


def test_meshtdb_dm_looks_spoofed(tmp_path):
    async def _run():
        #
        # Arrange
        #
        ft = FakeTransport()
        dev = MeshtDevice(ft)
        db = MeshtDb(dev, str(tmp_path))

        await db.start()

        async def _drain_startup():
            while True:
                fr = await asyncio.wait_for(db.next_fromradio(), timeout=1.0)
                if isinstance(fr, dict) and fr.get("config_complete_id"):
                    return

        await _drain_startup()

        node_num = 0x465413D7
        node_hex = f"{node_num & 0xFFFFFFFF:08x}"
        frame_a = {"node_info": {"num": node_num, "user": {"public_key": b"a"}}}
        frame_b = {"node_info": {"num": node_num, "user": {"public_key": b"b"}}}
        await ft._recv_q.put(pb.encode(frame_a, FROMRADIO_SCHEMA))
        await ft._recv_q.put(pb.encode(frame_b, FROMRADIO_SCHEMA))

        #
        # Act
        #
        await asyncio.wait_for(db.next_fromradio(), timeout=1.0)
        await asyncio.wait_for(db.next_fromradio(), timeout=1.0)

        #
        # Assert
        #
        assert db.dm_looks_spoofed(node_hex)
        assert not db.dm_looks_spoofed("00000001")

        await db.close()

        db2 = MeshtDb(MeshtDevice(FakeTransport()), str(tmp_path))
        assert db2.dm_looks_spoofed(node_hex)

    asyncio.run(_run())


def test_meshtdb_updates_rx_snr_from_packets(tmp_path):
    async def _run():
        #
        # Arrange
        #
        ft = FakeTransport()
        dev = MeshtDevice(ft)
        db = MeshtDb(dev, str(tmp_path))

        await db.start()

        async def _drain_startup():
            while True:
                fr = await asyncio.wait_for(db.next_fromradio(), timeout=1.0)
                if isinstance(fr, dict) and fr.get("config_complete_id"):
                    return

        await _drain_startup()

        node_num = 0xCAFEBABE
        snr = -7.25
        frame = {
            "packet": {
                "from": node_num,
                "to": 0,
                "rx_snr": snr,
                "rx_rssi": -113,
                "decoded": {
                    "portnum": NAMES_TO_PORTNUMS["TEXT_MESSAGE_APP"],
                    "payload": b"hello",
                },
            }
        }
        await ft._recv_q.put(pb.encode(frame, FROMRADIO_SCHEMA))

        #
        # Act
        #
        await asyncio.wait_for(db.next_fromradio(), timeout=1.0)

        #
        # Assert
        #
        node_hex = f"{node_num & 0xFFFFFFFF:08x}"
        node = db.node_info.get(node_hex)
        assert node is not None
        assert node.rx_snr == snr

        await db.close()

    asyncio.run(_run())


def test_meshtdb_updates_rx_rssi_from_packets(tmp_path):
    async def _run():
        #
        # Arrange
        #
        ft = FakeTransport()
        dev = MeshtDevice(ft)
        db = MeshtDb(dev, str(tmp_path))

        await db.start()

        async def _drain_startup():
            while True:
                fr = await asyncio.wait_for(db.next_fromradio(), timeout=1.0)
                if isinstance(fr, dict) and fr.get("config_complete_id"):
                    return

        await _drain_startup()

        node_num = 0xD00DFEED
        rssi = -101
        frame = {
            "packet": {
                "from": node_num,
                "to": 0,
                "rx_rssi": rssi,
                "decoded": {
                    "portnum": NAMES_TO_PORTNUMS["TEXT_MESSAGE_APP"],
                    "payload": b"ping",
                },
            }
        }
        await ft._recv_q.put(pb.encode(frame, FROMRADIO_SCHEMA))

        #
        # Act
        #
        await asyncio.wait_for(db.next_fromradio(), timeout=1.0)

        #
        # Assert
        #
        node_hex = f"{node_num & 0xFFFFFFFF:08x}"
        node = db.node_info.get(node_hex)
        assert node is not None
        assert node.rx_rssi == rssi

        await db.close()

    asyncio.run(_run())


def test_meshtdb_startup_channel_seen(tmp_path):
    async def _run():
        #
        # Arrange
        #
        ft = FakeTransport()
        dev = MeshtDevice(ft)
        db = MeshtDb(dev, str(tmp_path))

        #
        # Act and assert: after start(), we should observe a channel frame via next_fromradio
        #
        await db.start()
        async def _drain_until_channel():
            while True:
                fr = await db.next_fromradio()
                if isinstance(fr, dict) and fr.get("channel"):
                    return
        await asyncio.wait_for(_drain_until_channel(), timeout=1.0)
        await db.close()

    asyncio.run(_run())


def test_meshtdb_persists_startup_nodeinfo_log(tmp_path):
    async def _run():
        #
        # Arrange
        #
        ft = FakeTransport()
        dev = MeshtDevice(ft)
        db = MeshtDb(dev, str(tmp_path))

        #
        # Act
        #
        await db.start()
        # Drain startup frames so nodeinfo file is written
        async def _drain_startup():
            while True:
                fr = await asyncio.wait_for(db.next_fromradio(), timeout=1.0)
                if isinstance(fr, dict) and fr.get("config_complete_id"):
                    break
        await _drain_startup()

        #
        # Assert
        #
        node_path = os.path.join(str(tmp_path), "nodeinfo.jsonl")
        with open(node_path, "r", encoding="utf-8") as f:
            entries = [json.loads(ln) for ln in f if ln.strip()]
        assert entries and all("raw_packet" in e for e in entries)
        # Each entry has timestamps and friendly fields
        assert all(isinstance(e.get("ts"), int) for e in entries)
        assert all(isinstance(e.get("tsh"), str) and e.get("tsh") for e in entries)
        assert any((e.get("ID") and e.get("short_name") == "n1" and e.get("long_name") == "NodeOne") for e in entries)

        await db.close()

    asyncio.run(_run())


def test_radio_db_get_messages_filters_and_orders(tmp_path):
    async def _run():
        #
        # Arrange
        #
        ft = FakeTransport()
        dev = MeshtDevice(ft)
        db = MeshtDb(dev, str(tmp_path))
        await db.start()

        now = int(time.time())
        msgs = [
            {"packet": {"from": 2, "channel": 0, "rx_time": now - 10, "decoded": {"portnum": 1, "payload": b"m1"}}},
            {"packet": {"from": 3, "channel": 1, "rx_time": now - 8, "decoded": {"portnum": 1, "payload": b"m2"}}},
            {"packet": {"from": 2, "channel": 0, "rx_time": now - 5, "decoded": {"portnum": 1, "payload": b"m3"}}},
        ]
        for fr in msgs:
            await ft._recv_q.put(pb.encode(fr, FROMRADIO_SCHEMA))

        # Drain frames until all three text messages are ingested
        async def _drain_texts():
            seen = 0
            while seen < len(msgs):
                fr = await db.next_fromradio()
                pkt = (fr or {}).get("packet") or {}
                dec = pkt.get("decoded") or {}
                if dec.get("portnum") == 1:
                    seen += 1
        await asyncio.wait_for(_drain_texts(), timeout=1.0)

        #
        # Act
        #
        last_two = db.get_messages(channel=0)

        #
        # Assert
        #
        assert len(last_two) == 2
        texts = [ m.get("text") for m in last_two ]
        assert texts == ["m1", "m3"]

        await db.close()

    asyncio.run(_run())


def test_radio_db_send_text_logs(tmp_path):
    async def _run():
        #
        # Arrange
        #
        ft = FakeTransport()
        dev = MeshtDevice(ft)
        db = MeshtDb(dev, str(tmp_path))

        await db.start()
        # Drain startup frames so our node/user metadata is known
        async def _drain_startup():
            while True:
                fr = await asyncio.wait_for(db.next_fromradio(), timeout=1.0)
                if isinstance(fr, dict) and fr.get("config_complete_id"):
                    break
        await _drain_startup()
        # channel 0 exists due to FakeTransport start handshake
        await db.send_text("hello", 0)

        #
        # Assert
        #
        with open(os.path.join(str(tmp_path), "messages.0.jsonl"), "r", encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
        assert any('"type":"ToRadio"' in ln for ln in lines)
        # The ToRadio entry should use our node's name when known
        import json
        toradio_lines = [json.loads(ln) for ln in lines if '"type":"ToRadio"' in ln]
        assert toradio_lines, "expected at least one ToRadio entry"
        last = toradio_lines[-1]
        assert last.get("sender_long_name") == "NodeOne"
        assert last.get("sender_short_name") == "n1"
        # Human readable timestamp should be present
        assert isinstance(last.get("tsh"), str) and last.get("tsh")
        # 'from' should be hex-formatted string now
        assert isinstance(last.get("from"), str)
        assert len(last.get("from")) == 8

        await db.close()

    asyncio.run(_run())


def test_meshtdb_updates_sent_message_status_to_ack(tmp_path):
    async def _run():
        #
        # Arrange
        #
        ft = FakeTransport()
        dev = MeshtDevice(ft)
        db = MeshtDb(dev, str(tmp_path))

        await db.start()

        async def _drain_startup():
            while True:
                fr = await asyncio.wait_for(db.next_fromradio(), timeout=1.0)
                if isinstance(fr, dict) and fr.get("config_complete_id"):
                    return

        await _drain_startup()

        pkt = await db.send_text("hello", 0)
        routing_payload = pb.encode({"error_reason": 0}, ROUTING_SCHEMA)
        fr = {
            "packet": {
                "from": 1,
                "to": 1,
                "rx_time": int(time.time()),
                "decoded": {
                    "portnum": NAMES_TO_PORTNUMS["ROUTING_APP"],
                    "request_id": pkt.get("id"),
                    "payload": routing_payload,
                },
            }
        }
        await ft._recv_q.put(pb.encode(fr, FROMRADIO_SCHEMA))

        #
        # Act
        #
        await asyncio.wait_for(db.next_fromradio(), timeout=1.0)

        #
        # Assert
        #
        with open(os.path.join(str(tmp_path), "messages.0.jsonl"), "r", encoding="utf-8") as f:
            entries = [json.loads(ln) for ln in f if ln.strip()]
        sent_entries = [e for e in entries if e.get("type") == "ToRadio" and e.get("message_id") == pkt.get("id")]
        status_entries = [e for e in entries if e.get("type") == "DeliveryStatus" and e.get("message_id") == pkt.get("id")]
        assert sent_entries
        assert "delivery_status" not in sent_entries[-1]
        assert status_entries
        assert status_entries[-1].get("delivery_status") == "ack"

        messages = db.get_messages(channel=0)
        resolved = [m for m in messages if m.get("type") == "ToRadio" and m.get("message_id") == pkt.get("id")]
        assert resolved
        assert resolved[-1].get("delivery_status") == "ack"
        assert resolved[-1].get("delivery_ack_count") == 1

        await db.close()

    asyncio.run(_run())


def test_meshtdb_updates_sent_message_status_to_failed(tmp_path):
    async def _run():
        #
        # Arrange
        #
        ft = FakeTransport()
        dev = MeshtDevice(ft)
        db = MeshtDb(dev, str(tmp_path))

        await db.start()

        async def _drain_startup():
            while True:
                fr = await asyncio.wait_for(db.next_fromradio(), timeout=1.0)
                if isinstance(fr, dict) and fr.get("config_complete_id"):
                    return

        await _drain_startup()

        pkt = await db.send_direct_text("hello", 2)
        routing_payload = pb.encode({"error_reason": 6}, ROUTING_SCHEMA)
        fr = {
            "packet": {
                "from": 1,
                "to": 1,
                "rx_time": int(time.time()),
                "decoded": {
                    "portnum": NAMES_TO_PORTNUMS["ROUTING_APP"],
                    "request_id": pkt.get("id"),
                    "payload": routing_payload,
                },
            }
        }
        await ft._recv_q.put(pb.encode(fr, FROMRADIO_SCHEMA))

        #
        # Act
        #
        await asyncio.wait_for(db.next_fromradio(), timeout=1.0)

        #
        # Assert
        #
        with open(os.path.join(str(tmp_path), "messages.dm.00000002.jsonl"), "r", encoding="utf-8") as f:
            entries = [json.loads(ln) for ln in f if ln.strip()]
        sent_entries = [e for e in entries if e.get("type") == "ToRadio" and e.get("message_id") == pkt.get("id")]
        status_entries = [e for e in entries if e.get("type") == "DeliveryStatus" and e.get("message_id") == pkt.get("id")]
        assert sent_entries
        assert "delivery_status" not in sent_entries[-1]
        assert status_entries
        assert status_entries[-1].get("delivery_status") == "failed"

        messages = db.get_direct_messages("00000002")
        resolved = [m for m in messages if m.get("type") == "ToRadio" and m.get("message_id") == pkt.get("id")]
        assert resolved
        assert resolved[-1].get("delivery_status") == "failed"
        assert resolved[-1].get("delivery_ack_count") == 0

        await db.close()

    asyncio.run(_run())


def test_meshtdb_channel_ack_count_dedupes_nodes(tmp_path):
    async def _run():
        #
        # Arrange
        #
        ft = FakeTransport()
        dev = MeshtDevice(ft)
        db = MeshtDb(dev, str(tmp_path))

        await db.start()

        async def _drain_startup():
            while True:
                fr = await asyncio.wait_for(db.next_fromradio(), timeout=1.0)
                if isinstance(fr, dict) and fr.get("config_complete_id"):
                    return

        await _drain_startup()

        pkt = await db.send_text("hello", 0)
        routing_payload = pb.encode({"error_reason": 0}, ROUTING_SCHEMA)
        acks = [
            {
                "packet": {
                    "from": 2,
                    "to": 1,
                    "rx_time": int(time.time()),
                    "decoded": {
                        "portnum": NAMES_TO_PORTNUMS["ROUTING_APP"],
                        "request_id": pkt.get("id"),
                        "payload": routing_payload,
                    },
                }
            },
            {
                "packet": {
                    "from": 2,
                    "to": 1,
                    "rx_time": int(time.time()),
                    "decoded": {
                        "portnum": NAMES_TO_PORTNUMS["ROUTING_APP"],
                        "request_id": pkt.get("id"),
                        "payload": routing_payload,
                    },
                }
            },
            {
                "packet": {
                    "from": 3,
                    "to": 1,
                    "rx_time": int(time.time()),
                    "decoded": {
                        "portnum": NAMES_TO_PORTNUMS["ROUTING_APP"],
                        "request_id": pkt.get("id"),
                        "payload": routing_payload,
                    },
                }
            },
        ]
        for fr in acks:
            await ft._recv_q.put(pb.encode(fr, FROMRADIO_SCHEMA))

        #
        # Act
        #
        await asyncio.wait_for(db.next_fromradio(), timeout=1.0)
        await asyncio.wait_for(db.next_fromradio(), timeout=1.0)
        await asyncio.wait_for(db.next_fromradio(), timeout=1.0)

        #
        # Assert
        #
        messages = db.get_messages(channel=0)
        resolved = [m for m in messages if m.get("type") == "ToRadio" and m.get("message_id") == pkt.get("id")]
        assert resolved
        assert resolved[-1].get("delivery_status") == "ack"
        assert resolved[-1].get("delivery_ack_count") == 2

        await db.close()

    asyncio.run(_run())


def test_meshtdb_restart_rebuilds_message_path_index_in_memory(tmp_path):
    async def _run():
        #
        # Arrange
        #
        ft1 = FakeTransport()
        dev1 = MeshtDevice(ft1)
        db1 = MeshtDb(dev1, str(tmp_path))

        await db1.start()

        async def _drain_startup(db):
            while True:
                fr = await asyncio.wait_for(db.next_fromradio(), timeout=1.0)
                if isinstance(fr, dict) and fr.get("config_complete_id"):
                    return

        await _drain_startup(db1)
        pkt = await db1.send_text("hello", 0)
        await db1.close()

        index_path = os.path.join(str(tmp_path), "message_index.jsonl")
        assert not os.path.exists(index_path)

        ft2 = FakeTransport()
        dev2 = MeshtDevice(ft2)
        db2 = MeshtDb(dev2, str(tmp_path))
        await db2.start()
        await _drain_startup(db2)

        routing_payload = pb.encode({"error_reason": 0}, ROUTING_SCHEMA)
        fr = {
            "packet": {
                "from": 2,
                "to": 1,
                "rx_time": int(time.time()),
                "decoded": {
                    "portnum": NAMES_TO_PORTNUMS["ROUTING_APP"],
                    "request_id": pkt.get("id"),
                    "payload": routing_payload,
                },
            }
        }
        await ft2._recv_q.put(pb.encode(fr, FROMRADIO_SCHEMA))

        #
        # Act
        #
        await asyncio.wait_for(db2.next_fromradio(), timeout=1.0)

        #
        # Assert
        #
        with open(os.path.join(str(tmp_path), "messages.0.jsonl"), "r", encoding="utf-8") as f:
            entries = [json.loads(ln) for ln in f if ln.strip()]
        status_entries = [e for e in entries if e.get("type") == "DeliveryStatus" and e.get("message_id") == pkt.get("id")]
        assert status_entries
        assert status_entries[-1].get("delivery_status") == "ack"

        await db2.close()

    asyncio.run(_run())


def test_nodeinfo_compaction_keeps_first_changes_newest(tmp_path):
    async def _run():
        #
        # Arrange
        #
        ft = FakeTransport()
        dev = MeshtDevice(ft)
        db = MeshtDb(dev, str(tmp_path))

        await db.start()
        # Drain startup frames so the initial node_info from handshake is ingested
        async def _drain_startup():
            while True:
                fr = await asyncio.wait_for(db.next_fromradio(), timeout=1.0)
                if isinstance(fr, dict) and fr.get("config_complete_id"):
                    break
        await _drain_startup()

        # Build a sequence of node_info updates for the same node (ID 00000001)
        frames = [
            {"node_info": {"num": 1, "user": {"long_name": "NodeOne", "short_name": "n1", "public_key": b"a"}}},
            {"node_info": {"num": 1, "user": {"long_name": "NodeOne", "short_name": "n1", "public_key": b"a"}}},
            {"node_info": {"num": 1, "user": {"long_name": "NodeOne", "short_name": "n1x", "public_key": b"a"}}},
            {"node_info": {"num": 1, "user": {"long_name": "NodeOneX", "short_name": "n1x", "public_key": b"a"}}},
            {"node_info": {"num": 1, "user": {"long_name": "NodeOneX", "short_name": "n1x", "public_key": b"b"}}},
            {"node_info": {"num": 1, "user": {"long_name": "NodeOneX", "short_name": "n1x", "public_key": b"b"}}},
        ]
        for fr in frames:
            await ft._recv_q.put(pb.encode(fr, FROMRADIO_SCHEMA))

        # Drain until we've ingested all node_info frames
        async def _drain(n):
            seen = 0
            while seen < n:
                fr = await db.next_fromradio()
                if isinstance(fr, dict) and fr.get("node_info"):
                    seen += 1
        await asyncio.wait_for(_drain(len(frames)), timeout=1.0)

        #
        # Assert
        #
        node_path = os.path.join(str(tmp_path), "nodeinfo.jsonl")
        with open(node_path, "r", encoding="utf-8") as f:
            entries = [json.loads(ln) for ln in f if ln.strip()]

        # Filter for our node and map to (short,long,public_key_base64)
        entries = [e for e in entries if e.get("ID") == "00000001"]
        def _tup(e):
            return (
                e.get("short_name") or "",
                e.get("long_name") or "",
                e.get("public_key") or "",
            )

        got = [ _tup(e) for e in entries ]
        expected = [
            ("n1", "NodeOne", ""),                 # first seen during startup handshake
            ("n1", "NodeOne", "YQ=="),              # change: public_key a
            ("n1x", "NodeOne", "YQ=="),            # change: short_name
            ("n1x", "NodeOneX", "YQ=="),           # change: long_name
            ("n1x", "NodeOneX", "Yg=="),           # newest (and change) kept
            ("n1x", "NodeOneX", "Yg=="),           # newest duplicate
        ]
        assert got == expected

        await db.close()

    asyncio.run(_run())


def test_nodeinfo_compaction_keeps_first_and_newest_if_identical(tmp_path):
    async def _run():
        #
        # Arrange
        #
        ft = FakeTransport()
        dev = MeshtDevice(ft)
        db = MeshtDb(dev, str(tmp_path))

        await db.start()

        async def _drain_startup():
            while True:
                fr = await asyncio.wait_for(db.next_fromradio(), timeout=1.0)
                if isinstance(fr, dict) and fr.get("config_complete_id"):
                    return

        await _drain_startup()

        node_num = 0x11111111
        frame = {"node_info": {"num": node_num, "user": {"long_name": "Same", "short_name": "same", "public_key": b"a"}}}
        await ft._recv_q.put(pb.encode(frame, FROMRADIO_SCHEMA))
        await ft._recv_q.put(pb.encode(frame, FROMRADIO_SCHEMA))
        await ft._recv_q.put(pb.encode(frame, FROMRADIO_SCHEMA))

        #
        # Act
        #
        await asyncio.wait_for(db.next_fromradio(), timeout=1.0)
        await asyncio.wait_for(db.next_fromradio(), timeout=1.0)
        await asyncio.wait_for(db.next_fromradio(), timeout=1.0)

        #
        # Assert
        #
        node_hex = f"{node_num & 0xFFFFFFFF:08x}"
        node_path = os.path.join(str(tmp_path), "nodeinfo.jsonl")
        with open(node_path, "r", encoding="utf-8") as f:
            entries = [json.loads(ln) for ln in f if ln.strip()]
        entries = [e for e in entries if e.get("ID") == node_hex]
        assert len(entries) == 2
        assert entries[0].get("public_key") == "YQ=="
        assert entries[-1].get("public_key") == "YQ=="

        await db.close()

    asyncio.run(_run())


def test_nodeinfo_compaction_keeps_reversion_change_marker(tmp_path, monkeypatch):
    async def _run():
        #
        # Arrange
        #
        tick = {"value": 2000000000}

        def _fake_time():
            tick["value"] += 1
            return tick["value"]

        monkeypatch.setattr("mesht_db.time.time", _fake_time)

        ft = FakeTransport()
        dev = MeshtDevice(ft)
        db = MeshtDb(dev, str(tmp_path))

        await db.start()

        async def _drain_startup():
            while True:
                fr = await asyncio.wait_for(db.next_fromradio(), timeout=1.0)
                if isinstance(fr, dict) and fr.get("config_complete_id"):
                    return

        await _drain_startup()

        node_num = 0x22222222
        node_hex = f"{node_num & 0xFFFFFFFF:08x}"
        frames = [
            {"node_info": {"num": node_num, "user": {"public_key": b"a"}}},
            {"node_info": {"num": node_num, "user": {"public_key": b"b"}}},
            {"node_info": {"num": node_num, "user": {"public_key": b"a"}}},
            {"node_info": {"num": node_num, "user": {"public_key": b"a"}}},
        ]
        for fr in frames:
            await ft._recv_q.put(pb.encode(fr, FROMRADIO_SCHEMA))

        async def _drain(n):
            seen = 0
            while seen < n:
                fr = await db.next_fromradio()
                if isinstance(fr, dict) and fr.get("node_info"):
                    seen += 1

        #
        # Act
        #
        await asyncio.wait_for(_drain(len(frames)), timeout=1.0)
        events = db.get_direct_messages(node_hex)

        #
        # Assert
        #
        node_path = os.path.join(str(tmp_path), "nodeinfo.jsonl")
        with open(node_path, "r", encoding="utf-8") as f:
            entries = [json.loads(ln) for ln in f if ln.strip()]
        entries = [e for e in entries if e.get("ID") == node_hex]
        assert [e.get("public_key") for e in entries] == ["YQ==", "Yg==", "YQ==", "YQ=="]

        warnings = [e for e in events if e.get("type") == "Warning"]
        assert len(warnings) == 2
        assert warnings[-1].get("ts") == entries[2].get("ts")
        assert warnings[-1].get("ts") != entries[3].get("ts")

        await db.close()

    asyncio.run(_run())


def test_meshtdb_ingests_nodeinfo_packets(tmp_path):
    async def _run():
        #
        # Arrange
        #
        ft = FakeTransport()
        dev = MeshtDevice(ft)
        db = MeshtDb(dev, str(tmp_path))

        await db.start()

        async def _drain_startup():
            while True:
                fr = await asyncio.wait_for(db.next_fromradio(), timeout=1.0)
                if isinstance(fr, dict) and fr.get("config_complete_id"):
                    return
        await _drain_startup()

        node_num = 0x12345678
        now = int(time.time())
        public_key = b"\x01" * 32
        payload = pb.encode({
            "id": "!unit-test",
            "long_name": "Unit Test",
            "short_name": "ut",
            "public_key": public_key,
        }, USER_SCHEMA)
        frame = {
            "packet": {
                "from": node_num,
                "to": 0,
                "rx_time": now,
                "rx_snr": 9.5,
                "rx_rssi": -107,
                "decoded": {
                    "portnum": NAMES_TO_PORTNUMS["NODEINFO_APP"],
                    "payload": payload,
                },
            }
        }
        await ft._recv_q.put(pb.encode(frame, FROMRADIO_SCHEMA))

        #
        # Act
        #
        await asyncio.wait_for(db.next_fromradio(), timeout=1.0)

        #
        # Assert
        #
        node_hex = f"{node_num & 0xFFFFFFFF:08x}"
        node = db.node_info.get(node_hex)
        assert node is not None
        assert node.short_name == "ut"
        assert node.long_name == "Unit Test"
        assert node.node_id == node_hex
        assert node.user_id == "!unit-test"
        pk_b64 = base64.b64encode(public_key).decode("ascii")
        assert node.public_key == pk_b64
        assert node.last_heard == now

        node_path = os.path.join(str(tmp_path), "nodeinfo.jsonl")
        with open(node_path, "r", encoding="utf-8") as f:
            entries = [json.loads(ln) for ln in f if ln.strip()]
        matching = [e for e in entries if e.get("ID") == node_hex]
        assert matching
        assert matching[-1].get("public_key") == pk_b64
        assert matching[-1].get("user_id") == "!unit-test"
        assert matching[-1].get("last_heard") == now
        assert matching[-1].get("rx_snr") == 9.5
        assert matching[-1].get("rx_rssi") == -107

        await db.close()

        # Assert reload restores fields we persisted in nodeinfo.jsonl
        dev2 = MeshtDevice(FakeTransport())
        db2 = MeshtDb(dev2, str(tmp_path))
        cached = db2.node_info.get(node_hex)
        assert cached is not None
        assert cached.short_name == "ut"
        assert cached.long_name == "Unit Test"
        assert cached.node_id == node_hex
        assert cached.user_id == "!unit-test"
        assert cached.public_key == pk_b64
        assert cached.last_heard == now
        assert cached.rx_snr == 9.5
        assert cached.rx_rssi == -107

    asyncio.run(_run())
