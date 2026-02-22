# MiniMeshT

_A tiny terminal chat client for Meshtastic. No protobufs, no bloat._

Author: Allan Riordan Boll

MiniMeshT is a simple, irssi-style terminal chat client that talks to devices running Meshtastic firmware (tested with v2.6.11). 

Use it to chat, explore the mesh, or as a lightweight basis for your own tools.

**What makes it different?**
 * No dependency on the official Meshtastic libraries or protobufs.
 * Pretty minimal dependencies: just `bleak` for Bluetooth, `pyserial-asyncio` for serial ports, and `pytest`.
 * Ships with a minimal Protobuf encoder/decoder (~550 lines excluding comments and blanks. Also available standalone: [pb.py](https://github.com/allanrbo/pb.py/)).
 * Hackable base: ~1,200 lines of Python. Easy to fork and extend.

![Illustration of laptop running MiniMeshT](./readme_graphics/laptop.jpeg)

Also has a screen to list detected nodes:

![Screenshot of node info list](./readme_graphics/nodelist.png)

## Keyboard shortcuts
| Keys   | Action           |
|--------|------------------|
| Ctrl+G | View node list   |
| Ctrl+N | Next chat        |
| Ctrl+P | Previous chat    |
| Ctrl+C | Exit             |
| Enter | Open DM (node list) |

Direct messages appear inline in the channel strip as `DM` (or `[DM:Name]` when selected). Use Ctrl+G to open the node list, move with arrow keys, and press Enter to open a DM thread. The node list shows a `DMs` column with the count of DMs for each node.

## Running

You'll have to do your initial device setup (username, region, modem settings) using some other app (e.g. the official app), as MiniMeshT does not yet have any features for setting device configuration values.

To run, first do the usual Python venv dance to install the few requirements there are (`bleak` for Bluetooth, `pyserial-asyncio`, and `pytest`):

```
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

Using USB is the easiest.
```
venv/bin/python3 chat.py --serial-port /dev/ttyACM0
```

Using Bluetooth is another option. I have only tested on Linux.
```
# Select a BLE radio on the PC for for use by bluetoothctl (apparently it can only use one at a time).
sudo btmgmt info
bluetoothctl select 11:22:33:44:55:66

# Readies the device for pairing.
bluetoothctl connect 77:88:99:AA:BB:CC

# Actually pairs. Will prompt for pin.
bluetoothctl pair 77:88:99:AA:BB:CC

# Sets the device to re-pair automatically when it is turned on, which eliminates the need to pair all over again.
bluetoothctl trust 77:88:99:AA:BB:CC

# Check whether paired and connected.
bluetoothctl devices
bluetoothctl info 77:88:99:AA:BB:CC

# Disconnect bluez from the device. Seems that sometimes bleak needs this, maybe to "own" the connection?
bluetoothctl disconnect 77:88:99:AA:BB:CC

# Start MiniMeshT using BLE.
venv/bin/python3 chat.py --transport ble --ble-adapter hci1 --ble-address 77:88:99:AA:BB:CC
```

Using TCP is another option. You will need to have configured your device to connect to WiFi beforehand via some other app (e.g. the official app).
```
venv/bin/python3 chat.py --transport tcp --tcp-host 1.2.3.4
```

## Docker

I wouldn't really recommend it. I personally prefer to run the Python code in a venv straight on my machine, as shown above. But for folks who insist, I've provided Docker images:

```
mkdir -p data
docker run --rm -it \
    --device /dev/ttyACM0 \
    -v "$(pwd)/data:/app/data" \
    allanrbo/minimesht:latest --serial-port /dev/ttyACM0
```

## Forking and reusing this code

I encourage you to use this as a basis for your own client, if you like myself also don't feel like depending on the official libraries and protos.

All you really need are these files, which are 1194 lines of code as of writing (2025-09-22). Note this includes the proto library `pb.py`, and proto schema definitions embedded in `mesht_device.py` (around 100 lines)!

 * mesht_device.py
 * transport_ble.py
 * transport_serial.py
 * transport_tcp.py
 * pb.py

The UI and file persistence, which you probably don't want if you are rolling your own client, but may find useful for inspiration, are implemented in

 * chat.py
 * mesht_db.py

An example that uses just the basic `mesht_device.py` and a transport to send and receive messages, without any bookkeeping:
```py
import asyncio

from mesht_device import MeshtDevice


async def _wait_for_config_complete(device):
    while True:
        from_radio, _ = await device.recv()
        print(from_radio)
        print("---")
        if isinstance(from_radio, dict) and from_radio.get("config_complete_id") is not None:
            return


async def run():
    from transport_serial import SerialTransport
    transport = SerialTransport(port="/dev/ttyACM0", baudrate=115200)
    # OR
    #from transport_ble import BLETransport
    #transport = BLETransport(address="77:88:99:AA:BB:CC", adapter="hci1")
    # OR
    #from transport_tcp import TCPTransport
    #transport = TCPTransport("1.2.3.4", 4403)

    device = MeshtDevice(transport)
    try:
        await device.start()
        await _wait_for_config_complete(device)

        # Sending a message:
        channel_name = "somechannel"
        idx = device.get_channel_index(channel_name)
        if idx is None:
            print(f"Could not find channel named '{channel_name}'. Aborting send.")
            return
        print(f"Sending text to channel '{channel_name}'")
        await device.send_text("Hello world", idx)

        # Sending a DM:
        dm_node_id = 0x12345678
        print(f"Sending DM to {dm_node_id:08x}")
        await device.send_direct_text("Hello DM", dm_node_id)

        # Receiving (all messages types):
        while True:
            print(await device.recv())
            print("---")

    finally:
        await device.close()


asyncio.run(run())
```

An example of a simple bot that also uses MeshtDb to read previously sent messages, and pays attention to delivery receipts:
```py

import asyncio
import os

from mesht_device import MeshtDevice
from mesht_db import MeshtDb
from packet_parsing import parse_text_packet, parse_delivery_packet, is_dm_for
from transport_serial import SerialTransport


async def run():
    #
    # Build transport + DB facade.
    #
    transport = SerialTransport(port="/dev/tty.ttyACM0", baudrate=115200)
    # OR:
    # from transport_ble import BLETransport
    # transport = BLETransport(address="77:88:99:AA:BB:CC", adapter="hci1")
    # OR:
    # from transport_tcp import TCPTransport
    # transport = TCPTransport("1.2.3.4", 4403)

    device = MeshtDevice(transport)
    base_dir = os.path.dirname(os.path.abspath(__file__))
    db = MeshtDb(device, os.path.join(base_dir, "data"))
    pending = {}

    try:
        #
        # Start and wait for the startup config dump to complete.
        #
        await db.start()
        await db.wait_for_config_complete()
        if device.my_node_id == "00000000":
            print("Could not resolve local node ID yet.")
            return
        print(f"My node id: {device.my_node_id}")
        print("DM ping bot ready. Send 'ping' as DM to get 'pong'.")

        #
        # Main loop: handle DM ping and routing delivery reports.
        #
        while True:
            fr = await db.next_fromradio()
            pkt = (fr or {}).get("packet") or {}

            # Handle delivery reports for our sent ping replies.
            delivery = parse_delivery_packet(pkt)
            if delivery is not None:
                request_id = delivery.request_id
                if request_id in pending:
                    peer_hex = pending.pop(request_id)
                    status = (delivery.status or "").upper()
                    print(f"Delivery for {request_id} to {peer_hex}: {status}")

            # Handle incoming direct text packets.
            text_info = parse_text_packet(pkt)
            if text_info is not None:
                if not is_dm_for(text_info, device.my_node_id):
                    continue

                text = text_info.text or ""
                print(f"DM from {text_info.sender_hex}: {text}")

                if db.dm_looks_spoofed(text_info.sender_hex):
                    print(f"Skipping reply to {text_info.sender_hex}: key changed in history")
                    continue

                # Show the stored DM history for this peer.
                print(f"History with {text_info.sender_hex}:")
                for msg in db.get_direct_messages(text_info.sender_hex):
                    print(msg)

                if text.strip().lower() != "ping":
                    continue

                sent = await db.send_direct_text("pong", int(text_info.sender_num))
                message_id = int(sent.get("id") or 0)
                pending[message_id] = text_info.sender_hex
                print(f"Sent pong to {text_info.sender_hex}")

    finally:
        await db.close()


asyncio.run(run())
```

As mentioned earlier, MiniMeshT uses a novel tiny embedded Protobuf encoder and decoder: [pb.py](https://github.com/allanrbo/pb.py/). This Protobuf library does not use the conventional codegen from .proto files, but instead simply allows for lightweight embedded schemas straight in the Python source code. You will find these in the top of `mesht_device.py`. I did this because for this small project I didn't feel like depending on the more heavyweight ecosystem of codegen with protoc. It also allows me to not depend on the official Meshtastic protobufs, of which most message types and fields I do not care about.
