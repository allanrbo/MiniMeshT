import argparse
import logging
logger = logging.getLogger(__name__)
import asyncio
import curses
import datetime as dt
import os
import base64
import sys
import signal
import time

from mesht_device import MeshtDevice, PRESET_NAMES, REGION_NAMES, FROMRADIO_SCHEMA, TORADIO_SCHEMA, BROADCAST_NUM
from transport_ble import BLETransport
from transport_serial import SerialTransport
from transport_tcp import TCPTransport
from mesht_db import MeshtDb
import pb


class ChatUI:
    def __init__(self, stdscr):
        self.stdscr = stdscr
        # Messages for the currently selected chat (formatted lines)
        self.messages = []
        self.input_buf = []
        self.status = ""
        self.scroll_offset = 0
        self.mode = "chat"  # "chat" or "nodes"
        self.current_chat = None  # ("channel", index) or ("direct", node_hex)
        self.switch_chat = None  # +1 for next, -1 for prev
        # Track last-rendered state for stable scrolling
        self._last_view_height = None
        self._last_wrapped_count = 0
        self._last_size = None
        self._last_chat_key = None
        self._last_nodes_order = []
        self._last_nodes_count = 0
        # Node list sorting (default: LastHeard desc)
        self.nodes_sort_key = "last"
        self.nodes_sort_reverse = True
        self.nodes_selected = 0
        self.open_dm_node = None

    def setup(self):
        curses.curs_set(1)
        curses.noecho()
        curses.cbreak()
        self.stdscr.keypad(True)
        self.stdscr.nodelay(True)
        try:
            curses.set_escdelay(25)
        except Exception:
            pass

    def teardown(self):
        try:
            curses.nocbreak()
            self.stdscr.keypad(False)
            curses.echo()
            curses.curs_set(1)
        except Exception:
            pass

    def add_message(self, line, ts_epoch=None):
        # Use provided epoch seconds when valid; otherwise use current time
        if ts_epoch is not None:
            ts = dt.datetime.fromtimestamp(ts_epoch)
        else:
            ts = dt.datetime.now()
        self.messages.append(f"[{ts.strftime('%Y-%m-%d %H:%M')}] {line}")

    def set_status(self, text):
        self.status = text

    def get_input_text(self):
        return "".join(self.input_buf)

    def clear_input(self):
        self.input_buf.clear()

    def handle_key(self, ch):
        # Ctrl+G (7) toggles nodes list view
        if ch == 7:
            self.mode = "nodes" if self.mode == "chat" else "chat"
            self.scroll_offset = 0
            self.open_dm_node = None
            if self.mode == "nodes":
                self.nodes_selected = 0
            return None

        if self.mode == "chat":
            # Ctrl+N (14) next chat, Ctrl+P (16) previous chat
            if ch == 14:
                self.switch_chat = 1
                return None
            if ch == 16:
                self.switch_chat = -1
                return None
            if ch in (curses.KEY_ENTER, 10, 13):
                # Return a completed line when Enter is pressed
                text = self.get_input_text().strip()
                self.clear_input()
                return text
            elif ch == curses.KEY_UP:
                # Scroll up one line (older content)
                self.scroll_offset += 1
                return None
            elif ch == curses.KEY_DOWN:
                # Scroll down one line (newer content)
                self.scroll_offset = max(0, self.scroll_offset - 1)
                return None
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                if self.input_buf:
                    self.input_buf.pop()
            elif ch == curses.KEY_DC:
                # ignore delete for simplicity
                pass
            elif ch == curses.KEY_RESIZE:
                pass
            elif ch == curses.KEY_PPAGE:
                # Page up with 1-line overlap based on last view height
                step = max(1, (self._last_view_height or self._page_size()) - 1)
                self.scroll_offset += step
            elif ch == curses.KEY_NPAGE:
                # Page down with 1-line overlap based on last view height
                step = max(1, (self._last_view_height or self._page_size()) - 1)
                self.scroll_offset = max(0, self.scroll_offset - step)
            elif ch == curses.KEY_HOME:
                # Jump to top (use a large value; draw() clamps)
                self.scroll_offset = 1_000_000_000
            elif ch == curses.KEY_END:
                self.scroll_offset = 0
            elif ch >= 32 and ch < 127:
                self.input_buf.append(chr(ch))
            return None

        if self.mode == "nodes":
            if ch == 27 or (hasattr(curses, "KEY_EXIT") and ch == curses.KEY_EXIT):
                self.mode = "chat"
                self.scroll_offset = 0
                return None
            # Sorting shortcuts (Shift+I/S/L/H/N/T)
            if ch in (ord('I'), ord('S'), ord('L'), ord('H'), ord('N'), ord('R'), ord('T')):
                key_map = {
                    ord('I'): ("id", False),
                    ord('S'): ("short", False),
                    ord('L'): ("long", False),
                    ord('H'): ("hops", False),
                    ord('N'): ("rx_snr", True),
                    ord('R'): ("rx_rssi", True),
                    ord('T'): ("last", True),
                }
                new_key, default_rev = key_map[ch]
                if new_key == self.nodes_sort_key:
                    self.nodes_sort_reverse = not self.nodes_sort_reverse
                else:
                    self.nodes_sort_key = new_key
                    self.nodes_sort_reverse = default_rev
                self.scroll_offset = 0
                return None
            count = int(self._last_nodes_count or 0)
            if count <= 0:
                return None
            if ch in (curses.KEY_ENTER, 10, 13):
                if 0 <= self.nodes_selected < len(self._last_nodes_order):
                    node_hex = self._last_nodes_order[self.nodes_selected]
                    if node_hex:
                        self.open_dm_node = node_hex
                        self.mode = "chat"
                        self.scroll_offset = 0
                return None
            if ch == curses.KEY_UP:
                self.nodes_selected = max(0, self.nodes_selected - 1)
            elif ch == curses.KEY_DOWN:
                self.nodes_selected = min(count - 1, self.nodes_selected + 1)
            elif ch == curses.KEY_PPAGE:
                step = max(1, (self._last_view_height or self._page_size()) - 1)
                self.nodes_selected = max(0, self.nodes_selected - step)
            elif ch == curses.KEY_NPAGE:
                step = max(1, (self._last_view_height or self._page_size()) - 1)
                self.nodes_selected = min(count - 1, self.nodes_selected + step)
            elif ch == curses.KEY_HOME:
                self.nodes_selected = 0
            elif ch == curses.KEY_END:
                self.nodes_selected = count - 1
            return None

    def _page_size(self):
        h, _ = self.stdscr.getmaxyx()
        # minus status and input lines
        return max(1, h - 3)

    def draw_messages(self, channels, node_info):
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()
        size = (h, w)

        # Layout: messages area (h-2), status (1), input (1)
        msg_h = max(1, h - 2)
        status_y = h - 2
        input_y = h - 1
        line_w = max(1, w - 1)

        # Prepare message lines with wrapping as needed
        wrapped = []
        for line in self.messages:
            if len(line) <= line_w:
                wrapped.append(line)
            else:
                # simple wrap
                start = 0
                while start < len(line):
                    wrapped.append(line[start : start + line_w])
                    start += line_w

        # Determine viewport
        total = len(wrapped)

        # Keep viewport anchored while scrolled: if new lines arrive and we're not at bottom,
        # increase scroll_offset by the number of newly wrapped lines so the view stays fixed.
        resized = size != (self._last_size or size)
        chan_changed = self._last_chat_key is not None and self._last_chat_key != self.current_chat
        if self.scroll_offset > 0 and not resized and not chan_changed and total > self._last_wrapped_count:
            self.scroll_offset += (total - self._last_wrapped_count)

        offset = min(self.scroll_offset, max(0, total))

        # Compute viewport indices
        end_idx0 = max(0, total - offset)
        start_idx0 = max(0, end_idx0 - msg_h)

        # Effective viewport height equals message area height (no hints)
        msg_h_eff = msg_h
        self._last_view_height = msg_h_eff

        # Final viewport
        end_idx = end_idx0
        start_idx = start_idx0
        view = wrapped[start_idx:end_idx]

        # Bottom-align within message area
        inner_top = 0
        if len(view) < msg_h_eff:
            inner_top += msg_h_eff - len(view)

        for i, line in enumerate(view[:msg_h_eff]):
            y = i + inner_top
            if y >= msg_h:
                break
            try:
                self.stdscr.addnstr(y, 0, line, line_w)
            except Exception:
                pass

        status = self.status or ""
        try:
            self.stdscr.addnstr(status_y, 0, status.ljust(w), line_w, curses.A_REVERSE)
        except Exception:
            pass

        # Input line
        cur_name = "No chat"
        cur_chat = self.current_chat
        if cur_chat is not None:
            chat_type, chat_id = cur_chat
            if chat_type == "channel":
                cur_name = f"Channel {chat_id}"
                for c in channels:
                    if c.index == chat_id:
                        cur_name = c.name or cur_name
                        break
            elif chat_type == "direct":
                node = node_info.get(chat_id) if isinstance(node_info, dict) else None
                display = node.display_name() if node else ""
                if display:
                    cur_name = f"DM: {display}"
                else:
                    cur_name = f"DM: {(chat_id or '').upper()}"
        prompt = f"[{cur_name}] > "
        input_text = self.get_input_text()
        try:
            self.stdscr.addnstr(input_y, 0, (prompt + input_text).ljust(w), line_w)
            # Move cursor
            x = min(w - 1, len(prompt) + len(input_text))
            self.stdscr.move(input_y, x)
        except Exception:
            pass

        self.stdscr.refresh()

        # Update last-rendered state
        # Keep internal scroll offset within valid bounds
        self.scroll_offset = min(max(0, offset), max(0, total))
        self._last_wrapped_count = total
        self._last_size = size
        self._last_chat_key = self.current_chat

    def draw_nodes(self, node_items, dm_counts):
        # node_items: iterable of (node_id_hex, NodeInfo)
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()
        size = (h, w)

        # Layout: instruction header (0), column header (1), table area, status (bottom)
        status_y = h - 1
        # Table height excludes 2 header lines and 1 status line
        table_h = max(1, h - 3)
        line_w = max(1, w - 1)

        # Build data rows from node info
        nodes = []
        for nid, ni in node_items:
            nid_hex = (nid or "").lower()
            short = ni.short_name or ""
            long = ni.long_name or ""
            hops = ni.hops_away
            rx_snr = ni.rx_snr
            rx_rssi = ni.rx_rssi
            last = ni.last_heard
            batt = ni.battery_level
            volt = ni.voltage
            dm_count = 0
            if isinstance(dm_counts, dict):
                dm_count = int(dm_counts.get(nid_hex, 0) or 0)
            nodes.append({
                "id": (nid or "").upper(),
                "id_hex": nid_hex,
                "short": short,
                "long": long,
                "dm_count": dm_count,
                "hops": hops,
                "rx_snr": round(rx_snr, 1) if rx_snr is not None else None,
                "rx_rssi": rx_rssi,
                "last": last,
                "batt": batt,
                "volt": round(volt, 2) if volt is not None else None,
            })
        # Sort by selected key while keeping entries with actual values ahead of None.
        k = self.nodes_sort_key
        reverse = bool(self.nodes_sort_reverse)
        if k == "id":
            nodes.sort(key=lambda entry: entry.get("id") or "", reverse=reverse)
        else:
            key_is_text = k in ("short", "long")
            with_value = []
            missing_value = []
            for entry in nodes:
                node_id = entry.get("id") or ""
                value = entry.get(k)
                if value is None:
                    missing_value.append((node_id, entry))
                    continue
                if key_is_text:
                    text_value = str(value)
                    if not text_value.strip():
                        missing_value.append((node_id, entry))
                        continue
                    sort_val = text_value.lower()
                else:
                    try:
                        sort_val = value
                    except Exception:
                        missing_value.append((node_id, entry))
                        continue
                with_value.append((sort_val, node_id, entry))

            with_value.sort(key=lambda item: item[1])
            with_value.sort(key=lambda item: item[0], reverse=reverse)
            missing_value.sort(key=lambda item: item[0])

            nodes = [entry for _sort_val, _node_id, entry in with_value]
            nodes.extend(entry for _node_id, entry in missing_value)

        def fmt_last(ts):
            if ts is None:
                return ""
            try:
                return dt.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                return ""

        # Build header and formatted rows
        # Columns: ID, Short name, Long name, DMs, Hops, SNR, RSSI, LastHeard, Batt, Volt
        # Allocate simple widths; truncate as needed
        cols = [
            ("ID", 8),
            ("Short name", 10),
            ("Long name", max(10, w // 5)),
            ("DMs", 4),
            ("Hops", 4),
            ("SNR", 5),
            ("RSSI", 6),
            ("LastHeard", 19),
            ("Batt%", 6),
            ("Volt", 6),
        ]
        # Adjust Long name column to fit remaining width
        # Account for two spaces between columns
        fixed = sum(cw for _cn, cw in cols if _cn != "Long name") + 2 * (len(cols) - 1)
        long_w = max(5, line_w - fixed)
        cols = [(n, (long_w if n == "Long name" else w_)) for n, w_ in cols]

        def _to_ascii(s):
            if s is None:
                return ""
            try:
                return str(s).encode("ascii", "ignore").decode("ascii")
            except Exception:
                return str(s)

        # Right-align numeric-like columns for readability
        numeric_cols = {"DMs", "Hops", "SNR", "RSSI", "Batt%", "Volt"}

        def fmt_row(values):
            parts = []
            for (name, cw), val in zip(cols, values):
                s = _to_ascii(val)
                if len(s) > cw:
                    s = s[:cw]
                if name in numeric_cols:
                    s = s.rjust(cw)
                else:
                    s = s.ljust(cw)
                parts.append(s)
            return "  ".join(parts)

        header = fmt_row([n for n, _ in cols])
        data_rows = []
        for r in nodes:
            data_rows.append(
                fmt_row(
                    (
                        r["id"],
                        r["short"],
                        r["long"],
                        r["dm_count"],
                        ("" if r["hops"] is None else r["hops"]),
                        ("" if r["rx_snr"] is None else r["rx_snr"]),
                        ("" if r["rx_rssi"] is None else r["rx_rssi"]),
                        fmt_last(r["last"]),
                        ("" if r["batt"] is None else r["batt"]),
                        ("" if r["volt"] is None else r["volt"]),
                    )
                )
            )

        # Determine viewport with scroll (top-anchored)
        total = len(data_rows)
        self._last_nodes_count = total
        if total <= 0:
            self.nodes_selected = 0
            self._last_nodes_order = []
        else:
            self.nodes_selected = min(max(0, self.nodes_selected), total - 1)
            self._last_nodes_order = [entry.get("id_hex") or "" for entry in nodes]
        max_off = max(0, total - table_h)
        if self.nodes_selected < self.scroll_offset:
            self.scroll_offset = self.nodes_selected
        elif self.nodes_selected > self.scroll_offset + table_h - 1:
            self.scroll_offset = self.nodes_selected - table_h + 1
        if self.scroll_offset < 0:
            self.scroll_offset = 0
        if self.scroll_offset > max_off:
            self.scroll_offset = max_off
        start_idx = self.scroll_offset
        end_idx = min(total, start_idx + table_h)
        view = data_rows[start_idx:end_idx]

        # Instruction line at top
        instr = "Enter: DM  |  Esc/Ctrl+G exit  |  Sort: Shift+I ID  Shift+S Short  Shift+L Long  Shift+H Hops  Shift+N SNR  Shift+R RSSI  Shift+T LastHeard (toggle to reverse)"
        try:
            self.stdscr.addnstr(0, 0, instr.ljust(w), line_w, curses.A_REVERSE)
        except Exception:
            pass

        # Sticky column header
        try:
            self.stdscr.addnstr(1, 0, header.ljust(w), line_w)
        except Exception:
            pass

        for i, line in enumerate(view):
            y = i + 2
            try:
                row_idx = start_idx + i
                if row_idx == self.nodes_selected:
                    self.stdscr.addnstr(y, 0, line, line_w, curses.A_REVERSE)
                else:
                    self.stdscr.addnstr(y, 0, line, line_w)
            except Exception:
                pass

        status = f"Total nodes ever observed: {max(0, len(nodes))}"
        try:
            self.stdscr.addnstr(status_y, 0, status.ljust(w), line_w, curses.A_REVERSE)
        except Exception:
            pass
        self.stdscr.refresh()
        self._last_size = size
        self._last_view_height = table_h


async def main_async(args):
    # Build transport
    if args.transport == "ble":
        transport = BLETransport(address=args.ble_address, adapter=args.ble_adapter)
    elif args.transport == "tcp":
        transport = TCPTransport(args.tcp_host, args.tcp_port)
    else:
        transport = SerialTransport(port=args.serial_port, baudrate=args.serial_baudrate)
    device = MeshtDevice(transport)

    # Curses setup
    stdscr = curses.initscr()
    ui = ChatUI(stdscr)
    ui.setup()

    base_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = args.data_dir or os.path.join(base_dir, "data")
    db = MeshtDb(device, data_dir)
    current_chat = None

    connected = False
    try:
        ui.set_status("Connecting...")
        ui.draw_messages([], {})
        # Try to start DB (which starts the device and recv loop)
        try:
            logger.debug("chat: initial db.start()")
            await db.start()
            connected = True
            logger.debug("chat: initial db.start() success")
        except Exception as e:
            connected = False
            logger.warning("chat: initial db.start() failed: %r", e)
        # Choose first available chat target (channel or DMs)
        lora_preset_name = None
        lora_region_name = None

        def my_display_name():
            me = db.node_info.get(device.my_node_id)
            if me is None:
                return "You"
            name = me.display_name()
            return name or "You"

        def resolve_sender_name(sender, meta=None):
            if isinstance(meta, dict):
                n = meta.get("sender_long_name") or meta.get("sender_short_name")
                if n:
                    return n
            node_info = db.node_info.get(sender)
            if node_info is not None:
                name = node_info.display_name()
                if name:
                    return name
            # Fallback to hex formatting without prefix
            return sender.upper() or "00000000"

        def get_chat_targets():
            targets = []
            for c in db.get_channels():
                targets.append(("channel", c.index))
            dm_nodes = db.get_direct_nodes()
            if current_chat is not None and current_chat[0] == "direct" and current_chat[1] not in dm_nodes:
                dm_nodes.append(current_chat[1])
            for node_hex in sorted({n for n in dm_nodes}):
                targets.append(("direct", node_hex))
            return targets

        def set_current_chat(target):
            nonlocal current_chat
            current_chat = target
            ui.current_chat = target

        def _is_direct_packet(pkt):
            to_num = pkt.get("to")
            if to_num is None:
                return False
            return int(to_num) != BROADCAST_NUM

        def _direct_peer_hex(pkt):
            to_num = pkt.get("to")
            from_num = pkt.get("from")
            if to_num is None or from_num is None:
                return None
            if int(to_num) == BROADCAST_NUM:
                return None
            my_hex = device.my_node_id or ""
            if my_hex == "00000000":
                return None
            try:
                my_num = int(my_hex, 16)
            except Exception:
                return None
            if from_num == my_num:
                peer = to_num
            elif to_num == my_num:
                peer = from_num
            else:
                return None
            return f"{peer & 0xFFFFFFFF:08x}"

        def _direct_peer_hex_or_sender(pkt):
            peer_hex = _direct_peer_hex(pkt)
            if peer_hex is not None:
                return peer_hex
            sender = pkt.get("from")
            if sender is not None:
                return f"{sender & 0xFFFFFFFF:08x}"
            to_num = pkt.get("to")
            if to_num is None or int(to_num) == BROADCAST_NUM:
                return None
            return f"{int(to_num) & 0xFFFFFFFF:08x}"

        def update_status():
            parts = []

            # Channel list with current highlighted
            chan_parts = []
            for c in db.get_channels():
                name = c.name or f"Channel {c.index}"
                label = name
                if current_chat is not None and current_chat[0] == "channel" and current_chat[1] == c.index:
                    label = f"[{name}]"
                chan_parts.append(label)
            dm_nodes = db.get_direct_nodes()
            if current_chat is not None and current_chat[0] == "direct" and current_chat[1] not in dm_nodes:
                dm_nodes.append(current_chat[1])
            if dm_nodes:
                if current_chat is not None and current_chat[0] == "direct":
                    name = resolve_sender_name(current_chat[1])
                    chan_parts.append(f"[DM:{name}]")
                else:
                    chan_parts.append("DM")
            if chan_parts:
                parts.append("Channels: " + " ".join(chan_parts))

            if chan_parts:
                parts.append("Ctrl+N/P to switch")

            # Summary of current modem settings
            config_msg = []
            if lora_preset_name is not None:
                config_msg.append(f"preset={lora_preset_name}")
            if lora_region_name is not None:
                config_msg.append(f"region={lora_region_name}")
            if config_msg:
                parts.append(", ".join(config_msg))

            parts.append("Ctrl+G for node list")
            if not connected:
                parts.append(f"offline: reconnecting {args.transport} ...")

            status = "  |  ".join(parts)
            ui.set_status(status)

        update_status()
        # Seed LoRa status from cached device config (device.start may have consumed config frames)
        def set_lora_labels(lora):
            nonlocal lora_preset_name, lora_region_name
            if not isinstance(lora, dict):
                return
            region = lora.get('region')
            preset = lora.get('modem_preset')
            if region is not None:
                lora_region_name = REGION_NAMES.get(region) or str(region)
            if preset is not None:
                lora_preset_name = PRESET_NAMES.get(preset) or str(preset)

        lc = db.lora_config
        if connected and isinstance(lc, dict):
            set_lora_labels(lc)
            update_status()

        targets = get_chat_targets()
        set_current_chat(targets[0] if targets else None)
        update_status()

        def _render_message(msg):
            if msg.get("type") in ("Warning", "KeyInfo"):
                text = msg.get("text") or ""
                if text:
                    ui.add_message(text, ts_epoch=msg.get("ts"))
                return
            raw = msg.get("raw_packet")
            if not raw:
                return
            try:
                buf = base64.b64decode(raw.encode("ascii"))
            except Exception:
                return
            decoded = pb.decode(buf, FROMRADIO_SCHEMA) if msg.get("type") == "FromRadio" else pb.decode(buf, TORADIO_SCHEMA)
            pkt = (decoded or {}).get("packet") or {}
            dec = pkt.get("decoded") or {}
            if dec.get("portnum") != 1:
                return
            payload_b = dec.get("payload") or b""
            if isinstance(payload_b, (bytes, bytearray)):
                text = bytes(payload_b).decode("utf-8", errors="replace")
            elif isinstance(payload_b, str):
                text = payload_b
            else:
                text = ""
            ts = msg.get("ts")
            if msg.get("type") == "ToRadio":
                name = msg.get("sender_long_name") or msg.get("sender_short_name") or my_display_name()
            else:
                sender = msg.get("from")
                name = resolve_sender_name(sender, msg)
            if not text:
                return
            if current_chat is None:
                return
            chat_type, chat_id = current_chat
            if _is_direct_packet(pkt):
                peer_hex = _direct_peer_hex_or_sender(pkt)
                if chat_type == "direct" and peer_hex == chat_id:
                    ui.add_message(f"{name}: {text}", ts_epoch=ts)
            else:
                ch_idx = pkt.get("channel") or 0
                if chat_type == "channel" and ch_idx == chat_id:
                    ui.add_message(f"{name}: {text}", ts_epoch=ts)

        def reload_chat_messages():
            # Rebuild messages for the currently selected chat
            ui.messages = []
            if current_chat is None:
                return
            chat_type, chat_id = current_chat
            if chat_type == "channel":
                recent = db.get_messages(channel=chat_id)
            elif chat_type == "direct":
                recent = db.get_direct_messages(chat_id)
            else:
                return
            for msg in recent:
                _render_message(msg)

        # Initial load of recent messages for current chat
        reload_chat_messages()

        # Event-driven input handling and redraws
        loop = asyncio.get_running_loop()
        key_queue = asyncio.Queue()
        needs_redraw = asyncio.Event()

        # Message pump: pull from DB facade and dispatch to UI/state
        async def radio_worker():
            nonlocal connected, lora_preset_name, lora_region_name
            try:
                while True:
                    if not connected:
                        await asyncio.sleep(0.5)
                        continue
                    try:
                        fr = await db.next_fromradio()
                    except ConnectionError as e:
                        connected = False
                        logger.warning("chat: radio_worker disconnect: %r", e)
                        update_status()
                        needs_redraw.set()
                        # Pause reads until connection_worker marks connected again
                        continue
                    # Packet: render text messages for current chat; also refresh nodes view
                    if isinstance(fr, dict) and fr.get("packet"):
                        pkt = fr.get("packet") or {}
                        dec = pkt.get("decoded") or {}
                        if dec.get("portnum") == 1:
                            payload = dec.get("payload") or b""
                            if isinstance(payload, (bytes, bytearray)):
                                text = bytes(payload).decode("utf-8", errors="replace")
                            elif isinstance(payload, str):
                                text = payload
                            else:
                                text = ""
                            ts = pkt.get("rx_time")
                            sender = pkt.get("from")
                            if sender is None:
                                sender_hex = ""
                            else:
                                sender_hex = f"{sender & 0xFFFFFFFF:08x}"
                            name = resolve_sender_name(sender_hex)
                            if text:
                                if _is_direct_packet(pkt):
                                    peer_hex = _direct_peer_hex_or_sender(pkt)
                                    if current_chat is not None and current_chat[0] == "direct" and peer_hex == current_chat[1]:
                                        ui.add_message(f"{name}: {text}", ts_epoch=ts if ts else None)
                                        needs_redraw.set()
                                    if peer_hex:
                                        update_status()
                                        needs_redraw.set()
                                else:
                                    ch_idx = pkt.get("channel") or 0
                                    if current_chat is not None and current_chat[0] == "channel" and ch_idx == current_chat[1]:
                                        ui.add_message(f"{name}: {text}", ts_epoch=ts if ts else None)
                                        needs_redraw.set()
                        # While viewing nodes, reflect last-heard changes promptly
                        if ui.mode == "nodes":
                            needs_redraw.set()
                        continue
                    # Node info updates: names may change -> rebuild messages
                    if isinstance(fr, dict) and fr.get("node_info"):
                        reload_chat_messages()
                        needs_redraw.set()
                        continue
                    # Config updates: reflect LoRa region/preset
                    if isinstance(fr, dict) and fr.get("config"):
                        cfg = fr.get("config") or {}
                        lora = cfg.get("lora") if isinstance(cfg, dict) else None
                        if lora:
                            set_lora_labels(lora)
                            update_status()
                            needs_redraw.set()
                        continue
                    # Channel list updates: refresh status bar
                    if isinstance(fr, dict) and fr.get("channel"):
                        update_status()
                        needs_redraw.set()
                        continue
            except asyncio.CancelledError:
                return

        def on_stdin_ready():
            # Drain all pending keys and queue them
            while True:
                try:
                    ch = ui.stdscr.getch()
                except Exception:
                    ch = -1
                if ch == -1:
                    break
                try:
                    key_queue.put_nowait(ch)
                except asyncio.QueueFull:
                    break
            needs_redraw.set()

        loop.add_reader(sys.stdin.fileno(), on_stdin_ready)
        try:
            loop.add_signal_handler(signal.SIGWINCH, needs_redraw.set)
        except Exception:
            # Signals may not be available (e.g., on some platforms)
            pass

        async def input_worker():
            nonlocal current_chat
            try:
                while True:
                    ch = await key_queue.get()
                    line = ui.handle_key(ch)
                    if ui.open_dm_node:
                        set_current_chat(("direct", ui.open_dm_node))
                        ui.open_dm_node = None
                        ui.scroll_offset = 0
                        update_status()
                        reload_chat_messages()
                        needs_redraw.set()
                    if line is not None and line:
                        if not connected:
                            ui.set_status("Offline: cannot send")
                        elif current_chat is None:
                            ui.set_status("Not sent: no chat selected")
                        else:
                            try:
                                chat_type, chat_id = current_chat
                                if chat_type == "channel":
                                    await db.send_text(line, chat_id)
                                elif chat_type == "direct":
                                    await db.send_direct_text(line, int(chat_id, 16))
                                else:
                                    ui.set_status("Select a DM to send")
                                    needs_redraw.set()
                                    continue
                                reload_chat_messages()
                                needs_redraw.set()
                            except Exception as e:
                                ui.set_status(f"Send error: {e}")
                    # Handle Ctrl+N / Ctrl+P chat switching
                    if ui.switch_chat is not None and ui.mode == "chat":
                        targets = get_chat_targets()
                        if targets:
                            if current_chat is None or current_chat not in targets:
                                set_current_chat(targets[0])
                            else:
                                pos = targets.index(current_chat)
                                pos = (pos + ui.switch_chat) % len(targets)
                                set_current_chat(targets[pos])
                            ui.scroll_offset = 0
                            update_status()
                            # Rebuild messages for selected chat
                            reload_chat_messages()
                        ui.switch_chat = None
                    needs_redraw.set()
            except asyncio.CancelledError:
                return

        async def draw_worker():
            nonlocal current_chat
            try:
                while True:
                    await needs_redraw.wait()
                    needs_redraw.clear()
                    # Handle terminal resize proactively (without waiting for KEY_RESIZE)
                    try:
                        ts = os.get_terminal_size(sys.stdin.fileno())
                        new_h, new_w = ts.lines, ts.columns
                        if hasattr(curses, "resizeterm"):
                            curses.resizeterm(new_h, new_w)
                        elif hasattr(curses, "resize_term"):
                            curses.resize_term(new_h, new_w)
                    except Exception:
                        # Best-effort; proceed with current size
                        pass
                    chs = db.get_channels()
                    if ui.mode == "chat":
                        targets = get_chat_targets()
                        if current_chat is None:
                            set_current_chat(targets[0] if targets else None)
                            ui.scroll_offset = 0
                            update_status()
                            reload_chat_messages()
                        elif current_chat[0] == "channel" and current_chat not in targets:
                            set_current_chat(targets[0] if targets else None)
                            ui.scroll_offset = 0
                            update_status()
                            reload_chat_messages()
                        ui.draw_messages(chs, db.node_info)
                    elif ui.mode == "nodes":
                        # Build DM counts for node list display.
                        dm_counts = {}
                        for node_hex in db.get_direct_nodes():
                            dm_counts[node_hex] = len(db.get_direct_messages(node_hex))
                        ui.draw_nodes(db.node_info.items(), dm_counts)
            except asyncio.CancelledError:
                return

        async def refresh_worker():
            try:
                while True:
                    await asyncio.sleep(5)
                    if ui.mode == "nodes":
                        needs_redraw.set()
            except asyncio.CancelledError:
                return

        async def connection_worker():
            nonlocal connected
            # Periodically attempt (re)connect when offline
            try:
                while True:
                    if not connected:
                        # Show reconnecting state and attempt every 5 seconds
                        update_status()
                        needs_redraw.set()
                        await asyncio.sleep(5)
                        try:
                            logger.debug("chat: reconnect attempt via db.start()")
                            await db.start()
                            connected = True
                            logger.info("chat: reconnected")
                            update_status()
                            # After connecting, refresh channels and redraw
                            needs_redraw.set()
                        except Exception as e:
                            # Stay offline and try again
                            logger.warning("chat: reconnect failed: %r", e)
                    else:
                        await asyncio.sleep(1)
            except asyncio.CancelledError:
                return

        # Start workers and perform an initial draw
        needs_redraw.set()
        try:
            await asyncio.gather(
                input_worker(),
                draw_worker(),
                refresh_worker(),
                connection_worker(),
                radio_worker(),
            )
        except asyncio.CancelledError:
            pass
        finally:
            loop.remove_reader(sys.stdin.fileno())
    except KeyboardInterrupt:
        pass
    finally:
        try:
            await db.close()
        finally:
            ui.teardown()
            curses.endwin()


def main():
    p = argparse.ArgumentParser(description="Minimal curses chat for Meshtastic (serial/BLE/TCP)")
    p.add_argument("--data-dir", help="Directory used for chat data", default=None)
    p.add_argument("--debug-log", help="Enable debug logging to this file", default=None)
    p.add_argument("--transport", choices=["ble", "serial", "tcp"], default="serial")

    # BLE options
    p.add_argument("--ble-address", help="BLE MAC address", default=None)
    p.add_argument("--ble-adapter", help="BLE adapter name (e.g. hci0)", default=None)

    # Serial options
    p.add_argument("--serial-port", help="Serial port, e.g. /dev/ttyACM0", default="/dev/ttyACM0")
    p.add_argument("--serial-baudrate", type=int, default=115200)

    # TCP options
    p.add_argument("--tcp-host", help="TCP host for Meshtastic")
    p.add_argument("--tcp-port", help="TCP port for Meshtastic", type=int, default=4403)

    args = p.parse_args()

    if args.transport == "tcp" and not args.tcp_host:
        p.error("--tcp-host is required when --transport tcp")

    # Configure logging: only enable when --debug-log is provided.
    # Otherwise, suppress all logging to avoid breaking the curses UI.
    if args.debug_log:
        logging.basicConfig(
            filename=args.debug_log,
            filemode="a",
            level=logging.DEBUG,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )
        logger.info("chat: debug logging enabled")
    else:
        # Disable all logging output (including WARNING/ERROR) when not debugging
        logging.disable(logging.CRITICAL)

    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        # Graceful Ctrl+C: suppress traceback after cleanup in main_async
        pass


if __name__ == "__main__":
    main()
