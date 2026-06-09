"""
Outline boundaries for "You can't place blocks here!"
regions around bases and diamond/emerald generators.
"""

from petty.events import listen_server, subscribe, listen_client
from petty.protocol.datatypes import (
    Angle,
    Buffer,
    Double,
    VarInt,
    Int,
    Short,
    UnsignedByte,
    Float,
    Position,
    Slot,
    Byte,
    Pos,
)

from proxhy.utils import uuid_version
from gamestate.models import Entity, Player
from gamestate.state import GameState
from plugins.statcheck import GamePlayer
# from plugins.commands import command

from typing import Literal

from collections import deque

import time
import asyncio


class BoundariesPlugin:
    def _init_boundaries(self):
        self.gamestate: GameState
        self.last_game_start: float = float("-inf")
        self.entities_teleported: dict[str, tuple[float, float, float]] = {}
        self.team_spawnpoints: dict[str, tuple[float, float, float]] = {}
        self.recently_placed: deque[Pos] = deque(maxlen=10)
        self.placed_mappings: deque[int] = deque(maxlen=10)

        # developer flag to enable features that make it
        # easier to get the boundary positions on new maps
        self.log_boundaries = True

    def game_recently_started(self, window: float = 2.0) -> bool:
        # game started less than `window` seconds ago
        return time.time() - self.last_game_start < window

    @listen_server(0x18)  # on entity teleport
    async def save_player_spawnpoints(self, buff: Buffer):
        self.downstream.send_packet(0x18, buff.getvalue())

        if (
            self.game_recently_started()
            and self.log_boundaries
            and self.game.gametype == "bedwars"
        ):
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

    @listen_server(0x08, blocking=True)  # player move and look packet
    async def read_own_spawnpoint(self, buff: Buffer):
        self.downstream.send_packet(0x08, buff.getvalue())

        if (
            self.game_recently_started()
            and self.log_boundaries
            and self.game.gametype == "bedwars"
        ):
            x = buff.unpack(Double)
            y = buff.unpack(Double)
            z = buff.unpack(Double)

            await asyncio.sleep(0.5)  # make sure teams have populated

            own_team = self.get_own_team_info()
            self.team_spawnpoints[own_team.name] = (x, y, z)

    @subscribe(r"chat:server:The game starts in 1 second!")
    async def received_game_start_chat(self, match, buff: Buffer):
        self.downstream.send_packet(0x02, buff.getvalue())

        # reset team dicts at the start of the game
        self.entities_teleported = {}
        self.team_spawnpoints = {}

        self.last_game_start = time.time()

    @staticmethod
    def get_offset_position(pos: Pos, face: int) -> Pos:
        match face:
            case 0:
                return Pos(pos.x, pos.y - 1, pos.z)
            case 1:
                return Pos(pos.x, pos.y + 1, pos.z)
            case 2:
                return Pos(pos.x, pos.y, pos.z - 1)
            case 3:
                return Pos(pos.x, pos.y, pos.z + 1)
            case 4:
                return Pos(pos.x - 1, pos.y, pos.z)
            case 5:
                return Pos(pos.x + 1, pos.y, pos.z)

    @listen_client(0x08, blocking=True)
    async def placed_block(self, buff: Buffer):
        self.upstream.send_packet(0x08, buff.getvalue())

        if self.log_boundaries and self.game.gametype == "bedwars":
            pos = buff.unpack(Position)
            face = buff.unpack(Byte)
            held_item = buff.unpack(Slot)

            if held_item.item and face != 255:
                adj_pos = self.get_offset_position(pos, face)
                self.recently_placed.appendleft(adj_pos)
                self.placed_mappings.appendleft(held_item.item.id)

    def save_boundary(self, block_deleted: int, pos: Pos):
        if block_deleted == 35:  # wool
            ...
        # TODO: add map data for base block restriction bounding boxes relative to
        # spawn positions
        # TODO: import those bounding boxes; check whether passed in position is inside
        # that region. if not, make sure it's within a certain range (figure out what that should be?)
        # TODO: if we pass validation above, expand the boundary for that map and save locally
        # TODO: implement /updateboundary command that saves the newly expanded boundary to
        # the map data json file so we can start placing the boundaries on the edges of the build limit

    @listen_server(0x23, blocking=True)
    async def block_changed(self, buff: Buffer):
        self.downstream.send_packet(0x23, buff.getvalue())

        if self.log_boundaries and self.game.gametype == "bedwars":
            pos = buff.unpack(Position)
            block_id = buff.unpack(VarInt)
            block_type = block_id >> 4
            # block_meta = block_id & 15

            # if the block is air (id=0) and was a block we recently placed
            # then the server deleted one of our recently placed blocks
            if block_type == 0 and pos in self.recently_placed:
                deque_id = self.recently_placed.index(pos)
                block_deleted = self.placed_mappings[deque_id]

                self.save_boundary(block_deleted, pos)

                self.downstream.chat(f"Server deleted your §9{block_deleted} block!")

    @listen_server(0x22, blocking=True)
    async def multi_block_change(self, buff: Buffer):
        self.downstream.send_packet(0x22, buff.getvalue())

        if self.log_boundaries and self.game.gametype == "bedwars":
            chunk_x, chunk_z = buff.unpack(Int), buff.unpack(Int)
            record_count = buff.unpack(VarInt)

            for b in range(record_count):
                xz_pos = buff.unpack(UnsignedByte)
                y = buff.unpack(UnsignedByte)

                rel_x_pos = (0xF0 & xz_pos) >> 4
                rel_z_pos = 0x0F & xz_pos

                x = chunk_x * 16 + rel_x_pos
                z = chunk_z * 16 + rel_z_pos

                block_id = buff.unpack(VarInt)
                pos = Pos(x, y, z)

                if block_id == 0 and pos in self.recently_placed:
                    deque_id = self.recently_placed.index(pos)
                    block_deleted = self.placed_mappings[deque_id]

                    self.save_boundary(block_deleted, pos)

                    self.downstream.chat(
                        f"Server deleted your §9{block_deleted} block! §e(multi-block change)"
                    )

    async def place_boundary(
        self,
        pos: tuple[float, float, float],
        b_type: Literal["corner", "edge"],
        rot: tuple[float, float, float] = (0.0, 0.0, 0.0),
    ):
        # armor stand internal id: 1E; network ID: 4E

        x_adjust = int(pos[0] * 32)  # "fixed-point number"
        y_adjust = int(pos[1] * 32)
        z_adjust = int(pos[2] * 32)

        rot_x = rot[0]
        rot_y = rot[1]
        rot_z = rot[2]

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
            # metadata inject
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
        metadata = 0 if b_type == "edge" else 1
        self.downstream.send_packet(
            0x04,
            VarInt.pack(999),  # Entity ID (must match the spawn packet)
            Short.pack(4),  # Slot: 4 (Helmet)
            # -- SLOT DATA --
            Short.pack(97),  # Item ID: Monster Egg
            UnsignedByte.pack(1),  # Item Count: 1
            Short.pack(metadata),  # Item Damage/Metadata: 0/1 (stone/cobblestone)
            UnsignedByte.pack(0),  # NBT Terminator (Empty NBT compound)
        )
