import traceback
import socket
import json
import threading
import time
import random
import logging
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, asdict, field
from enum import Enum, auto
from collections import defaultdict

# Server Configuration
SERVER_PORT = 5000
BUFFER_SIZE = 4096
ROUND_LIMIT = 15
MIN_PLAYERS_PER_TEAM = 2
MAX_PLAYERS_PER_TEAM = 4
ROUND_TIME_LIMIT = 180  # seconds
PREPARATION_TIME = 10  # seconds
INACTIVE_TIMEOUT = 30   # seconds

# Game Constants
INITIAL_HP = 100
INITIAL_MP = 100
TOTEM_CAPTURE_TIME = 10
TOTEM_CAPTURE_RADIUS = 1.0
MP_REGEN_RATE = 5
HP_REGEN_DELAY = 5
HP_REGEN_RATE = 5

# Logging Configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(threadName)s: %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

def log_call(func):
    def wrapper(*args, **kwargs):
        namm = func.__name__
        if namm in ["end_round","start_round"]:
          print(f"Вызов функции: {func.__name__}")
          #print(f"Аргументы позиционные: {args}")
          #print(f"Аргументы именованные: {kwargs}")
        result = func(*args, **kwargs)
        #print(result)
        return result
    return wrapper

class GameState(Enum):
    WAITING = auto()
    PREPARATION = auto()
    IN_PROGRESS = auto()
    ROUND_END = auto()
    GAME_OVER = auto()

@dataclass
class PlayerStats:
    hp: int = INITIAL_HP
    mp: int = INITIAL_MP
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    damage_dealt: int = 0
    healing_done: int = 0
    position: List[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    ready: bool = False
    last_damage_time: float = 0
    last_position_update: float = 0
    skill_cooldowns: Dict[str, float] = field(default_factory=dict)
    active_effects: Dict[str, Dict[str, Any]] = field(default_factory=dict)

@dataclass
class Skill:
    name: str
    damage: int
    cooldown: float
    mp_cost: int
    range: float
    area_effect: bool = False
    heal: int = 0
    effects: Dict[str, Any] = field(default_factory=dict)

@dataclass
class TotemState:
    active: bool = True
    position: List[float] = field(default_factory=lambda: [20.0, 0.0, 20.0])
    capturing_team: Optional[str] = None
    capture_progress: float = 0
    last_capture_time: float = 0
    contested: bool = False

@dataclass
class RoomState:
    id: str
    players: List[Any]
    teams: Dict[str, List[Any]]
    scores: Dict[str, int] = field(default_factory=lambda: {"Team A": 0, "Team B": 0})
    ready_status: Dict[Any, bool] = field(default_factory=dict)
    state: GameState = GameState.WAITING
    current_round: int = 0
    round_start_time: float = 0
    round_wins: Dict[str, int] = field(default_factory=lambda: {"Team A": 0, "Team B": 0})
    totem_state: Optional[TotemState] = None
    last_state_update: float = 0
    respawn_queue: List[Tuple[Any, float]] = field(default_factory=list)

class GameServer:
    def __init__(self, ip: str, port: int):
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.server_socket.bind((ip, port))
        
        self.clients: Dict[Any, Dict[str, Any]] = {}
        self.rooms: Dict[str, RoomState] = {}
        self.lobby_queue: List[Any] = []
        
        self.skills_config = {
            "Power Slash": Skill(
                name="Power Slash",
                damage=40,
                cooldown=3,
                mp_cost=10,
                range=2.0,
                effects={"knockback": 2.0}
            ),
            "Fireball": Skill(
                name="Fireball",
                damage=50,
                cooldown=5,
                mp_cost=20,
                range=8.0,
                area_effect=True,
                effects={"burn": {"damage": 20, "duration": 3}}
            ),
            "Healing Wave": Skill(
                name="Healing Wave",
                damage=0,
                cooldown=10,
                mp_cost=25,
                range=5.0,
                heal=3,
                area_effect=True,
                effects={"regeneration": {"healing": 3, "duration": 3}}
            ),
            "Shield Bash": Skill(
                name="Shield Bash",
                damage=15,
                cooldown=4,
                mp_cost=15,
                range=2.5,
                effects={"stun": {"duration": 1.5}}
            )
        }
        
        self.lock = threading.Lock()
        self.running = True
        
        self.start_management_threads()
        logger.info(f"Server started on {ip}:{port}")

    def start_management_threads(self):
        """Start all management threads for the server."""
        threads = [
            (self.manage_lobby, "LobbyManager"),
            (self.update_games, "GameUpdater"),
            (self.check_inactive_players_loop, "InactiveChecker"),
            (self.process_respawn_queue, "RespawnManager"),
            (self.update_player_resources, "ResourceManager")
        ]
        
        self.management_threads = []
        for target, name in threads:
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self.management_threads.append(thread)

    def generate_player_id(self) -> str:
        """Generate a unique player ID."""
        while True:
            player_id = f"player_{random.randint(1000, 9999)}"
            if not any(client["id"] == player_id for client in self.clients.values()):
                return player_id
    @log_call
    def calculate_position_distance(self, pos1: List[float], pos2: List[float]) -> float:
        """Calculate the Euclidean distance between two positions."""
        return sum((a - b) ** 2 for a, b in zip(pos1, pos2)) ** 0.5
    
    @log_call
    def send_message(self, client_address: Any, message: Dict[str, Any]):
        """Send a message to a client with error handling."""
        try:
            data = json.dumps(message).encode()
            self.server_socket.sendto(data, client_address)
        except Exception as e:
            logger.error(f"Error sending message to {client_address}: {e}")
            self.handle_player_leave(client_address)
    
    @log_call
    def broadcast_to_room(self, room_id: str, message: Dict[str, Any], exclude: Optional[Any] = None):
        """Broadcast a message to all players in a room, optionally excluding one."""
        room = self.rooms.get(room_id)
        if room:
            for player in room.players:
                if player != exclude:
                    self.send_message(player, message)

    def get_team_for_player(self, room: RoomState, player: Any) -> Optional[str]:
        """Get the team name for a player in a room."""
        return next((team for team, players in room.teams.items() 
                    if player in players), None)
                    
    def manage_lobby(self):
        """Manage the lobby queue and create rooms when enough players are available."""
        while self.running:
            try:
                with self.lock:
                    if len(self.lobby_queue) >= 4:
                        players_for_room = self.lobby_queue[:4]
                        self.lobby_queue = self.lobby_queue[4:]
                        random.shuffle(players_for_room)
                        room_id = self.create_room(players_for_room)
                        
                        if room_id:
                            logger.info(f"Room {room_id} created with players {[self.clients[p]['name'] for p in players_for_room]}")
                        else:
                            self.lobby_queue.extend(players_for_room)
                            logger.error("Failed to create room, returning players to queue")

                time.sleep(1)
            except Exception as e:
                logger.error(f"Error in lobby management: {e}")

    def get_team_name(self,participant, teams):
      for team_name, members in teams.items():
          if participant in members:
              return team_name
      return None
      
    @log_call
    def create_room(self, players: List[Any]) -> Optional[str]:
        """Create a new game room with balanced teams."""
        try:
            if len(players) != 4:
                return None

            room_id = f"room_{len(self.rooms) + 1}"
            teams = {
                "Team A": players[:2],
                "Team B": players[2:]
            }
            
            room = RoomState(
                id=room_id,
                players=players,
                teams=teams,
                ready_status={p: False for p in players}
            )

            # Initialize player positions
            spawn_positions = {
                "Team A": [[-10.0, 0.0, -10.0], [-8.0, 0.0, -10.0]],
                "Team B": [[10.0, 0.0, 10.0], [8.0, 0.0, 10.0]]
            }

            for team_name, team_players in teams.items():
                for idx, player in enumerate(team_players):
                    self.clients[player]["stats"].position = spawn_positions[team_name][idx]

            self.rooms[room_id] = room
            for player in players:
                my_team = self.get_team_name(player,teams)
                self.send_message(player, {
                    "action": "room_created",
                    "room_id": room_id,
                    "teams": {team: [self.clients[p]["name"] for p in team_players] 
                             for team, team_players in teams.items()},
                    "player_name": self.clients[player]["name"],
                    "spawn_position": self.clients[player]["stats"].position,
                    "my_team":my_team
                })
            return room_id
        except Exception as e:
            logger.error(f"Error creating room: {e}")
            return None

    def update_games(self):
      """Main game update loop for all active rooms."""
      while self.running:
          try:
              current_time = time.time()
              with self.lock:
                  for room_id, room in list(self.rooms.items()):
                      if room.state == GameState.IN_PROGRESS:
                          self.update_game_state(room, current_time)
                          
                          # Check win conditions
                          winner = self.determine_round_winner(room)
                          if winner:
                              self.end_round(room_id, winner)
                              continue
                          
                          # Check time limit
                          if current_time - room.round_start_time > ROUND_TIME_LIMIT:
                              print("******************************")
                              self.end_round(room_id, self.determine_round_winner(room))
                              continue
                          
                          # Update totem state
                          if room.totem_state and room.totem_state.active:
                              self.update_totem_state(room, current_time)
  
              time.sleep(0.05)  # 20 updates per second
          except Exception as e:
              logger.error(f"Error in game update loop: {e}")

    def update_game_state(self, room: RoomState, current_time: float):
        """Update the game state for a room."""
        # Update effects and cooldowns
        for player in room.players:
            stats = self.clients[player]["stats"]
            
            # Update skill cooldowns
            for skill_name in list(stats.skill_cooldowns.keys()):
                if current_time - stats.skill_cooldowns[skill_name] >= self.skills_config[skill_name].cooldown:
                    del stats.skill_cooldowns[skill_name]

            # Update active effects
            for effect_name in list(stats.active_effects.keys()):
                effect = stats.active_effects[effect_name]
                if current_time > effect["end_time"]:
                    del stats.active_effects[effect_name]
                elif effect.get("tick_time", 0) <= current_time:
                    self.apply_effect_tick(player, effect_name, effect)

    def apply_effect_tick(self, player: Any, effect_name: str, effect: Dict[str, Any]):
        """Apply periodic effect ticks to a player."""
        stats = self.clients[player]["stats"]
        current_time = time.time()

        if effect_name == "burn":
            #self.apply_damage(None, player, effect["damage"], is_dot=True)
            pass
        elif effect_name == "regeneration":
            stats.hp = min(INITIAL_HP, stats.hp + effect["healing"])
            stats.healing_done += effect["healing"]

        effect["tick_time"] = current_time + 1.0  # Next tick in 1 second
    
    @log_call
    def update_totem_state(self, room: RoomState, current_time: float):
      """Update totem capture progress and check for capture completion."""
      if not room.totem_state:
          return
  
      totem = room.totem_state
      capturing_players = self.get_players_near_totem(room)
      time_delta = current_time - totem.last_capture_time
      
      totem.contested = False
      
      if not capturing_players:
          if totem.capture_progress > 0:
              decay_rate = 20
              totem.capture_progress = max(0, totem.capture_progress - decay_rate * time_delta)
              totem.capturing_team = None
      else:
          teams_present = {self.get_team_for_player(room, p) for p in capturing_players}
          
          if len(teams_present) == 1:
              team = teams_present.pop()
              if totem.capturing_team != team:
                  totem.capture_progress = 0
              
              totem.capturing_team = team
              capture_rate = 20 * len(capturing_players)
              totem.capture_progress = min(100, totem.capture_progress + capture_rate * time_delta)
              
              if totem.capture_progress >= 100:
                  self.end_round(room.id, team)
                  return
          else:
              totem.contested = True
      
      totem.last_capture_time = current_time
      
      self.broadcast_to_room(room.id, {
          "action": "totem_update",
          "progress": totem.capture_progress,
          "capturing_team": totem.capturing_team,
          "contested": totem.contested
      })


    def get_players_near_totem(self, room: RoomState) -> List[Any]:
      """Get all players within capture range of the totem."""
      if not room.totem_state:
          return []
  
      nearby_players = []
      for player in room.players:
          stats = self.clients[player]["stats"]
          if stats.hp > 0:
              distance = self.calculate_position_distance(
                  stats.position,
                  room.totem_state.position
              )
              if distance <= TOTEM_CAPTURE_RADIUS:
                  nearby_players.append(player)
  
      return nearby_players
        
    @log_call
    def handle_skill_usage(self, room: RoomState, player: Any, skill_name: str, target_position: List[float]):
      """Handle player skill usage."""
      skill = self.skills_config.get(skill_name)
      if not skill:
          return
  
      player_stats = self.clients[player]["stats"]
      current_time = time.time()
  
      if not self.can_use_skill(player_stats, skill, current_time):
          self.send_message(player, {
              "action": "skill_failed",
              "reason": "Cannot use skill now"
          })
          return
  
      distance_to_target = self.calculate_position_distance(
          player_stats.position,
          target_position
      )
      if distance_to_target > skill.range:
          self.send_message(player, {
              "action": "skill_failed",
              "reason": "Target out of range"
          })
          return
  
      player_stats.mp -= skill.mp_cost
      player_stats.skill_cooldowns[skill_name] = current_time
      affected_players = self.get_players_in_range(room, player, target_position, skill)
      
      for target in affected_players:
          if skill.damage > 0:
              self.apply_damage(player, target, skill.damage)
          if skill.heal > 0:
              self.apply_healing(player, target, skill.heal)
          if skill.effects:
              self.apply_skill_effects(player, target, skill.effects)
  
      self.broadcast_to_room(room.id, {
          "action": "skill_used",
          "player": self.clients[player]["name"],
          "skill": skill_name,
          "position": target_position,
          "affected_players": [self.clients[p]["name"] for p in affected_players]
      })
        
        
    @log_call
    def can_use_skill(self, stats: PlayerStats, skill: Skill, current_time: float) -> bool:
        """Check if a player can use a skill."""
        if stats.hp <= 0:
            return False
        if stats.mp < skill.mp_cost:
            return False
        if skill.name in stats.skill_cooldowns:
            if current_time - stats.skill_cooldowns[skill.name] < skill.cooldown:
                return False
        return True
    
    @log_call
    def apply_skill_effects(self, caster: Any, target: Any, effects: Dict[str, Any]):
        """Apply additional effects from skills to a target."""
        target_stats = self.clients[target]["stats"]
        current_time = time.time()

        for effect_name, effect_data in effects.items():
            if isinstance(effect_data, dict):
                duration = effect_data.get("duration", 0)
                target_stats.active_effects[effect_name] = {
                    "start_time": current_time,
                    "end_time": current_time + duration,
                    "tick_time": current_time + 1.0,
                    **effect_data
                }

    def process_respawn_queue(self):
        """Process the respawn queue for all rooms."""
        while self.running:
            try:
                current_time = time.time()
                with self.lock:
                    for room in self.rooms.values():
                        if room.state != GameState.IN_PROGRESS:
                            continue
                            
                        for player, respawn_time in room.respawn_queue[:]:
                            if current_time >= respawn_time:
                                self.respawn_player(room, player)
                                room.respawn_queue.remove((player, respawn_time))

                time.sleep(0.1)
            except Exception as e:
                logger.error(f"Error processing respawn queue: {e}")

    def respawn_player(self, room: RoomState, player: Any):
        """Respawn a player with full health and mana."""
        team = self.get_team_for_player(room, player)
        if not team:
            return

        stats = self.clients[player]["stats"]
        stats.hp = INITIAL_HP
        stats.mp = INITIAL_MP
        stats.active_effects.clear()
        stats.skill_cooldowns.clear()

        # Set spawn position based on team
        spawn_positions = {
            "Team A": [[-10.0, 0.0, -10.0], [-8.0, 0.0, -10.0]],
            "Team B": [[10.0, 0.0, 10.0], [8.0, 0.0, 10.0]]
        }
        stats.position = random.choice(spawn_positions[team])

        self.broadcast_to_room(room.id, {
            "action": "player_respawned",
            "player": self.clients[player]["name"],
            "position": stats.position
        })
        
    @log_call
    def update_player_resources(self):
        """Update player MP and HP regeneration."""
        while self.running:
            try:
                current_time = time.time()
                with self.lock:
                    for room in self.rooms.values():
                        if room.state != GameState.IN_PROGRESS:
                            continue

                        for player in room.players:
                            stats = self.clients[player]["stats"]
                            
                            if stats.hp > 0:
                                stats.mp = min(INITIAL_MP, stats.mp + MP_REGEN_RATE * 0.1)
                            
                            if stats.hp > 0 and stats.hp < INITIAL_HP:
                                if current_time - stats.last_damage_time > HP_REGEN_DELAY:
                                    stats.hp = min(INITIAL_HP, stats.hp + HP_REGEN_RATE * 0.1)

                time.sleep(0.1)  # Update every 100ms
            except Exception as e:
                logger.error(f"Error updating player resources: {e}")

    @log_call
    def check_inactive_players_loop(self):
        """Check for and remove inactive players."""
        while self.running:
            try:
                current_time = time.time()
                with self.lock:
                    inactive_clients = [
                        client_address for client_address, client_data in self.clients.items()
                        if current_time - client_data.get("last_ping", 0) > INACTIVE_TIMEOUT
                    ]

                    for client_address in inactive_clients:
                        self.handle_player_leave(client_address)

                time.sleep(5)  # Check every 5 seconds
            except Exception as e:
                logger.error(f"Error checking inactive players: {e}")
                
    def handle_client_message(self, message_str: str, client_address: Any):
        """Handle incoming client messages."""
        try:
            message = json.loads(message_str)
            action = message.get("action")
    
            if action == "join":
                self.handle_player_join(client_address, message.get("name", "Unknown"))
            elif action == "ping":
                self.handle_ping(client_address)
            elif action == "ready":
                self.handle_player_ready(client_address)
            elif action == "move":
                self.handle_player_move(client_address, message.get("position", [0, 0, 0]))
            elif action == "skill":
                self.handle_skill_request(client_address, message)
            elif action == "leave":
                self.handle_player_leave(client_address)
            else:
                logger.warning(f"Unknown action received: {action}")
    
        except json.JSONDecodeError:
            logger.error(f"Invalid JSON received from {client_address}")
        except Exception as e:
            logger.error(f"Error handling message from {client_address}: {e}")
            
    def handle_player_join(self, client_address: Any, player_name: str):
        """Handle new player connection."""
        with self.lock:
            if client_address in self.clients:
                return
                
            player_id = self.generate_player_id()
            self.clients[client_address] = {
                "id": player_id,
                "name": player_name,
                "stats": PlayerStats(),
                "last_ping": time.time()
            }
            
            self.lobby_queue.append(client_address)
            user_stat = self.clients[client_address]["stats"]
            statt = {
              "hp":user_stat.hp,
              "mp":user_stat.mp,
              "skill_cooldowns":user_stat.skill_cooldowns
            }
            self.send_message(client_address, {
                "action": "join_success",
                "player_id": player_id,
                "queue_position": len(self.lobby_queue),
                "stats":statt
            })
            logger.info(f"Player {player_name} ({player_id}) joined the server")
    
    def handle_ping(self, client_address: Any):
        """Update last ping time for client."""
        if client_address in self.clients:
            self.clients[client_address]["last_ping"] = time.time()
            self.send_message(client_address, {"action": "pong"})
    @log_call
    def handle_player_ready(self, client_address: Any):
        """Handle player ready status."""
        with self.lock:
            room = self.find_player_room(client_address)
            if not room:
                return
    
            room.ready_status[client_address] = True
            
            if all(room.ready_status.values()):
              self.start_round(room.id)
              
    @log_call
    def handle_player_move(self, client_address: Any, new_position: List[float]):
        """Handle player movement updates."""
        with self.lock:
            if client_address not in self.clients:
                return
            try:
               room = self.find_player_room(client_address)
               if not room or room.state != GameState.IN_PROGRESS:
                    return
               stats = self.clients[client_address]["stats"]
                    
               stats.position = new_position
               stats.last_position_update = time.time()
               self.broadcast_to_room(room.id, {
                    "action": "position_update",
                    "player": self.clients[client_address]["name"],
                    "position": new_position
                })
            except Exception as e:
               print(e)
            
    
    @log_call
    def handle_skill_request(self, client_address: Any, message: Dict[str, Any]):
        """Handle skill usage requests."""
        with self.lock:
            room = self.find_player_room(client_address)
            if not room or room.state != GameState.IN_PROGRESS:
                return
    
            skill_name = message.get("skill")
            target_position = message.get("target_position")
            
            if not skill_name or not target_position:
                return
                
            self.handle_skill_usage(room, client_address, skill_name, target_position)
            
    def handle_player_leave(self, client_address: Any):
        """Handle player disconnection."""
        with self.lock:
            if client_address not in self.clients:
                return
                
            player_name = self.clients[client_address]["name"]
            
            if client_address in self.lobby_queue:
                self.lobby_queue.remove(client_address)
                
            room = self.find_player_room(client_address)
            if room:
                self.handle_room_leave(room, client_address)
                
            del self.clients[client_address]
            logger.info(f"Player {player_name} left the server")
    
    def handle_room_leave(self, room: RoomState, client_address: Any):
        """Handle player leaving a room."""
        room.players.remove(client_address)
        
        for team in room.teams.values():
            if client_address in team:
                team.remove(client_address)
                
        # Notify other players
        self.broadcast_to_room(room.id, {
            "action": "player_left",
            "player": self.clients[client_address]["name"]
        })
        
        # Check if room should be closed
        if len(room.players) < MIN_PLAYERS_PER_TEAM * 2:
            self.end_game(room.id, "insufficient_players")
            
    @log_call
    def find_player_room(self, client_address: Any) -> Optional[RoomState]:
        """Find the room a player is currently in."""
        return next((room for room in self.rooms.values() 
                    if client_address in room.players), None)
                    
    @log_call
    def start_round(self, room_id: str):
        """Start a new round in the room."""
        room = self.rooms.get(room_id)
        if not room:
            return
            
        room.current_round += 1
        room.state = GameState.IN_PROGRESS
        room.round_start_time = time.time()
        room.totem_state = TotemState()
        
        for player in room.players:
            stats = self.clients[player]["stats"]
            stats.hp = INITIAL_HP
            stats.mp = INITIAL_MP
            stats.active_effects.clear()
            stats.skill_cooldowns.clear()
        
        self.broadcast_to_room(room_id, {
            "action": "round_start",
            "round": room.current_round,
            "totem_position": room.totem_state.position
        })
        
    @log_call
    def end_round(self, room_id: str, winning_team: Optional[str]):
      """End the current round and update scores."""
      room = self.rooms.get(room_id)
      if not room:
          return
          
      if winning_team:
          room.round_wins[winning_team] += 1
          room.scores[winning_team] += 1
      
      room.state = GameState.ROUND_END
      
      if (room.current_round >= ROUND_LIMIT or
          any(wins >= (ROUND_LIMIT // 2 + 1) for wins in room.round_wins.values())):
          self.end_game(room_id, "normal")
      else:
          self.reset_room_for_next_round(room)
            
    @log_call
    def reset_room_for_next_round(self, room: RoomState):
        """Reset room state for the next round."""
        room.state = GameState.PREPARATION
        room.ready_status = {player: False for player in room.players}
        
        room.totem_state = TotemState()
        room.respawn_queue.clear()
        
        self.broadcast_to_room(room.id, {
            "action": "prepare_round",
            "scores": room.scores,
            "round_wins": room.round_wins
        })
    
    def end_game(self, room_id: str, reason: str):
        """End the game and clean up room."""
        room = self.rooms.get(room_id)
        if not room:
            return
            
        winning_team = max(room.scores.items(), key=lambda x: x[1])[0]
        
        # Send final results to all players
        self.broadcast_to_room(room.id, {
          "action": "game_over",
          "winner": winning_team,
          "scores": room.scores,
          "reason": reason,
          "stats": {
              f"{player[0]}:{player[1]}": {
                  "kills": self.clients[player]["stats"].kills,
                  "deaths": self.clients[player]["stats"].deaths,
                  "assists": self.clients[player]["stats"].assists,
                  "damage_dealt": self.clients[player]["stats"].damage_dealt,
                  "healing_done": self.clients[player]["stats"].healing_done
          } for player in room.players
            }
          })
        
        # Clean up room
        del self.rooms[room_id]
        
    def get_players_in_range(self, room: RoomState, caster: Any, target_position: List[float], skill: Skill) -> List[Any]:
        """Get all players within range of a skill effect."""
        affected_players = []
        caster_team = self.get_team_for_player(room, caster)
        
        for player in room.players:
            if player == caster:
                continue
                
            target_team = self.get_team_for_player(room, player)
            stats = self.clients[player]["stats"]
            
            if stats.hp <= 0:
                continue
                
            distance = self.calculate_position_distance(stats.position, target_position)
            
            if distance <= skill.range:
                if skill.heal > 0 and target_team != caster_team:
                    continue
                    
                if skill.damage > 0 and target_team == caster_team:
                    continue
                    
                affected_players.append(player)
    
        return affected_players
        
    @log_call    
    def apply_damage(self, attacker: Optional[Any], target: Any, damage: int, is_dot: bool = False):
        """Apply damage to a target and handle death."""
        if target not in self.clients:
            return
            
        target_stats = self.clients[target]["stats"]
        if target_stats.hp <= 0:
            return
            
        # Apply damage
        target_stats.hp = max(0, target_stats.hp - damage)
        print(f"HP {target}",target_stats.hp)
        target_stats.last_damage_time = time.time()
        
        if attacker:
            self.clients[attacker]["stats"].damage_dealt += damage
        
        # Handle death
        if target_stats.hp <= 0:
            self.handle_player_death(attacker, target)
            
        room = self.find_player_room(target)
        if room:
            self.broadcast_to_room(room.id, {
                "action": "damage_dealt",
                "target": self.clients[target]["name"],
                "damage": damage,
                "current_hp": target_stats.hp,
                "attacker": self.clients[attacker]["name"] if attacker else None,
                "is_dot": is_dot
            })
    @log_call    
    def handle_player_death(self, killer: Optional[Any], victim: Any):
        """Handle player death and setup respawn."""
        victim_stats = self.clients[victim]["stats"]
        victim_stats.deaths += 1
        
        if killer:
            killer_stats = self.clients[killer]["stats"]
            killer_stats.kills += 1
        
        # Add to respawn queue
        room = self.find_player_room(victim)
        if room:
            respawn_time = time.time() + 5.0  # 5 second respawn timer
            room.respawn_queue.append((victim, respawn_time))
            
            self.broadcast_to_room(room.id, {
                "action": "player_died",
                "victim": self.clients[victim]["name"],
                "killer": self.clients[killer]["name"] if killer else None,
                "respawn_time": respawn_time
            })
            
    @log_call    
    def apply_healing(self, healer: Any, target: Any, amount: int):
        """Apply healing to a target."""
        if target not in self.clients:
            return
            
        target_stats = self.clients[target]["stats"]
        if target_stats.hp <= 0:
            return
            
        old_hp = target_stats.hp
        target_stats.hp = min(INITIAL_HP, target_stats.hp + amount)
        actual_healing = target_stats.hp - old_hp
        
        if healer:
            self.clients[healer]["stats"].healing_done += actual_healing
        
        room = self.find_player_room(target)
        if room:
            self.broadcast_to_room(room.id, {
                "action": "healing_applied",
                "target": self.clients[target]["name"],
                "amount": actual_healing,
                "current_hp": target_stats.hp,
                "healer": self.clients[healer]["name"] if healer else None
            })
    
    @log_call
    def determine_round_winner(self, room: RoomState) -> Optional[str]:
      """Determine the winning team of the current round."""
      # Приоритет захвата тотема
      if room.totem_state and room.totem_state.capture_progress >= 100:
          return room.totem_state.capturing_team
      
      # Подсчет живых игроков по командам
      team_stats = defaultdict(lambda: {"alive_players": 0, "total_hp": 0})
      for player in room.players:
          team = self.get_team_for_player(room, player)
          stats = self.clients[player]["stats"]
          
          if stats.hp > 0:
              team_stats[team]["alive_players"] += 1
              team_stats[team]["total_hp"] += stats.hp
      
      # Проверка команд с живыми игроками
      teams_with_alive_players = [
          team for team, stats in team_stats.items() 
          if stats["alive_players"] > 0
      ]
      
      # Если одна команда полностью уничтожена
      if len(teams_with_alive_players) == 1:
          return teams_with_alive_players[0]
      
      # Если обе команды имеют живых игроков - победителя нет
      if len(teams_with_alive_players) > 1:
          return None
      
      # Если нет живых игроков - ничья
      return None
    
    def server_loop(self):
        """Main server loop for receiving client messages."""
        while self.running:
            try:
                data, client_address = self.server_socket.recvfrom(BUFFER_SIZE)
                threading.Thread(target=self.handle_client_message,
                              args=(data.decode(), client_address)).start()
            except Exception as e:
                logger.error(f"Error in server loop: {e}")
                
    def cleanup(self):
        """Cleanup server resources."""
        self.running = False
        for thread in self.management_threads:
            thread.join(timeout=1.0)
        self.server_socket.close()
        logger.info("Server cleaned up and shut down")

def main():
    server = None
    try:
        server = GameServer("127.0.0.1", SERVER_PORT)
        server.server_loop()
    except KeyboardInterrupt:
        logger.info("Server shutdown initiated by user")
    except Exception as e:
        logger.critical(f"Critical server error: {e}")
        logger.critical(traceback.format_exc())
    finally:
        if server:
            server.cleanup()

if __name__ == "__main__":
    main()