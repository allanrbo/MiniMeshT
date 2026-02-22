import asyncio
import base64
import hashlib
import json
import os
import time
import logging
logger = logging.getLogger(__name__)

import pb
from mesht_device import TORADIO_SCHEMA, FROMRADIO_SCHEMA, PORTNUMS, Channel, USER_SCHEMA
from packet_parsing import parse_text_packet, parse_delivery_packet, direct_peer_hex


def _b64(s):
    return base64.b64encode(s).decode("ascii")


def _append_jsonl(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))
        f.write("\n")


def _fmt_ts(ts):
    # Human-readable UTC time for convenience when inspecting files
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(int(ts or 0)))


def _load_jsonl(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


class NodeInfo:
    def __init__(self, node_id):
        self.node_id = node_id or ""
        self.short_name = ""
        self.long_name = ""
        self.user_id = ""
        self.public_key = ""
        self.hops_away = None
        self.rx_snr = None
        self.rx_rssi = None
        self.last_heard = None
        self.battery_level = None
        self.voltage = None

    def apply_user_proto_dict(self, data):
        if not data:
            return self
        short = data.get("short_name")
        if short:
            self.short_name = short
        long_name = data.get("long_name")
        if long_name:
            self.long_name = long_name
        user_id = data.get("id")
        if user_id:
            self.user_id = user_id
        public_key_bytes = data.get("public_key")
        if public_key_bytes is not None:
            self.public_key = base64.b64encode(public_key_bytes).decode("ascii")

        return self

    def display_name(self):
        return self.long_name or self.short_name or self.user_id or (self.node_id.upper() if self.node_id else "")


class MeshtDb:
    def __init__(self, device, data_dir):
        self.device = device
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)
        self._last_lora = None
        # Node info cache keyed by 8-char lowercase hex ID
        self._node_info = {}
        # Message ID -> jsonl file path for fast delivery-status appends.
        self._message_path_by_id = {}
        # Suppress nodeinfo compaction during startup handshake until config_complete_id
        self._in_startup = True

        self._load_node_info()
        self._load_message_path_index()

    @property
    def node_info(self):
        return self._node_info

    async def start(self):
        logger.debug("MeshtDb.start: starting device")
        self._in_startup = True
        await self.device.start()
        logger.debug("MeshtDb.start: device started")

    async def close(self):
        await self.device.close()
        logger.debug("MeshtDb.close: device closed")

    async def send_text(self, text, channel_index):
        pkt = await self.device.send_text(text, channel_index)
        # Persist ToRadio entry with base64 raw proto
        entry = self._make_toradio_entry(pkt)
        msg_path = os.path.join(self.data_dir, f"messages.{int(channel_index)}.jsonl")
        _append_jsonl(msg_path, entry)
        self._remember_message_path(pkt.get("id"), msg_path)
        return pkt

    async def send_direct_text(self, text, destination):
        pkt = await self.device.send_direct_text(text, destination)
        entry = self._make_toradio_entry(pkt)
        node_hex = f"{int(destination) & 0xFFFFFFFF:08x}"
        msg_path = self._direct_messages_path(node_hex)
        _append_jsonl(msg_path, entry)
        self._remember_message_path(pkt.get("id"), msg_path)
        return pkt

    async def next_fromradio(self):
        # Read from the device, ingest, persist, and return decoded frame
        fr, raw = await self.device.recv()
        # if not (self._in_startup and fr.get("node_info")):
        self._handle_from_radio(fr, raw)
        return fr

    async def wait_for_config_complete(self):
        # Wait for the startup config dump to finish.
        if not self._in_startup:
            return
        while True:
            fr = await self.next_fromradio()
            if isinstance(fr, dict) and fr.get("config_complete_id") is not None:
                return

    def _handle_from_radio(self, fr, raw):
        # Route and persist a single FromRadio dict
        # Detect end of startup handshake
        if isinstance(fr, dict) and fr.get("config_complete_id") is not None:
            logger.debug("Got config_complete_id")
            self._in_startup = False
            try:
                self._compact_nodeinfo()
            except Exception:
                logger.exception("nodeinfo compaction after startup failed")
            return
        if fr.get("packet"):
            pkt = fr.get("packet") or {}
            decoded = pkt.get("decoded")
            port_name = PORTNUMS.get((decoded or {}).get("portnum")) if decoded else None
            if port_name == "ROUTING_APP":
                self._update_delivery_status_from_routing_packet(pkt)
            elif port_name == "TEXT_MESSAGE_APP":
                entry = self._make_fromradio_entry(fr, raw)
                peer_hex = direct_peer_hex(pkt, self.device.my_node_id)
                if peer_hex:
                    msg_path = self._direct_messages_path(peer_hex)
                    _append_jsonl(msg_path, entry)
                else:
                    ch_idx = pkt.get("channel") or 0
                    msg_path = os.path.join(self.data_dir, f"messages.{int(ch_idx)}.jsonl")
                    _append_jsonl(msg_path, entry)
            elif port_name == "NODEINFO_APP":
                self._ingest_packet_nodeinfo(pkt, raw)

            # Update last-heard timestamp, etc., for the sender in our node cache
            try:
                sender = pkt.get("from")
                if sender is None:
                    return
                hex_id = f"{sender & 0xFFFFFFFF:08x}"
                rx_time = pkt.get("rx_time")
                ts = rx_time if rx_time else int(time.time())
                node = self._node_info.setdefault(hex_id, NodeInfo(hex_id))
                node.last_heard = ts
                rx_snr = pkt.get("rx_snr")
                if rx_snr is not None:
                    node.rx_snr = rx_snr
                rx_rssi = pkt.get("rx_rssi")
                if rx_rssi is not None:
                    node.rx_rssi = rx_rssi
            except Exception:
                # Non-fatal; best effort to enrich last_heard
                pass
            return
        if fr.get("node_info"):
            ni = fr.get("node_info") or {}
            self._ingest_top_level_nodeinfo(ni, raw)
            return
        if fr.get("config"):
            cfg = fr.get("config") or {}
            lora = cfg.get("lora") if isinstance(cfg, dict) else None
            if lora:
                self._last_lora = lora
            return

    @property
    def lora_config(self):
        return self._last_lora

    def _update_delivery_status_from_routing_packet(self, pkt):
        # Parse routing result and append an immutable delivery-status event.
        delivery = parse_delivery_packet(pkt)
        if delivery is None:
            return

        self._append_delivery_status_event(
            delivery.request_id,
            delivery.status,
            delivery.error_reason,
            pkt.get("rx_time"),
            delivery.reporter_hex,
        )

    def _append_delivery_status_event(self, message_id, status, error_reason, event_ts, reporter_hex):
        # Find the chat log containing this sent message ID.
        path = self._message_path_by_id.get(int(message_id))
        if path is None:
            return

        # Persist an immutable status event in append-only form.
        ts = int(event_ts) if event_ts else int(time.time())
        entry = {
            "type": "DeliveryStatus",
            "message_id": int(message_id),
            "delivery_status": status,
            "error_reason": int(error_reason),
            "ts": ts,
            "tsh": _fmt_ts(ts),
        }
        if reporter_hex:
            entry["from"] = reporter_hex
        _append_jsonl(path, entry)

    def _remember_message_path(self, message_id, path):
        if message_id is None:
            return
        self._message_path_by_id[int(message_id)] = path

    def _load_message_path_index(self):
        # Build message-id -> file map once at startup.
        try:
            names = os.listdir(self.data_dir)
        except Exception:
            return

        for name in names:
            if not name.startswith("messages.") or not name.endswith(".jsonl"):
                continue
            path = os.path.join(self.data_dir, name)
            for entry in _load_jsonl(path):
                if entry.get("type") != "ToRadio":
                    continue
                message_id = entry.get("message_id")
                if message_id is None:
                    continue
                self._remember_message_path(message_id, path)

    def _load_node_info(self):
        # Load node info history and build last-known cache
        path = os.path.join(self.data_dir, "nodeinfo.jsonl")
        for entry in _load_jsonl(path):
            node_id = entry.get("ID")
            if not node_id:
                continue

            node = self._node_info.setdefault(node_id, NodeInfo(node_id))
            node.node_id = node_id

            short_name = entry.get("short_name")
            if short_name:
                node.short_name = short_name
            long_name = entry.get("long_name")
            if long_name:
                node.long_name = long_name
            public_key = entry.get("public_key")
            if public_key:
                node.public_key = public_key
            user_id = entry.get("user_id")
            if user_id:
                node.user_id = user_id
            hops_away = entry.get("hops_away")
            if hops_away is not None:
                node.hops_away = hops_away
            rx_snr = entry.get("rx_snr")
            if rx_snr is not None:
                node.rx_snr = rx_snr
            rx_rssi = entry.get("rx_rssi")
            if rx_rssi is not None:
                node.rx_rssi = rx_rssi
            last_heard = entry.get("last_heard")
            if last_heard is not None:
                node.last_heard = last_heard
            battery_level = entry.get("battery_level")
            if battery_level is not None:
                node.battery_level = battery_level
            voltage = entry.get("voltage")
            if voltage is not None:
                node.voltage = voltage

    def get_messages(self, channel=None):
        if channel is not None:
            path = os.path.join(self.data_dir, f"messages.{int(channel)}.jsonl")
            lines = _load_jsonl(path)
        else:
            lines = []
        return self._filter_text_entries(lines)

    def get_direct_messages(self, node_id):
        node_hex = (node_id or "").lower()
        path = self._direct_messages_path(node_hex)
        lines = _load_jsonl(path)
        messages = self._filter_text_entries(lines)
        key_events = self._build_key_events(node_hex)
        combined = []
        order = 0
        for entry in messages:
            combined.append((int(entry.get("ts") or 0), order, entry))
            order += 1
        for entry in key_events:
            combined.append((int(entry.get("ts") or 0), order, entry))
            order += 1
        combined.sort(key=lambda item: (item[0], item[1]))
        return [entry for _ts, _order, entry in combined]

    def dm_looks_spoofed(self, node_id):
        # If this node's public key changed in nodeinfo history, treat the DM
        # identity as suspicious.
        node_hex = (node_id or "").lower()
        if not node_hex:
            return False
        path = os.path.join(self.data_dir, "nodeinfo.jsonl")
        previous_key = None
        for entry in _load_jsonl(path):
            if (entry.get("ID") or "").lower() != node_hex:
                continue
            public_key = entry.get("public_key") or ""
            if not public_key:
                continue
            if previous_key and public_key != previous_key:
                return True
            previous_key = public_key
        return False

    def get_local_channel_indices(self):
        # Discover channels by scanning messages.<n>.jsonl files
        try:
            names = os.listdir(self.data_dir)
        except Exception:
            names = []
        out = []
        for n in names:
            if not n.startswith("messages.") or not n.endswith(".jsonl"):
                continue
            mid = n[len("messages."):-len(".jsonl")]
            try:
                out.append(int(mid))
            except Exception:
                pass
        return sorted({i for i in out})

    def get_direct_nodes(self):
        # Discover direct-message peers by scanning messages.dm.<id>.jsonl files
        try:
            names = os.listdir(self.data_dir)
        except Exception:
            names = []
        out = []
        for n in names:
            if not n.startswith("messages.dm.") or not n.endswith(".jsonl"):
                continue
            mid = n[len("messages.dm."):-len(".jsonl")]
            if mid:
                out.append(mid.lower())
        return sorted({i for i in out})

    def get_channels(self):
        # Prefer live device channels; fall back to numeric channels from files
        chs = list(self.device.get_channels())
        if chs:
            return chs
        return [Channel(i, None, 1) for i in self.get_local_channel_indices()]

    def _make_fromradio_entry(self, fr, raw):
        pkt = fr.get("packet") or {}
        rx_time = pkt.get("rx_time")
        sender = pkt.get("from")
        # Resolve names from node_info cache for readability in the jsonl file
        sender_hex = f"{sender & 0xFFFFFFFF:08x}"
        node = self.node_info.get(sender_hex)
        short_name = node.short_name if node else ""
        long_name = node.long_name if node else ""
        text = ""
        parsed_text = parse_text_packet(pkt)
        if parsed_text is not None:
            text = parsed_text.text or ""
        return {
            "type": "FromRadio",
            "from": sender_hex,
            "ts": rx_time if rx_time else int(time.time()),
            "tsh": _fmt_ts(rx_time if rx_time else int(time.time())),
            "text": text,
            "sender_short_name": short_name,
            "sender_long_name": long_name,
            "raw_packet": _b64(raw),
        }

    def _make_toradio_entry(self, meshpacket):
        # Use our node info (if known) for friendly sender labels on ToRadio
        me = self._node_info.get(self.device.my_node_id)
        short_name = me.short_name if me else ""
        long_name = me.long_name if me else ""

        # Encode exactly what was sent so logs can be replayed/decoded later.
        pkt_for_wire = {"packet": dict(meshpacket)}
        raw = pb.encode(pkt_for_wire, TORADIO_SCHEMA)

        # Extract a readable message body when this is a text packet.
        text = ""
        parsed_text = parse_text_packet(meshpacket)
        if parsed_text is not None:
            text = parsed_text.text or ""

        # Mark new outbound packets as waiting until routing status updates.
        ts = int(time.time())
        return {
            "type": "ToRadio",
            "from": self.device.my_node_id,
            "ts": ts,
            "tsh": _fmt_ts(ts),
            "text": text,
            "sender_short_name": short_name,
            "sender_long_name": long_name,
            "message_id": meshpacket.get("id"),
            "raw_packet": _b64(raw),
        }

    def _direct_messages_path(self, node_hex):
        node_hex = (node_hex or "").lower()
        return os.path.join(self.data_dir, f"messages.dm.{node_hex}.jsonl")

    def _filter_text_entries(self, lines):
        out = []
        status_state_by_message_id = {}

        # Collect status events for each message ID.
        for entry in lines:
            if entry.get("type") != "DeliveryStatus":
                continue
            message_id = entry.get("message_id")
            if message_id is None:
                continue
            status = (entry.get("delivery_status") or "").lower()
            if status not in {"waiting", "ack", "failed"}:
                continue
            mid = int(message_id)
            st = status_state_by_message_id.get(mid)
            if st is None:
                st = {
                    "saw_failed": False,
                    "ack_nodes": set(),
                }
                status_state_by_message_id[mid] = st
            if status == "failed":
                st["saw_failed"] = True
            elif status == "ack":
                sender_hex = (entry.get("from") or "").lower()
                if sender_hex:
                    st["ack_nodes"].add(sender_hex)

        # Iterate text messages in file order (oldest to newest).
        for entry in lines:
            et = entry.get("type")
            if et not in {"FromRadio", "ToRadio"}:
                continue
            if et == "FromRadio":
                raw = entry.get("raw_packet")
                if not raw:
                    continue
                buf = base64.b64decode(raw.encode("ascii"))
                decoded = pb.decode(buf, FROMRADIO_SCHEMA)
                pkt = (decoded or {}).get("packet") or {}
                d = pkt.get("decoded") if pkt else None
                if not d or PORTNUMS.get(d.get("portnum")) != "TEXT_MESSAGE_APP":
                    continue

            if et == "ToRadio":
                message_id = entry.get("message_id")
                resolved = "waiting"
                ack_count = 0
                if message_id is not None:
                    st = status_state_by_message_id.get(int(message_id))
                    if st is not None:
                        ack_count = len(st.get("ack_nodes") or set())
                        if ack_count > 0:
                            resolved = "ack"
                        elif st.get("saw_failed"):
                            resolved = "failed"
                text_entry = dict(entry)
                text_entry["delivery_status"] = resolved
                text_entry["delivery_ack_count"] = ack_count
                out.append(text_entry)
                continue

            out.append(entry)
        return out

    def _ingest_packet_nodeinfo(self, packet, raw):
        """
        Processes the type of node info that arrives during runtime, when
        the radio receives a packet over the air where the portnum is
        NODEINFO_APP.
        """
        if not packet:
            return
        sender = packet.get("from")
        if sender is None:
            return
        key = f"{sender & 0xFFFFFFFF:08x}"
        node = self._node_info.setdefault(key, NodeInfo(key))
        node.node_id = key

        rx_snr = packet.get("rx_snr")
        if rx_snr is not None:
            node.rx_snr = rx_snr
        rx_rssi = packet.get("rx_rssi")
        if rx_rssi is not None:
            node.rx_rssi = rx_rssi
        rx_time = packet.get("rx_time")
        if rx_time is not None:
            node.last_heard = rx_time

        decoded = packet.get("decoded") or {}
        payload = decoded.get("payload")
        if payload:
            try:
                user = pb.decode(bytes(payload), USER_SCHEMA) or {}
            except (ValueError, TypeError) as exc:
                logger.debug("failed to decode nodeinfo payload for %s: %s", node.node_id, exc)
                user = {}
            if user:
                node.apply_user_proto_dict(user)

        self._append_nodeinfo_log(node, raw)
        if not self._in_startup:
            try:
                self._compact_nodeinfo()
            except Exception:
                logger.exception("nodeinfo compaction failed")

    def _ingest_top_level_nodeinfo(self, data, raw):
        """
        Processes the type of node info that arrives during the config dump at
        startup, as a result of a want_config_id. These are the kind of
        FromRadio messages with a top level field called node_info.
        """
        if not data:
            return
        num = data.get("num")
        if num is None:
            return
        key = f"{num & 0xFFFFFFFF:08x}"

        node = self._node_info.setdefault(key, NodeInfo(key))

        node.node_id = f"{num & 0xFFFFFFFF:08x}"
        hops_away = data.get("hops_away")
        if hops_away is not None:
            node.hops_away = hops_away
        snr = data.get("snr")
        if snr is not None:
            node.rx_snr = snr
        last = data.get("last_heard")
        if last is not None:
            node.last_heard = last
        dm = data.get("device_metrics")
        if dm:
            batt = dm.get("battery_level")
            if batt is not None:
                node.battery_level = batt
            volt = dm.get("voltage")
            if volt is not None:
                node.voltage = volt
        user = data.get("user")
        if user:
            node.apply_user_proto_dict(user)

        self._append_nodeinfo_log(node, raw)

        if not self._in_startup:
            try:
                self._compact_nodeinfo()
            except Exception:
                logger.exception("nodeinfo compaction failed")

    def _append_nodeinfo_log(self, node, raw):
        if raw is None:
            return
        ts = int(time.time())
        entry = {
            "ID": node.node_id,
            "tsh": _fmt_ts(ts),
            "ts": ts,
            "short_name": node.short_name,
            "long_name": node.long_name,
            "public_key": node.public_key,
            "raw_packet": _b64(raw),
        }
        if node.user_id:
            entry["user_id"] = node.user_id
        if node.last_heard is not None:
            entry["last_heard"] = node.last_heard
        if node.rx_snr is not None:
            entry["rx_snr"] = node.rx_snr
        if node.rx_rssi is not None:
            entry["rx_rssi"] = node.rx_rssi
        if node.hops_away is not None:
            entry["hops_away"] = node.hops_away
        if node.battery_level is not None:
            entry["battery_level"] = node.battery_level
        if node.voltage is not None:
            entry["voltage"] = node.voltage

        _append_jsonl(os.path.join(self.data_dir, "nodeinfo.jsonl"), entry)

    def _compact_nodeinfo(self):
        # For each node ID, keep first ever, any change in short/long/public_key, and newest
        # Uses extracted fields stored alongside raw_packet (does not decode raw_packet).
        path = os.path.join(self.data_dir, "nodeinfo.jsonl")
        entries = _load_jsonl(path)
        if not entries:
            return
        last_state = {}
        last_index = {}
        first_index = {}
        keep = set()
        index_info = {}

        for i, e in enumerate(entries):
            node_id = e.get("ID") or ""
            short_name = e.get("short_name") or ""
            long_name = e.get("long_name") or ""
            pubkey_b64 = e.get("public_key") or ""

            if not node_id:
                continue

            last_index[node_id] = i
            if node_id not in first_index:
                first_index[node_id] = i
            st = (short_name, long_name, pubkey_b64)
            index_info[i] = (node_id, st)
            if node_id not in last_state:
                keep.add(i)
                last_state[node_id] = st
            elif st != last_state[node_id]:
                keep.add(i)
                last_state[node_id] = st

        for nid, idx in last_index.items():
            keep.add(idx)

        if len(keep) == len(entries):
            return

        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            for i, obj in enumerate(entries):
                if i in keep:
                    json.dump(obj, f, separators=(",", ":"))
                    f.write("\n")
        os.replace(tmp_path, path)

    def _build_key_events(self, node_hex):
        events = []
        if not node_hex:
            return events
        path = os.path.join(self.data_dir, "nodeinfo.jsonl")
        if not os.path.exists(path):
            return events

        last_fp = None
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                if entry.get("ID") != node_hex:
                    continue
                public_key = entry.get("public_key")
                if not public_key:
                    continue
                key_bytes = base64.b64decode(public_key)
                fp = hashlib.sha256(key_bytes).hexdigest().upper()
                ts = int(entry.get("ts") or 0)
                if last_fp is None:
                    events.append({
                        "type": "KeyInfo",
                        "ts": ts,
                        "tsh": _fmt_ts(ts),
                        "text": f"Public key SHA-256: {fp}",
                    })
                    last_fp = fp
                    continue
                if fp != last_fp:
                    events.append({
                        "type": "Warning",
                        "ts": ts,
                        "tsh": _fmt_ts(ts),
                        "text": (
                            "WARNING! KEY CHANGED! SOMEONE MAY BE SPOOFING THIS NODE, AND MAY BE ABLE TO "
                            "DECODE MESSAGES YOU SEND FROM HERE ON! NEW KEY SHA-256: "
                            f"{fp}"
                        ),
                    })
                    last_fp = fp

        return events
