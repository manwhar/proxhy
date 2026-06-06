"""
Outline boundaries for "You can't place blocks here!"
regions around bases and diamond/emerald generators.
"""

from petty.events import listen_server, subscribe
from petty.protocol.datatypes import Buffer, Double, VarInt, Int

from proxhy.utils import uuid_version
from gamestate.models import Entity, Player
from gamestate.state import GameState
from plugins.statcheck import GamePlayer

import time
import asyncio


class BoundariesPlugin:
    def _init_boundaries(self):
        self.gamestate: GameState
        self.last_game_start: float = float("-inf")
        self.entities_teleported: dict[str, tuple[float, float, float]] = {}
        self.team_spawnpoints: dict[str, tuple[float, float, float]] = {}

    def game_recently_started(self, window: float = 2.0):
        # game started less than `window` seconds ago
        return time.time() - self.last_game_start < window

    @listen_server(0x18)  # on entity teleport
    async def save_player_spawnpoints(self, buff: Buffer):
        self.downstream.send_packet(0x18, buff.getvalue())

        if not self.game_recently_started():
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

        if self.game_recently_started():
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
