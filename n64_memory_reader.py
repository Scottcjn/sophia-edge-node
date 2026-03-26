#!/usr/bin/env python3
"""
rustchain-arcade: N64 RDRAM Memory Reader via RetroArch Network Commands

Connects to RetroArch's Network Command Interface (UDP port 55355) to read
N64 RDRAM state from the running Legend of Elya ROM. Parses the GameCtx
struct fields at known offsets to detect in-game events.

RetroArch must be started with --cmd-port 55355 or have
network_cmd_enable = "true" in retroarch.cfg.

Protocol reference:
  Send:  READ_CORE_RAM <hex_addr> <byte_count>\n
  Recv:  READ_CORE_RAM <hex_addr> <hex_bytes>\n

All addresses are relative to N64 RDRAM base (0x00000000 in core memory,
which maps to 0x80000000 in MIPS virtual address space).
"""

import logging
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("n64-memory-reader")

# ---------------------------------------------------------------------------
# GameCtx struct field offsets (from legend_of_elya.c)
#
# These offsets are relative to the start of the static GameCtx G variable
# in RDRAM.  The base address of G must be determined from the ROM's symbol
# table or by scanning for a known sentinel pattern.
#
# N64 is big-endian MIPS — all multi-byte reads must be interpreted as BE.
#
# Offset calculation (N64 MIPS gcc, -O2, aligned structs):
#   GameState state;            +0x000  (4 bytes, enum = int)
#   int dialog_char;            +0x004  (4)
#   int dialog_done;            +0x008  (4)
#   uint8_t dialog_buf[128];    +0x00C  (128)
#   int dialog_len;             +0x08C  (4)
#   int frame;                  +0x090  (4)
#   uint32_t anniversary_cp0;   +0x094  (4)
#   RoomID current_room;        +0x098  (4, enum = int)
#   int transition_timer;       +0x09C  (4)
#   RoomID transition_target;   +0x0A0  (4)
#   int dialog_select_idx;      +0x0A4  (4)
#   int current_npc;            +0x0A8  (4)
#   int player_x;               +0x0AC  (4)
#   int player_y;               +0x0B0  (4)
#   Spark sparks[12];           +0x0B4  (12 * 6 = 72, but aligned to 2 => 72)
#                               Spark = {int16 x,y; int8 dx,dy; uint8 life} = 7 -> pad to 8?
#                               Actually packed: 2+2+1+1+1 = 7 bytes per spark, but C struct
#                               alignment: int16_t aligned to 2, so sizeof(Spark) = 8 (padded)
#                               12 * 8 = 96 bytes
#   ... (SGAIState ai, SGAIKVCache kv are huge — we skip to simpler fields)
#
# For practical tracking, we only need the first ~180 bytes of GameCtx.
# The fields after sparks[] involve the AI state (several MB) — we don't
# need those for achievement detection.
#
# Key fields for achievement tracking:
#   state           @ +0x000   (int32 BE)  — GameState enum
#   dialog_done     @ +0x008   (int32 BE)  — 1 when dialog complete
#   dialog_buf      @ +0x00C   (128 bytes) — current dialog text
#   dialog_len      @ +0x08C   (int32 BE)  — dialog buffer length
#   frame           @ +0x090   (int32 BE)  — frame counter
#   current_room    @ +0x098   (int32 BE)  — RoomID enum (0/1/2)
#   current_npc     @ +0x0A8   (int32 BE)  — NPC index (-1/0/1/2)
# ---------------------------------------------------------------------------

# GameState enum values (must match legend_of_elya.c)
STATE_ANNIVERSARY = 0
STATE_TITLE = 1
STATE_DUNGEON = 2
STATE_DIALOG_SELECT = 3
STATE_DIALOG = 4
STATE_GENERATING = 5
STATE_KEYBOARD = 6
STATE_ROOM_TRANSITION = 7

# RoomID enum values
ROOM_DUNGEON = 0
ROOM_LIBRARY = 1
ROOM_FORGE = 2

ROOM_NAMES = {
    ROOM_DUNGEON: "Crystal Dungeon",
    ROOM_LIBRARY: "Arcane Library",
    ROOM_FORGE: "Ember Forge",
}

STATE_NAMES = {
    STATE_ANNIVERSARY: "anniversary",
    STATE_TITLE: "title",
    STATE_DUNGEON: "dungeon",
    STATE_DIALOG_SELECT: "dialog_select",
    STATE_DIALOG: "dialog",
    STATE_GENERATING: "generating",
    STATE_KEYBOARD: "keyboard",
    STATE_ROOM_TRANSITION: "room_transition",
}

# Struct field offsets relative to GameCtx base in RDRAM
OFFSET_STATE = 0x000
OFFSET_DIALOG_CHAR = 0x004
OFFSET_DIALOG_DONE = 0x008
OFFSET_DIALOG_BUF = 0x00C
OFFSET_DIALOG_LEN = 0x08C
OFFSET_FRAME = 0x090
OFFSET_CURRENT_ROOM = 0x098
OFFSET_TRANSITION_TIMER = 0x09C
OFFSET_DIALOG_SELECT_IDX = 0x0A4
OFFSET_CURRENT_NPC = 0x0A8
OFFSET_PLAYER_X = 0x0AC
OFFSET_PLAYER_Y = 0x0B0

# We need gen_out_count for token tracking.  It lives after sparks[] and
# the AI structs.  Rather than computing the exact offset through the
# massive SGAIState/SGAIKVCache, we scan for it via a secondary read
# using a known pattern.  However, for the v1 bridge we track tokens
# by counting completed dialogs * average tokens, which is reliable
# without needing the deep struct offset.
#
# If you have the exact offset from `nm legend_of_elya.elf | grep gen_out_count`
# or from a GDB session, set it via N64_GEN_OUT_COUNT_OFFSET env var.


@dataclass
class N64GameState:
    """Snapshot of Legend of Elya game state read from RDRAM."""
    state: int = STATE_TITLE
    dialog_char: int = 0
    dialog_done: int = 0
    dialog_len: int = 0
    dialog_text: str = ""
    frame: int = 0
    current_room: int = ROOM_DUNGEON
    transition_timer: int = 0
    dialog_select_idx: int = 0
    current_npc: int = -1
    player_x: int = 0
    player_y: int = 0
    read_ok: bool = False
    timestamp: float = field(default_factory=time.time)

    @property
    def state_name(self) -> str:
        return STATE_NAMES.get(self.state, f"unknown({self.state})")

    @property
    def room_name(self) -> str:
        return ROOM_NAMES.get(self.current_room, f"room_{self.current_room}")

    @property
    def is_in_dialog(self) -> bool:
        return self.state in (STATE_DIALOG, STATE_GENERATING, STATE_DIALOG_SELECT)

    @property
    def is_keyboard_open(self) -> bool:
        return self.state == STATE_KEYBOARD

    @property
    def is_playing(self) -> bool:
        return self.state not in (STATE_ANNIVERSARY, STATE_TITLE)


class RetroArchMemoryReader:
    """Read N64 RDRAM via RetroArch Network Command Interface (UDP)."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 55355,
        timeout: float = 1.0,
        game_ctx_base: int = 0,
    ):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.game_ctx_base = game_ctx_base
        self._sock: Optional[socket.socket] = None

    def connect(self) -> bool:
        """Create UDP socket for RetroArch commands."""
        try:
            if self._sock is not None:
                self._sock.close()
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.settimeout(self.timeout)
            # Send a test read to verify connection
            test = self._send_cmd("GET_STATUS")
            if test is not None:
                log.info("Connected to RetroArch at %s:%d", self.host, self.port)
                return True
            log.warning("RetroArch at %s:%d not responding", self.host, self.port)
            return False
        except OSError as e:
            log.error("Failed to connect to RetroArch: %s", e)
            return False

    def close(self) -> None:
        """Close the UDP socket."""
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def _send_cmd(self, cmd: str) -> Optional[str]:
        """Send a command to RetroArch and return the response string."""
        if self._sock is None:
            return None
        try:
            self._sock.sendto(
                (cmd + "\n").encode("ascii"),
                (self.host, self.port),
            )
            data, _ = self._sock.recvfrom(65536)
            return data.decode("ascii", errors="replace").strip()
        except socket.timeout:
            return None
        except OSError as e:
            log.debug("UDP send/recv error: %s", e)
            return None

    def read_core_ram(self, addr: int, size: int) -> Optional[bytes]:
        """Read bytes from N64 RDRAM via READ_CORE_RAM command.

        addr: byte offset from RDRAM base (0x00000000)
        size: number of bytes to read (max ~256 per command for reliability)

        Returns raw bytes in big-endian order, or None on failure.
        """
        cmd = f"READ_CORE_RAM {addr:x} {size}"
        resp = self._send_cmd(cmd)
        if resp is None:
            return None

        # Response format: "READ_CORE_RAM <addr> <hex_bytes>"
        # hex_bytes is space-separated hex byte values or a single hex string
        parts = resp.split(" ", 2)
        if len(parts) < 3 or parts[0] != "READ_CORE_RAM":
            log.debug("Unexpected response: %s", resp[:80])
            return None

        hex_str = parts[2].replace(" ", "")
        try:
            return bytes.fromhex(hex_str)
        except ValueError:
            log.debug("Failed to parse hex bytes: %s", hex_str[:40])
            return None

    def read_int32_be(self, addr: int) -> Optional[int]:
        """Read a 32-bit big-endian signed integer from RDRAM."""
        raw = self.read_core_ram(addr, 4)
        if raw is None or len(raw) < 4:
            return None
        return struct.unpack(">i", raw)[0]

    def read_uint32_be(self, addr: int) -> Optional[int]:
        """Read a 32-bit big-endian unsigned integer from RDRAM."""
        raw = self.read_core_ram(addr, 4)
        if raw is None or len(raw) < 4:
            return None
        return struct.unpack(">I", raw)[0]

    def read_game_state(self) -> N64GameState:
        """Read the full GameCtx snapshot from RDRAM.

        Reads the key fields needed for achievement tracking in a
        minimal number of UDP round-trips by batching adjacent fields.
        """
        gs = N64GameState()
        base = self.game_ctx_base

        # Batch read: offset 0x000 through 0x0B4 (180 bytes covers all
        # fields up to player_y inclusive)
        raw = self.read_core_ram(base, 0x0B4)
        if raw is None or len(raw) < 0x0B4:
            gs.read_ok = False
            return gs

        # Parse fields from the raw buffer (big-endian)
        gs.state = struct.unpack_from(">i", raw, OFFSET_STATE)[0]
        gs.dialog_char = struct.unpack_from(">i", raw, OFFSET_DIALOG_CHAR)[0]
        gs.dialog_done = struct.unpack_from(">i", raw, OFFSET_DIALOG_DONE)[0]
        gs.dialog_len = struct.unpack_from(">i", raw, OFFSET_DIALOG_LEN)[0]
        gs.frame = struct.unpack_from(">i", raw, OFFSET_FRAME)[0]
        gs.current_room = struct.unpack_from(">i", raw, OFFSET_CURRENT_ROOM)[0]
        gs.transition_timer = struct.unpack_from(">i", raw, OFFSET_TRANSITION_TIMER)[0]
        gs.dialog_select_idx = struct.unpack_from(">i", raw, OFFSET_DIALOG_SELECT_IDX)[0]
        gs.current_npc = struct.unpack_from(">i", raw, OFFSET_CURRENT_NPC)[0]
        gs.player_x = struct.unpack_from(">i", raw, OFFSET_PLAYER_X)[0]
        gs.player_y = struct.unpack_from(">i", raw, OFFSET_PLAYER_Y)[0]

        # Extract dialog text (null-terminated ASCII from dialog_buf)
        buf_end = OFFSET_DIALOG_BUF + 128
        dialog_raw = raw[OFFSET_DIALOG_BUF:buf_end]
        try:
            null_idx = dialog_raw.index(0)
            gs.dialog_text = dialog_raw[:null_idx].decode("ascii", errors="replace")
        except ValueError:
            gs.dialog_text = dialog_raw.decode("ascii", errors="replace")

        gs.read_ok = True
        gs.timestamp = time.time()
        return gs

    def scan_for_game_ctx(self, search_start: int = 0x00000000,
                          search_end: int = 0x00800000,
                          step: int = 0x1000) -> Optional[int]:
        """Scan RDRAM for the GameCtx struct by looking for valid state patterns.

        Searches for a region where:
          - state field is a valid GameState enum (0-7)
          - frame field is a positive integer
          - current_room is a valid RoomID (0-2)

        This is a slow scan used only during initialization. Returns the
        base address of GameCtx in RDRAM, or None if not found.
        """
        log.info("Scanning RDRAM for GameCtx struct (0x%06x - 0x%06x)...",
                 search_start, search_end)

        for addr in range(search_start, search_end, step):
            raw = self.read_core_ram(addr, 0x0B4)
            if raw is None or len(raw) < 0x0B4:
                continue

            state = struct.unpack_from(">i", raw, OFFSET_STATE)[0]
            frame = struct.unpack_from(">i", raw, OFFSET_FRAME)[0]
            room = struct.unpack_from(">i", raw, OFFSET_CURRENT_ROOM)[0]
            npc = struct.unpack_from(">i", raw, OFFSET_CURRENT_NPC)[0]

            # Validate: state must be valid enum, frame positive, room valid
            if (0 <= state <= 7
                    and 0 < frame < 0x7FFFFFFF
                    and 0 <= room <= 2
                    and -1 <= npc <= 2):
                log.info("Found candidate GameCtx at RDRAM 0x%06x "
                         "(state=%d, frame=%d, room=%d, npc=%d)",
                         addr, state, frame, room, npc)
                return addr

        log.warning("GameCtx not found in RDRAM scan range")
        return None


def detect_game_ctx_address(reader: RetroArchMemoryReader) -> Optional[int]:
    """Try known addresses first, then fall back to scanning.

    The Legend of Elya static GameCtx G is typically placed in .bss
    by the N64 linker. Common locations depend on the libdragon build,
    but typically fall in the 0x80100000-0x80300000 range (RDRAM offsets
    0x00100000-0x00300000).
    """
    import os
    # Allow override via environment variable
    env_addr = os.environ.get("N64_GAMECTX_ADDR")
    if env_addr:
        addr = int(env_addr, 0)
        log.info("Using GameCtx address from env: 0x%06x", addr)
        reader.game_ctx_base = addr
        test = reader.read_game_state()
        if test.read_ok:
            return addr

    # Try common BSS locations for libdragon ROMs
    common_addrs = [
        0x00100000,  # Typical small ROM BSS
        0x00080000,  # Alternate BSS start
        0x00200000,  # Larger ROM BSS
        0x00050000,  # Early BSS
    ]

    for addr in common_addrs:
        reader.game_ctx_base = addr
        gs = reader.read_game_state()
        if (gs.read_ok
                and 0 <= gs.state <= 7
                and gs.frame > 0
                and 0 <= gs.current_room <= 2):
            log.info("GameCtx found at known address 0x%06x", addr)
            return addr

    # Fall back to full scan
    return reader.scan_for_game_ctx()


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------

def main():
    """Test the memory reader by connecting and polling game state."""
    import argparse
    parser = argparse.ArgumentParser(description="N64 RDRAM Memory Reader")
    parser.add_argument("--host", default="127.0.0.1", help="RetroArch host")
    parser.add_argument("--port", type=int, default=55355, help="RetroArch UDP port")
    parser.add_argument("--addr", default=None, help="GameCtx RDRAM address (hex)")
    parser.add_argument("--scan", action="store_true", help="Scan for GameCtx address")
    args = parser.parse_args()

    reader = RetroArchMemoryReader(host=args.host, port=args.port)
    if not reader.connect():
        print("ERROR: Could not connect to RetroArch. Is it running with network commands enabled?")
        print("  Start RetroArch with: retroarch --cmd-port 55355")
        print("  Or set network_cmd_enable = true in retroarch.cfg")
        return

    if args.addr:
        reader.game_ctx_base = int(args.addr, 0)
    elif args.scan:
        addr = reader.scan_for_game_ctx()
        if addr is not None:
            reader.game_ctx_base = addr
            print(f"GameCtx found at 0x{addr:06x}")
        else:
            print("GameCtx not found. Try specifying --addr manually.")
            return
    else:
        addr = detect_game_ctx_address(reader)
        if addr is not None:
            reader.game_ctx_base = addr
        else:
            print("GameCtx not found. Try --scan or --addr <hex>")
            return

    print(f"\nPolling game state from RDRAM 0x{reader.game_ctx_base:06x}...\n")
    try:
        prev_state = None
        while True:
            gs = reader.read_game_state()
            if not gs.read_ok:
                print("  [read failed]")
                time.sleep(1)
                continue

            # Only print when state changes
            summary = (gs.state, gs.current_room, gs.current_npc, gs.dialog_done)
            if summary != prev_state:
                print(f"  State: {gs.state_name:16s} | Room: {gs.room_name:16s} | "
                      f"NPC: {gs.current_npc:2d} | Frame: {gs.frame:6d} | "
                      f"Dialog: {gs.dialog_done}")
                if gs.dialog_text:
                    print(f"    Text: {gs.dialog_text[:60]}")
                prev_state = summary

            time.sleep(0.25)  # 4 Hz poll rate
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        reader.close()


if __name__ == "__main__":
    main()
