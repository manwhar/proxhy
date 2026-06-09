"""
Outline boundaries for "You can't place blocks here!"
regions around bases and diamond/emerald generators.
"""

from plugins.commands import command
from typing import overload
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
from gamestate.models import Player
from gamestate.state import GameState
from plugins.statcheck import GamePlayer
# from plugins.commands import command

from typing import Literal, TYPE_CHECKING

from collections import deque

import time
import asyncio
import math

if TYPE_CHECKING:
    from proxhy.plugin import ProxhyPlugin


class BoundariesPlugin:
    def _init_boundaries(self: ProxhyPlugin):
        self.gamestate: GameState
        self.last_game_start: float = float("-inf")

        # x, y, z, yaw (looking)
        self.entities_teleported: dict[
            str, tuple[float, float, float, Literal[0, 90, -90, 180, -180]]
        ] = {}
        self.team_spawnpoints: dict[
            str, tuple[float, float, float, Literal[0, 90, -90, 180, -180]]
        ] = {}
        self.recently_placed: deque[Pos] = deque(maxlen=10)
        self.placed_mappings: deque[int] = deque(maxlen=10)

        # developer flag to enable features that make it
        # easier to get the boundary positions on new maps
        self.log_boundaries = True

        # corners relative to spawn position
        # based on direction facing when you spawned in
        self.boundary_corner_1 = Pos(0, 0, 0)
        self.boundary_corner_2 = Pos(0, 0, 0)

    def game_recently_started(self: ProxhyPlugin, window: float = 2.0) -> bool:
        # game started less than `window` seconds ago
        return time.time() - self.last_game_start < window

    def validate_yaw(
        self: ProxhyPlugin, yaw: int | float
    ) -> Literal[0, 90, -90, 180, -180] | None:
        yaw = int(yaw)
        if yaw not in {0, 90, -90, 180, -180}:
            self.logger.warning(f"Received yaw on a non-90 degree increment! ({yaw})")
            return
        return yaw

    @listen_server(0x18)  # on entity teleport
    async def save_player_spawnpoints(self: ProxhyPlugin, buff: Buffer):
        self.downstream.send_packet(0x18, buff.getvalue())

        if (
            self.game_recently_started()
            and self.log_boundaries
            and self.game.gametype == "bedwars"
        ):
            entity_id = buff.unpack(VarInt)
            entity = self.gamestate.get_entity(entity_id)
            if entity is None:
                return
            entity_uuid = entity.uuid

            # if entity exists and is not an npc
            if uuid_version(entity_uuid) != 2:
                # divide by 32 because of stupid chud datatype fixed point number
                x = buff.unpack(Int) / 32.0
                y = buff.unpack(Int) / 32.0
                z = buff.unpack(Int) / 32.0

                yaw = buff.unpack(Angle)
                yaw = self.validate_yaw(yaw)

                if yaw is None:
                    return

                if isinstance(entity, Player):
                    self.entities_teleported[entity.name] = (x, y, z, yaw)
                else:  # fallback in case idk
                    player = self.gamestate.get_player_by_uuid(entity_uuid)
                    if player is not None:
                        self.entities_teleported[player.name] = (x, y, z, yaw)

            await asyncio.sleep(1)  # wait for teams to populate

            print(f"{len(self.entities_teleported)} entities teleported.")
            for e in list(self.entities_teleported.keys()):
                # wrap in list to avoid deleting from dict white iterating
                try:
                    game_player: GamePlayer = self.game_players[e]
                    if e not in self.real_players():
                        raise KeyError

                    team = game_player.team.name.lower()
                    if team in self.team_spawnpoints:
                        continue

                    x, y, z, yaw = self.entities_teleported[e]

                    self.team_spawnpoints[team] = (x, y, z, yaw)
                except KeyError:
                    # for redundancy, clean dict of non-player entities that might've snuck through
                    del self.entities_teleported[e]

            await asyncio.sleep(0.5)
            print(f"{len(self.team_spawnpoints)} spawnpoints logged.")

    @listen_server(0x08, blocking=True)  # player move and look packet
    async def read_own_spawnpoint(self: ProxhyPlugin, buff: Buffer):
        self.downstream.send_packet(0x08, buff.getvalue())

        if (
            self.game_recently_started()
            and self.log_boundaries
            and self.game.gametype == "bedwars"
        ):
            x = buff.unpack(Double)
            y = buff.unpack(Double)
            z = buff.unpack(Double)

            yaw = self.validate_yaw(buff.unpack(Float))
            if yaw is None:
                return

            await asyncio.sleep(1)  # make sure teams have populated

            own_team = self.get_own_team_info()
            self.team_spawnpoints[own_team.name] = (x, y, z, yaw)
            print(f"Got own spawnpoint: ({x}, {y}, {z}); yaw={yaw}")

    @subscribe(r"chat:server:The game starts in 1 second!")
    async def received_game_start_chat(self: ProxhyPlugin, match, buff: Buffer):
        self.downstream.send_packet(0x02, buff.getvalue())

        # reset team dicts at the start of the game
        self.entities_teleported = {}
        self.team_spawnpoints = {}

        self.last_game_start = time.time()

    @staticmethod
    @overload
    def get_offset_position(pos: Pos, face: Literal[255]) -> None: ...

    @staticmethod
    @overload
    def get_offset_position(pos: Pos, face: Literal[0, 1, 2, 3, 4, 5]) -> Pos: ...

    @staticmethod
    def get_offset_position(
        pos: Pos, face: Literal[0, 1, 2, 3, 4, 5, 255]
    ) -> Pos | None:
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
            case 255:
                return None

    @staticmethod
    def get_relative_pos_yaw(
        pos1: Pos, pos2: Pos, yaw: Literal[0, 90, -90, 180, -180]
    ) -> Pos:
        # pos1 is arbitrary position
        # pos2 is centered position with yaw
        # 1. Calculate standard world deltas
        dx = pos1.x - pos2.x
        dy = pos1.y - pos2.y
        dz = pos1.z - pos2.z

        # 2. Translate world deltas to local deltas based on yaw
        if yaw == 0:
            # Facing +Z: Forward is +Z, Right is +X
            local_x = dx
            local_z = dz
        elif yaw == 90:
            # Facing -X: Forward is -X, Right is +Z
            local_x = dz
            local_z = -dx
        elif yaw == 180 or yaw == -180:
            # Facing -Z: Forward is -Z, Right is -X
            local_x = -dx
            local_z = -dz
        elif yaw == -90:
            # Facing +X: Forward is +X, Right is -Z
            local_x = -dz
            local_z = dx
        else:
            raise ValueError(f"Unexpected yaw value: {yaw}")

        return Pos(local_x, dy, local_z)

    @listen_client(0x08, blocking=True)
    async def placed_block(self: ProxhyPlugin, buff: Buffer):
        self.upstream.send_packet(0x08, buff.getvalue())

        if self.log_boundaries and self.game.gametype == "bedwars":
            pos = buff.unpack(Position)
            face = buff.unpack(Byte)
            if face not in {0, 1, 2, 3, 4, 5}:
                return

            held_item = buff.unpack(Slot)

            if held_item.item and face != 255:
                adj_pos = self.get_offset_position(pos, face)
                self.recently_placed.appendleft(adj_pos)
                self.placed_mappings.appendleft(held_item.item.id)

    @command("getboundary")
    async def get_boundary(self: ProxhyPlugin):
        bc1x, bc1y, bc1z = (
            self.boundary_corner_1.x,
            self.boundary_corner_1.y,
            self.boundary_corner_1.z,
        )

        bc2x, bc2y, bc2z = (
            self.boundary_corner_2.x,
            self.boundary_corner_2.y,
            self.boundary_corner_2.z,
        )

        self.downstream.chat(
            f"Current boundary: ({bc1x}, {bc1y}, {bc1z}) -> ({bc2x}, {bc2y}, {bc2z})"
        )

    def update_boundary_size(self: ProxhyPlugin, block_deleted: int, pos: Pos):
        if block_deleted != 35:  # wool
            return

        if not self.team_spawnpoints:
            self.downstream.chat("We don't have any spawnpoint positions!")
            return

        # how many blocks away do we accept that we could still be in a base
        max_dist = 20

        # find nearest base center w/ manhattan distance because spawn
        # block placement protections are cuboids
        distances = [
            (
                team,
                yaw,
                (pos.x - spawn_x) + (pos.y - spawn_y) + (pos.z - spawn_z),
            )
            for team, (spawn_x, spawn_y, spawn_z, yaw) in self.team_spawnpoints.items()
        ]
        closest_team, yaw, min_dist = min(distances, key=lambda key: key[2])

        # we're more than max_dist blocks from the nearest base spawnpoint.
        # probably at a diamond gen or something, so don't expand the radius
        if min_dist > max_dist:
            return

        # check if the block deleted is already inside the known region
        spawn_x = int(self.team_spawnpoints[closest_team][0])
        spawn_y = int(self.team_spawnpoints[closest_team][1])
        spawn_z = int(self.team_spawnpoints[closest_team][2])
        yaw = self.validate_yaw((self.team_spawnpoints[closest_team][3]))
        if yaw:
            rel_pos = self.get_relative_pos_yaw(
                pos, Pos(spawn_x, spawn_y, spawn_z), yaw
            )
        else:
            return

        bc1x, bc1y, bc1z = (
            self.boundary_corner_1.x,
            self.boundary_corner_1.y,
            self.boundary_corner_1.z,
        )

        bc2x, bc2y, bc2z = (
            self.boundary_corner_2.x,
            self.boundary_corner_2.y,
            self.boundary_corner_2.z,
        )

        inside_x = bc1x <= rel_pos.x <= bc2x
        inside_y = bc1y <= rel_pos.y <= bc2y
        inside_z = bc1z <= rel_pos.z <= bc2z

        if inside_x and inside_y and inside_z:
            # we are already inside the boundary! no action required
            self.downstream.chat("Already inside the boundary.")
            return
        else:
            # expand boundary
            self.downstream.chat("Expanding boundary!")
            if not inside_x:
                if rel_pos.x > 0:
                    self.boundary_corner_1.x = rel_pos.x
                if rel_pos.x < 0:
                    self.boundary_corner_2.x = rel_pos.x
            if not inside_y:
                if rel_pos.y > 0:
                    self.boundary_corner_1.y = rel_pos.y
                if rel_pos.y < 0:
                    self.boundary_corner_2.y = rel_pos.y
            if not inside_z:
                if rel_pos.z > 0:
                    self.boundary_corner_1.z = rel_pos.z
                if rel_pos.z < 0:
                    self.boundary_corner_2.z = rel_pos.z

            self.downstream.chat("")

        # TODO: add map data for base block restriction bounding boxes relative to
        # spawn positions
        # TODO: import those bounding boxes; check whether passed in position is inside
        # that region. if not, make sure it's within a certain range (figure out what that should be?)
        # TODO: if we pass validation above, expand the boundary for that map and save locally
        # TODO: implement /updateboundary command that saves the newly expanded boundary to
        # the map data json file so we can start placing the boundaries on the edges of the build limit

    @listen_server(0x23, blocking=True)
    async def block_changed(self: ProxhyPlugin, buff: Buffer):
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

                self.downstream.chat(f"Server deleted your §9{block_deleted} block!")
                self.update_boundary_size(block_deleted, pos)

    @listen_server(0x22, blocking=True)
    async def multi_block_change(self: ProxhyPlugin, buff: Buffer):
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

                    self.update_boundary_size(block_deleted, pos)

                    self.downstream.chat(
                        f"Server deleted your §9{block_deleted} block! §e(multi-block change)"
                    )

    async def place_boundary(
        self: ProxhyPlugin,
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
