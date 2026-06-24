# Файл: database.py

import enum
from datetime import datetime
from typing import Optional, List

from sqlalchemy import (
    create_engine, Column, Integer, String, Boolean, DateTime,
    ForeignKey, Table, Text, Float, Enum as SAEnum, BigInteger, func
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, Session
from config import DATABASE_URL

# --- Настройка SQLAlchemy ---
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()


# --- Модели таблиц ---

class MatchStatus(enum.Enum):
    CREATED = 'created'
    DRAFTING = 'drafting'
    ACTIVE = 'active'
    FINISHED = 'finished'
    CANCELLED = 'cancelled'


match_players_table = Table(
    'match_players', Base.metadata,
    Column('match_id', ForeignKey('matches.id'), primary_key=True),
    Column('player_id', ForeignKey('players.id'), primary_key=True),
)


class Player(Base):
    __tablename__ = 'players'
    id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, nullable=True, unique=True)
    tg_username = Column(String, nullable=True)
    nickname = Column(String, nullable=False, unique=True)
    full_name = Column(String, nullable=False)
    position = Column(String, nullable=True)
    rating = Column(Float, default=0.0)

    def __repr__(self):
        return f"<Player nickname={self.nickname}>"


class Tournament(Base):
    __tablename__ = 'tournaments'
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    is_active = Column(Boolean, default=True)
    match_format = Column(String, default="BO1")  # "BO1" или "BO2"


class TournamentPairing(Base):
    __tablename__ = 'tournament_pairings'
    id = Column(Integer, primary_key=True)
    tournament_id = Column(Integer, ForeignKey('tournaments.id'))
    slot_id = Column(Integer, nullable=True)  # ID слота в турнирной сетке (от 1 до N-1)
    stage = Column(String, nullable=False)  # например: "1/8", "1/4", "ФИНАЛ"
    captain_a_id = Column(BigInteger, nullable=True)  # Сделано nullable для пустых ячеек ожидания
    captain_b_id = Column(BigInteger, nullable=True)  # Сделано nullable для пустых ячеек ожидания
    is_completed = Column(Boolean, default=False)
    manual_score_a = Column(Integer, nullable=True)
    manual_score_b = Column(Integer, nullable=True)
    manual_score_text = Column(String, nullable=True)  # Текстовый результат для удобных импортов

    tournament = relationship('Tournament', backref='pairings')
    matches = relationship('Match', back_populates='pairing', order_by='Match.created_at')


class Match(Base):
    __tablename__ = 'matches'
    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    admin_id = Column(BigInteger, nullable=False)
    captain_a_id = Column(BigInteger, nullable=False)
    captain_b_id = Column(BigInteger, nullable=False)
    status = Column(SAEnum(MatchStatus), default=MatchStatus.CREATED)
    is_return_match = Column(Boolean, default=False)
    score_a = Column(Integer, nullable=True)
    score_b = Column(Integer, nullable=True)
    players = relationship('Player', secondary=match_players_table)
    draft = relationship('DraftState', back_populates='match', uselist=False)

    # Связь с турнирной сеткой
    tournament_pairing_id = Column(Integer, ForeignKey('tournament_pairings.id'), nullable=True)
    pairing = relationship('TournamentPairing', back_populates='matches')


class DraftState(Base):
    __tablename__ = 'draft_states'
    id = Column(Integer, primary_key=True)
    match_id = Column(Integer, ForeignKey('matches.id'), unique=True)
    match = relationship('Match', back_populates='draft')
    turn = Column(String, default='a')
    pool = Column(Text, default='')
    team_a = Column(Text, default='')
    team_b = Column(Text, default='')
    rps_winner = Column(String, nullable=True)
    rps_choice_a = Column(String, nullable=True)
    rps_choice_b = Column(String, nullable=True)
    team_a_color = Column(String, default="🔴")
    team_b_color = Column(String, default="🔵")

    def get_pool_list(self):
        return [int(x) for x in self.pool.split(',') if x] if self.pool else []

    def set_pool_list(self, lst):
        self.pool = ','.join(map(str, lst))

    def get_team_list(self, side):
        raw = self.team_a if side == 'a' else self.team_b
        return [int(x) for x in raw.split(',') if x] if raw else []

    def set_team_list(self, side, lst):
        s = ','.join(map(str, lst))
        setattr(self, f'team_{side}', s)


class Action(Base):
    __tablename__ = 'actions'
    id = Column(Integer, primary_key=True)
    match_id = Column(Integer, ForeignKey('matches.id'))
    player_id = Column(Integer, ForeignKey('players.id'))
    action_type = Column(String)
    minute = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


def create_db_tables():
    Base.metadata.create_all(engine)


# --- Вспомогательные функции для доступа к данным (DAO) ---

def find_players(session: Session, query: str) -> List[Player]:
    search_query = f"%{query}%"
    return session.query(Player).filter(
        (Player.nickname.ilike(search_query)) |
        (Player.full_name.ilike(search_query)) |
        (Player.tg_username.ilike(search_query))
    ).all()


def get_player_by_nickname(session: Session, nickname: str) -> Optional[Player]:
    return session.query(Player).filter(Player.nickname == nickname).first()


def get_player_by_tgid(session: Session, tg_id: int) -> Optional[Player]:
    return session.query(Player).filter(Player.tg_id == tg_id).first()


def get_top_scorers_or_assisters(session: Session, action_type: str, limit: int = 5) -> List[tuple[Player, int]]:
    return session.query(
        Player,
        func.count(Action.id).label('count')
    ).join(Action, Player.id == Action.player_id) \
        .filter(Action.action_type == action_type) \
        .group_by(Player.id) \
        .order_by(func.count(Action.id).desc()) \
        .limit(limit) \
        .all()


def get_top_most_frequent_players(session: Session, limit: int = 10) -> List[tuple[Player, int]]:
    return session.query(
        Player,
        func.count(match_players_table.c.match_id).label('match_count')
    ).join(match_players_table, Player.id == match_players_table.c.player_id) \
        .group_by(Player.id) \
        .order_by(func.count(match_players_table.c.match_id).desc()) \
        .limit(limit) \
        .all()


def get_top_winners(session: Session, limit: int = 5) -> List[tuple[Player, int]]:
    all_players = session.query(Player).all()
    player_wins = []

    for player in all_players:
        player_matches = session.query(Match).join(match_players_table).filter(
            match_players_table.c.player_id == player.id,
            Match.status == MatchStatus.FINISHED
        ).all()

        wins = 0
        for match in player_matches:
            team_a_ids = match.draft.get_team_list('a')
            is_in_team_a = player.id in team_a_ids
            if (is_in_team_a and match.score_a > match.score_b) or \
                    (not is_in_team_a and match.score_b > match.score_a):
                wins += 1

        if wins > 0:
            player_wins.append((player, wins))

    player_wins.sort(key=lambda item: item[1], reverse=True)
    return player_wins[:limit]