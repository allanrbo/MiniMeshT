import asyncio
import random
import pb
import logging
logger = logging.getLogger(__name__)


DATA_SCHEMA = [
    ("varint", "portnum", 1),
    ("bytes", "payload", 2),
    ("bool", "want_response", 3),
    ("fixed32", "dest", 4),
    ("fixed32", "source", 5),
    ("fixed32", "request_id", 6),
    ("fixed32", "reply_id", 7),
    ("fixed32", "emoji", 8),
    ("uint32", "bitfield", 9),
]

MESHPACKET_SCHEMA = [
    ("fixed32", "from", 1),
    ("fixed32", "to", 2),
    ("uint32", "channel", 3),
    ("oneof", "payload_variant", [
        (DATA_SCHEMA, "decoded", 4),
        ("bytes", "encrypted", 5),
    ]),
    ("fixed32", "id", 6),
    ("fixed32", "rx_time", 7),
    ("float", "rx_snr", 8),
    ("uint32", "hop_limit", 9),
    ("bool", "want_ack", 10),
    ("int32", "priority", 11),
    ("int32", "rx_rssi", 12),
    ("bool", "via_mqtt", 14),
    ("uint32", "hop_start", 15),
    ("bytes", "public_key", 16),
    ("bool", "pki_encrypted", 17),
    ("uint32", "next_hop", 18),
    ("uint32", "relay_node", 19),
    ("uint32", "tx_after", 20),
]

CHANNEL_SETTINGS_SCHEMA = [
    ("uint32", "channel_num", 1),
    ("string", "name", 3),
]

CHANNEL_SCHEMA = [
    ("int32", "index", 1),
    (CHANNEL_SETTINGS_SCHEMA, "settings", 2),
    ("int32", "role", 3),
]

USER_SCHEMA = [
    ("string", "id", 1),
    ("string", "long_name", 2),
    ("string", "short_name", 3),
    ("int32", "hw_model", 5),
    ("bool", "is_licensed", 6),
    ("int32", "role", 7),
    ("bytes", "public_key", 8),
]

DEVICEMETRICS_SCHEMA = [
    ("uint32", "battery_level", 1),
    ("float", "voltage", 2),
]

MYNODEINFO_SCHEMA = [
    ("uint32", "my_node_num", 1),
]

NODEINFO_SCHEMA = [
    ("uint32", "num", 1),
    (USER_SCHEMA, "user", 2),
    ("float", "snr", 4),
    ("fixed32", "last_heard", 5),
    (DEVICEMETRICS_SCHEMA, "device_metrics", 6),
    ("uint32", "hops_away", 9),
]

LORACONFIG_SCHEMA = [
    ("bool", "use_preset", 1),
    ("int32", "modem_preset", 2),
    ("int32", "region", 7),
]

CONFIG_SCHEMA = [
    ("oneof", "variant", [
        (LORACONFIG_SCHEMA, "lora", 6),
    ]),
]

FROMRADIO_SCHEMA = [
    ("uint32", "id", 1),
    ("oneof", "payload_variant", [
        (MESHPACKET_SCHEMA, "packet", 2),
        (MYNODEINFO_SCHEMA, "my_info", 3),
        (NODEINFO_SCHEMA, "node_info", 4),
        (CONFIG_SCHEMA, "config", 5),
        ("uint32", "config_complete_id", 7),
        (CHANNEL_SCHEMA, "channel", 10),
    ]),
]

TORADIO_SCHEMA = [
    (MESHPACKET_SCHEMA, "packet", 1),
    ("uint32", "want_config_id", 3),
]

PRESET_NAMES = {
    0: "LongFast",
    1: "LongSlow",
    2: "VeryLongSlow",
    3: "MediumSlow",
    4: "MediumFast",
    5: "ShortSlow",
    6: "ShortFast",
    7: "LongModerate",
    8: "ShortTurbo",
}

REGION_NAMES = {
    0: "UNSET",
    1: "US",
    2: "EU_433",
    3: "EU_868",
    4: "CN",
    5: "JP",
    6: "ANZ",
    7: "KR",
    8: "TW",
    9: "RU",
    10: "IN",
    11: "NZ_865",
    12: "TH",
    13: "LORA_24",
    14: "UA_433",
    15: "UA_868",
    16: "MY_433",
    17: "MY_919",
    18: "SG_923",
    19: "PH_433",
    20: "PH_868",
    21: "PH_915",
    22: "ANZ_433",
    23: "KZ_433",
    24: "KZ_863",
    25: "NP_865",
    26: "BR_902",
}

PORTNUMS = {
    0: "UNKNOWN_APP",
    1: "TEXT_MESSAGE_APP",
    2: "REMOTE_HARDWARE_APP",
    3: "POSITION_APP",
    4: "NODEINFO_APP",
    5: "ROUTING_APP",
    6: "ADMIN_APP",
    7: "TEXT_MESSAGE_COMPRESSED_APP",
    8: "WAYPOINT_APP",
    9: "AUDIO_APP",
    10: "DETECTION_SENSOR_APP",
    11: "ALERT_APP",
    32: "REPLY_APP",
    33: "IP_TUNNEL_APP",
    34: "PAXCOUNTER_APP",
    64: "SERIAL_APP",
    65: "STORE_FORWARD_APP",
    66: "RANGE_TEST_APP",
    67: "TELEMETRY_APP",
    68: "ZPS_APP",
    69: "SIMULATOR_APP",
    70: "TRACEROUTE_APP",
    71: "NEIGHBORINFO_APP",
    72: "ATAK_PLUGIN",
    73: "MAP_REPORT_APP",
    74: "POWERSTRESS_APP",
    76: "RETICULUM_TUNNEL_APP",
    256: "PRIVATE_APP",
    257: "ATAK_FORWARDER",
    511: "MAX",
}
NAMES_TO_PORTNUMS = {v: k for k, v in PORTNUMS.items()}
BROADCAST_NUM = 0xFFFFFFFF


class Channel:
    def __init__(self, index, name, role):
        self.index = int(index)
        self.name = name
        self.role = int(role or 0)


class MeshtDevice:
    def __init__(self, transport):
        self.transport = transport
        self.channels = []
        self.lora_config = None
        # Track local node number from MyNodeInfo
        self.my_node_id = "00000000"

    async def start(self):
        await self.transport.start()
        nonce = random.randint(1, 1_000_000_000)
        logger.debug("MeshtDevice.start: sending want_config_id nonce=%s", nonce)
        await self.transport.send(pb.encode({"want_config_id": nonce}, TORADIO_SCHEMA))

    async def close(self):
        return await self.transport.close()

    async def send_text(self, text, channel_index):
        return await self._send_text(text, channel_index, BROADCAST_NUM)

    async def send_direct_text(self, text, destination):
        return await self._send_text(text, 0, int(destination))

    async def _send_text(self, text, channel_index, destination):
        data = {
            "portnum": NAMES_TO_PORTNUMS["TEXT_MESSAGE_APP"],
            "payload": text.encode("utf-8"),
            "want_response": False,
        }
        meshpacket = {
            "id": random.randint(1, 0x7FFFFFFF),
            "to": int(destination),
            "channel": int(channel_index),
            "want_ack": True,
            "decoded": data,
        }
        payload = pb.encode({"packet": meshpacket}, TORADIO_SCHEMA)
        await self.transport.send(payload)
        # Return the meshpacket details for logging by callers
        return meshpacket

    async def recv(self):
        data = await self.transport.recv()
        fr = pb.decode(data, FROMRADIO_SCHEMA)
        logger.debug(f"FromRadio: {fr}")
        self._maybe_store_channel(fr)
        self._maybe_store_lora_config(fr)
        self._maybe_store_my_node(fr)
        return fr, data

    def get_channel_index(self, name):
        for ch in self.channels:
            if ch.name == name:
                return ch.index
        return None

    def get_channels(self):
        return list(self.channels)

    def _maybe_store_my_node(self, from_radio):
        mi = from_radio.get("my_info")
        if not mi:
            return

        my_node_num = mi.get("my_node_num", 0)
        self.my_node_id = f"{my_node_num & 0xFFFFFFFF:08x}"

    def _maybe_store_channel(self, from_radio):
        if not isinstance(from_radio, dict):
            return
        ch = from_radio.get("channel")
        if not isinstance(ch, dict):
            return
        # protobuf default for 0 means 'missing' -> treat as index 0
        idx = int(ch.get("index") or 0)
        name = (ch.get("settings") or {}).get("name") or f"Channel {idx}"
        role = int(ch.get("role") or 0)

        for i, existing in enumerate(self.channels):
            if existing.index == idx:
                if role == 0:
                    # Remove if disabled
                    self.channels.pop(i)
                    return
                existing.name = name
                existing.role = role
                break
        else:
            if role != 0:
                self.channels.append(Channel(idx, name, role))

        self.channels.sort(key=lambda c: c.index)

    def _maybe_store_lora_config(self, from_radio):
        if not isinstance(from_radio, dict):
            return
        cfg = from_radio.get("config")
        if not isinstance(cfg, dict):
            return
        lora = cfg.get("lora")
        if isinstance(lora, dict):
            self.lora_config = lora
