import asyncio
import pytest

from mesht_device import MeshtDevice, FROMRADIO_SCHEMA, TORADIO_SCHEMA  # noqa: E402
import pb  # noqa: E402


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
                {"channel": {"index": 0, "settings": {"name": "General"}, "role": 1}},
                {"channel": {"index": 1, "settings": {"name": "SomeOtherChannel"}, "role": 2}},
                {"channel": {"index": 2, "settings": {"name": "SomeDisabledChannel"}}},
                {"channel": {"index": 3, "settings": {"name": "AnotherDisabledChannel"}, "role": 0}},
                {"my_info": {"my_node_num": 101}},
                {"node_info": {"num": 42, "user": {"long_name": "HelloNode", "short_name": "helo"}}},
                {"config_complete_id": want_config_id},
            ]
            for fr in packets:
                await self._recv_q.put(pb.encode(fr, FROMRADIO_SCHEMA))

    async def recv(self):
        return await self._recv_q.get()


def test_get_channel_index_and_my_node_id():
    async def _run():
        #
        # Arrange
        #
        ft = FakeTransport()
        dev = MeshtDevice(ft)
        await dev.start()
        # Drain startup frames so device state reflects handshake
        async def _drain_handshake():
            while True:
                fr, _ = await asyncio.wait_for(dev.recv(), timeout=1.0)
                if isinstance(fr, dict) and fr.get("config_complete_id"):
                    break
        await _drain_handshake()
 
        #
        # Act
        #
        idx = dev.get_channel_index("SomeOtherChannel")

        #
        # Assert
        #
        assert idx == 1
        enabled = dev.get_channels()
        enabled_indices = [c.index for c in enabled]
        assert enabled_indices == [0, 1]
        # My node ID captured from MyNodeInfo
        assert dev.my_node_id == "00000065"

        await dev.close()

    asyncio.run(_run())


def test_send_text_sends_on_known_channel():
    async def _run():
        #
        # Arrange
        #
        ft = FakeTransport()
        dev = MeshtDevice(ft)
        await dev.start()
        # Drain startup frames so channels are known
        async def _drain_handshake():
            while True:
                fr, _ = await asyncio.wait_for(dev.recv(), timeout=1.0)
                if isinstance(fr, dict) and fr.get("config_complete_id"):
                    break
        await _drain_handshake()

        #
        # Act
        #
        await dev.send_text("hello", dev.get_channel_index("General"))

        #
        # Assert
        #
        assert len(ft.sent) >= 1
        tr = pb.decode(ft.sent[-1], TORADIO_SCHEMA)
        assert isinstance(tr, dict)
        pkt = tr.get("packet")
        assert isinstance(pkt, dict)
        assert pkt.get("channel") == dev.get_channel_index("General")
        decoded = pkt.get("decoded")
        assert isinstance(decoded, dict)
        assert decoded.get("payload") == b"hello"

        await dev.close()

    asyncio.run(_run())


def test_send_text_raises_on_unknown_channel():
    async def _run():
        #
        # Arrange
        #
        ft = FakeTransport()
        dev = MeshtDevice(ft)
        await dev.start()
        # Drain startup frames to avoid consuming them during the test
        async def _drain_handshake():
            while True:
                fr, _ = await asyncio.wait_for(dev.recv(), timeout=1.0)
                if isinstance(fr, dict) and fr.get("config_complete_id"):
                    break
        await _drain_handshake()
        before = len(ft.sent)

        #
        # Act
        #
        idx = dev.get_channel_index("DoesNotExist")
        with pytest.raises(Exception):
            await dev.send_text("hi", idx)

        #
        # Assert
        #
        assert len(ft.sent) == before

        await dev.close()

    asyncio.run(_run())


def test_send_direct_text_sets_destination_and_channel():
    async def _run():
        #
        # Arrange
        #
        ft = FakeTransport()
        dev = MeshtDevice(ft)
        await dev.start()
        # Drain startup frames so channels are known
        async def _drain_handshake():
            while True:
                fr, _ = await asyncio.wait_for(dev.recv(), timeout=1.0)
                if isinstance(fr, dict) and fr.get("config_complete_id"):
                    break
        await _drain_handshake()

        #
        # Act
        #
        await dev.send_direct_text("hello", 0x1234ABCD)

        #
        # Assert
        #
        assert len(ft.sent) >= 1
        tr = pb.decode(ft.sent[-1], TORADIO_SCHEMA)
        assert isinstance(tr, dict)
        pkt = tr.get("packet")
        assert isinstance(pkt, dict)
        assert pkt.get("to") == 0x1234ABCD
        assert pkt.get("channel") == 0
        decoded = pkt.get("decoded")
        assert isinstance(decoded, dict)
        assert decoded.get("payload") == b"hello"

        await dev.close()

    asyncio.run(_run())


def test_recv_updates_lora_config_cache():
    async def _run():
        #
        # Arrange
        #
        ft = FakeTransport()
        dev = MeshtDevice(ft)
        await dev.start()
        # Drain startup frames first so the next recv() observes our test config
        async def _drain_handshake():
            while True:
                fr, _ = await asyncio.wait_for(dev.recv(), timeout=1.0)
                if isinstance(fr, dict) and fr.get("config_complete_id"):
                    break
        await _drain_handshake()

        # Build a lora config message
        lora_cfg = {"use_preset": True, "modem_preset": 1, "region": 7}
        fr = {"config": {"lora": lora_cfg}}
        await ft._recv_q.put(pb.encode(fr, FROMRADIO_SCHEMA))

        #
        # Act
        #
        _, _ = await dev.recv()

        #
        # Assert
        #
        assert isinstance(dev.lora_config, dict)
        assert dev.lora_config.get("use_preset") is True
        assert dev.lora_config.get("modem_preset") == 1
        assert dev.lora_config.get("region") == 7

        await dev.close()

    asyncio.run(_run())
