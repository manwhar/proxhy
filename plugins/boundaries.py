"""
Outline boundaries for "You can't place blocks here!"
regions around bases and diamond/emerald generators.
"""

from petty.events import listen_server, subscribe
from petty.protocol.datatypes import (
    Angle,
    Buffer,
    Double,
    VarInt,
    Int,
    Short,
    UnsignedByte,
    Float,
)

from proxhy.utils import uuid_version
from gamestate.models import Entity, Player
from gamestate.state import GameState
from plugins.statcheck import GamePlayer
from plugins.commands import command

import time
import asyncio


class BoundariesPlugin:
    def _init_boundaries(self):
        self.gamestate: GameState
        self.last_game_start: float = float("-inf")
        self.entities_teleported: dict[str, tuple[float, float, float]] = {}
        self.team_spawnpoints: dict[str, tuple[float, float, float]] = {}

    def game_recently_started(self, window: float = 2.0) -> bool:
        # game started less than `window` seconds ago
        return time.time() - self.last_game_start < window

    @listen_server(0x18)  # on entity teleport
    async def save_player_spawnpoints(self, buff: Buffer):
        self.downstream.send_packet(0x18, buff.getvalue())

        if not (self.game_recently_started() or self.game.mode.startswith("bedwars")):
            return

        entity_id = buff.unpack(VarInt)
        entity: Entity = self.gamestate.get_entity(entity_id)
        if entity is None:
            return
        entity_uuid = entity.uuid

        # if entity exists and is not an npc
        if uuid_version(entity_uuid) != 2:
            # divide by 32 because of stupid chud datatype fixed point number
            x = buff.unpack(Int) / 32.0
            y = buff.unpack(Int) / 32.0
            z = buff.unpack(Int) / 32.0

            if isinstance(entity, Player):
                self.entities_teleported[entity.name] = (x, y, z)
            else:  # fallback in case idk
                player = self.gamestate.get_player_by_uuid(entity_uuid)
                if player is not None:
                    self.entities_teleported[player.name] = (x, y, z)

        await asyncio.sleep(0.5)  # wait for teams to populate

        for e in list(self.entities_teleported.keys()):
            # wrap in list to avoid deleting from dict white iterating
            try:
                game_player: GamePlayer = self.game_players[e]
                if e not in self.real_players():
                    raise KeyError

                team = game_player.team.name.lower()
                if team in self.team_spawnpoints:
                    continue

                x, y, z = self.entities_teleported[e]
                self.team_spawnpoints[team] = (x, y, z)
            except KeyError:
                # for redundancy, clean dict of non-player entities that might've snuck through
                del self.entities_teleported[e]

    @listen_server(0x08)  # player move and look packet
    async def read_spawnpoint(self, buff: Buffer):
        self.downstream.send_packet(0x08, buff.getvalue())

        if self.game_recently_started() and self.game.mode.startswith("bedwars"):
            x = buff.unpack(Double)
            y = buff.unpack(Double)
            z = buff.unpack(Double)

            await asyncio.sleep(0.5)  # make sure teams have populated

            own_team = self.get_own_team_info()
            self.team_spawnpoints[own_team.name] = (x, y, z)

    @subscribe(r"chat:server:The game starts in 1 second!")
    async def received_game_start_chat(self, match, buff: Buffer):
        # reset team dicts at the start of the game
        self.entities_teleported = {}
        self.team_spawnpoints = {}

        self.last_game_start = time.time()
        self.downstream.send_packet(0x02, buff.getvalue())

    @command("armorstand")
    async def armor_stand_test(self):
        # armor stand internal id: 1E; network ID: 4E
        pos = self.gamestate.position
        x, y, z = pos.x, pos.y, pos.z  # own coordinates

        x_adjust = int(x * 32)  # "fixed-point number"
        y_adjust = int(y * 32)
        z_adjust = int(z * 32)

        rot_x = 0.0
        rot_y = 0.0
        rot_z = 0.0

        # spawn mob packet
        self.downstream.send_packet(
            0x0F,
            VarInt.pack(999),  # Entity ID
            UnsignedByte.pack(78),  # Type: Armor Stand
            Int.pack(x_adjust),
            Int.pack(y_adjust),
            Int.pack(z_adjust),
            Angle.pack(0),  # Yaw
            Angle.pack(0),  # Pitch
            Angle.pack(0),  # Head Pitch
            Short.pack(0),  # Velocity X
            Short.pack(0),  # Velocity Y
            Short.pack(0),  # Velocity Z
            # -- METADATA INJECTION --
            # Index 0: Invisible
            UnsignedByte.pack(0x00),  # Header: Type 0, Index 0
            UnsignedByte.pack(0x20),  # Value: 0x20 bitmask
            # Index 10: Marker & NoGravity
            UnsignedByte.pack(0x0A),  # Header: Type 0, Index 10
            UnsignedByte.pack(0x12),  # Value: 0x10 (Marker) | 0x02 (NoGravity)
            # Index 11: Head Pose (Vector3f)
            UnsignedByte.pack(0xEB),  # Header: Type 7, Index 11
            Float.pack(rot_x),  # Pitch (X)
            Float.pack(rot_y),  # Yaw (Y)
            Float.pack(rot_z),  # Roll (Z)
            UnsignedByte.pack(0x7F),  # Metadata Terminator
        )

        # entity equipment packet
        self.downstream.send_packet(
            0x04,
            VarInt.pack(999),  # Entity ID (must match the spawn packet)
            Short.pack(4),  # Slot: 4 (Helmet)
            # -- SLOT DATA --
            Short.pack(97),  # Item ID: Monster Egg
            UnsignedByte.pack(1),  # Item Count: 1
            Short.pack(1),  # Item Damage/Metadata: 1 (Cobblestone)
            UnsignedByte.pack(0),  # NBT Terminator (Empty NBT compound)
        )
