"""
Telegram Football Draft Bot
File: tg_football_draft_bot.py
"""

import os
import logging
import enum
from typing import List, Optional
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

# SQLAlchemy (ORM)
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    ForeignKey,
    Enum as SAEnum,
    Table,
    Text,
)
from sqlalchemy.orm import declarative_base, relationship, Session
from config import DATABASE_URL

# --------------------------
# Configuration
# --------------------------
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', '<PUT_YOUR_TOKEN_HERE>')
# DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql://user:pass@localhost:5432/football_bot')

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

Base = declarative_base()

# --------------------------
# Database models
# --------------------------

class MatchStatus(enum.Enum):
    CREATED = 'created'
    DRAFTING = 'drafting'
    ACTIVE = 'active'
    FINISHED = 'finished'
    CANCELLED = 'cancelled'


# Association table: which players are in match pool
match_players_table = Table(
    'match_players', Base.metadata,
    Column('match_id', ForeignKey('matches.id'), primary_key=True),
    Column('player_id', ForeignKey('players.id'), primary_key=True),
)


class Player(Base):
    __tablename__ = 'players'
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    tg_id = Column(Integer, nullable=True)  # if player has telegram account

    def __repr__(self):
        return f"Player(id={self.id}, name='{self.name}')"


class Match(Base):
    __tablename__ = 'matches'
    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    status = Column(SAEnum(MatchStatus), default=MatchStatus.CREATED)
    admin_id = Column(Integer, nullable=False)

    # captain user ids (telegram ids)
    captain_a_id = Column(Integer, nullable=False)
    captain_b_id = Column(Integer, nullable=False)
    is_return_match = Column(Boolean, default=False)

    # result fields
    score_a = Column(Integer, nullable=True)
    score_b = Column(Integer, nullable=True)
    result_text = Column(Text, nullable=True)

    players = relationship('Player', secondary=match_players_table)
    draft = relationship('DraftState', back_populates='match', uselist=False)


class DraftState(Base):
    __tablename__ = 'draft_states'
    id = Column(Integer, primary_key=True)
    match_id = Column(Integer, ForeignKey('matches.id'), unique=True)
    match = relationship('Match', back_populates='draft')

    # turn: 'a' or 'b' who gives pair currently
    turn = Column(String, default='a')
    # remaining pool serialized as comma-separated player ids
    pool = Column(Text, nullable=False, default='')
    # team compositions stored as comma-separated ids
    team_a = Column(Text, nullable=False, default='')
    team_b = Column(Text, nullable=False, default='')

    # pre-draft RPS results
    rps_winner = Column(String, nullable=True)
    rps_choice_a = Column(String, nullable=True)
    rps_choice_b = Column(String, nullable=True)

    def get_pool_list(self):
        return [int(x) for x in self.pool.split(',') if x.strip()] if self.pool else []

    def set_pool_list(self, lst: List[int]):
        self.pool = ','.join(str(x) for x in lst)

    def get_team_list(self, side: str) -> List[int]:
        raw = self.team_a if side == 'a' else self.team_b
        return [int(x) for x in raw.split(',') if x.strip()] if raw else []

    def set_team_list(self, side: str, lst: List[int]):
        if side == 'a':
            self.team_a = ','.join(str(x) for x in lst)
        else:
            self.team_b = ','.join(str(x) for x in lst)


class Action(Base):
    __tablename__ = 'actions'
    id = Column(Integer, primary_key=True)
    match_id = Column(Integer, ForeignKey('matches.id'))
    player_id = Column(Integer, ForeignKey('players.id'))
    action_type = Column(String)  # e.g., 'goal', 'assist'
    minute = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

# --------------------------
# Database setup
# --------------------------
engine = create_engine(DATABASE_URL, echo=False, future=True)
Base.metadata.create_all(engine)

# --------------------------
# Helpers
# --------------------------

def get_player_by_name(session: Session, name: str) -> Optional[Player]:
    return session.query(Player).filter(Player.name.ilike(name)).first()

def add_player(session: Session, name: str, tg_id: Optional[int] = None) -> Player:
    p = get_player_by_name(session, name)
    if p:
        return p
    p = Player(name=name, tg_id=tg_id)
    session.add(p)
    session.commit()
    return p

def format_team(session: Session, team_ids: List[int]) -> str:
    players = session.query(Player).filter(Player.id.in_(team_ids)).all()
    return ", ".join([p.name for p in players])

def get_match(session: Session, match_id: int) -> Optional[Match]:
    return session.query(Match).filter(Match.id == match_id).first()

# --------------------------
# Rock-Paper-Scissors logic
# --------------------------

RPS_CHOICES = ["rock", "paper", "scissors"]

def rps_winner(choice_a: str, choice_b: str) -> Optional[str]:
    if choice_a == choice_b:
        return None
    if (choice_a == "rock" and choice_b == "scissors") or \
       (choice_a == "scissors" and choice_b == "paper") or \
       (choice_a == "paper" and choice_b == "rock"):
        return "a"
    else:
        return "b"

# --------------------------
# Bot commands
# --------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я футбольный бот для организации матчей и драфтов.\n"
        "Команды:\n"
        "/create_match - создать матч\n"
        "/start_draft - начать драфт\n"
        "/finish_match - завершить матч\n"
    )

# Админ создает матч
async def create_match(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            "Использование: /create_match captainA_id captainB_id player1 player2 ... (четное количество игроков)"
        )
        return

    captain_a_name = args[0]
    captain_b_name = args[1]
    player_names = args[2:]

    if len(player_names) % 2 != 0:
        await update.message.reply_text("Ошибка: количество игроков должно быть четным!")
        return

    with Session(engine) as session:
        # Проверка на активный матч
        active = session.query(Match).filter(Match.status != MatchStatus.FINISHED).first()
        if active:
            await update.message.reply_text("Ошибка: уже есть активный матч!")
            return

        captain_a = add_player(session, captain_a_name)
        captain_b = add_player(session, captain_b_name)

        match = Match(
            admin_id=update.message.from_user.id,
            captain_a_id=captain_a.tg_id or 0,
            captain_b_id=captain_b.tg_id or 0,
            status=MatchStatus.CREATED
        )

        for pn in player_names:
            p = add_player(session, pn)
            match.players.append(p)

        session.add(match)
        session.commit()

        await update.message.reply_text(
            f"Матч #{match.id} создан!\n"
            f"Капитаны: {captain_a.name} vs {captain_b.name}\n"
            f"Игроки: {', '.join(player_names)}"
        )

# Начало драфта
async def start_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /start_draft match_id")
        return

    match_id = int(context.args[0])

    with Session(engine) as session:
        match = session.get(Match, match_id)
        if not match:
            await update.message.reply_text("Матч не найден.")
            return
        if match.status != MatchStatus.CREATED:
            await update.message.reply_text("Драфт уже запущен или матч завершён.")
            return

        # Ставим статус драфта
        match.status = MatchStatus.DRAFT
        session.commit()

        if match.is_rematch:
            # Если ответный матч → автоматически определяем победителя по предыдущему
            last_match = (
                session.query(Match)
                .filter(
                    ((Match.captain_a_id == match.captain_a_id) & (Match.captain_b_id == match.captain_b_id)) |
                    ((Match.captain_a_id == match.captain_b_id) & (Match.captain_b_id == match.captain_a_id))
                )
                .filter(Match.id < match.id)
                .order_by(Match.id.desc())
                .first()
            )
            if last_match and last_match.rps_winner:
                match.rps_winner = (
                    match.captain_a_id if last_match.rps_winner == match.captain_b_id else match.captain_b_id
                )
                session.commit()
                await update.message.reply_text(
                    f"Это ответный матч. RPS не проводится. "
                    f"Победителем назначен капитан с id={match.rps_winner}."
                )
                return
            else:
                await update.message.reply_text("Не найден прошлый матч для RPS.")
                return
        else:
            # Обычный матч → запускаем RPS
            await context.bot.send_message(
                chat_id=match.captain_a_id,
                text=f"Матч #{match.id}: сыграйте RPS! Команда: /rps {match.id} rock|paper|scissors"
            )
            await context.bot.send_message(
                chat_id=match.captain_b_id,
                text=f"Матч #{match.id}: сыграйте RPS! Команда: /rps {match.id} rock|paper|scissors"
            )
            await update.message.reply_text(f"Драфт для матча #{match.id} запущен. Ждём RPS.")


# --------------------------
# Draft process
# --------------------------

# Капитан делает выбор в RPS
async def rps_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Использование: /rps match_id rock|paper|scissors")
        return
    match_id = int(args[0])
    choice = args[1].lower()
    if choice not in RPS_CHOICES:
        await update.message.reply_text("Выбери: rock, paper или scissors")
        return

    user_id = update.message.from_user.id

    with Session(engine) as session:
        match = get_match(session, match_id)
        if not match or not match.draft:
            await update.message.reply_text("Матч или драфт не найден")
            return

        draft = match.draft
        if user_id == match.captain_a_id:
            draft.rps_choice_a = choice
        elif user_id == match.captain_b_id:
            draft.rps_choice_b = choice
        else:
            await update.message.reply_text("Вы не капитан!")
            return

        session.commit()

        if draft.rps_choice_a and draft.rps_choice_b:
            winner = rps_winner(draft.rps_choice_a, draft.rps_choice_b)
            if winner:
                draft.rps_winner = winner
                session.commit()
                await update.message.reply_text(
                    f"Игра завершена! Победитель: капитан {winner.upper()}. Он выбирает: давать пару или выбирать."
                )
            else:
                draft.rps_choice_a = None
                draft.rps_choice_b = None
                session.commit()
                await update.message.reply_text("Ничья! Переигровка.")
        else:
            await update.message.reply_text("Ожидаем выбор второго капитана.")

# Победитель выбирает стратегию
async def rps_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Использование: /rps_decision match_id give|choose")
        return
    match_id = int(args[0])
    decision = args[1].lower()
    if decision not in ["give", "choose"]:
        await update.message.reply_text("Доступно только: give или choose")
        return

    with Session(engine) as session:
        match = get_match(session, match_id)
        if not match or not match.draft:
            await update.message.reply_text("Матч не найден")
            return

        draft = match.draft
        if not draft.rps_winner:
            await update.message.reply_text("Сначала надо сыграть RPS")
            return

        if decision == "give":
            draft.turn = draft.rps_winner
        else:  # choose
            draft.turn = "a" if draft.rps_winner == "b" else "b"
        session.commit()

        await update.message.reply_text(f"Старт драфта! Первый ход делает капитан {draft.turn.upper()} (дает пару).")

# Капитан дает пару
async def give_pair(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 3:
        await update.message.reply_text("Использование: /give_pair match_id playerX playerY")
        return
    match_id = int(args[0])
    pair_names = args[1:3]

    with Session(engine) as session:
        match = get_match(session, match_id)
        if not match or not match.draft:
            await update.message.reply_text("Матч не найден")
            return

        draft = match.draft
        user_id = update.message.from_user.id
        expected = match.captain_a_id if draft.turn == "a" else match.captain_b_id
        if user_id != expected:
            await update.message.reply_text("Сейчас не ваш ход")
            return

        # получаем id игроков
        pool = draft.get_pool_list()
        players = []
        for name in pair_names:
            p = get_player_by_name(session, name)
            if not p or p.id not in pool:
                await update.message.reply_text(f"Игрок {name} не найден в пуле")
                return
            players.append(p)

        # сохраняем пару во временный контекст
        context.user_data["pair"] = [p.id for p in players]
        opponent_id = match.captain_b_id if draft.turn == "a" else match.captain_a_id

        # отправляем сопернику клавиатуру
        keyboard = [
            [InlineKeyboardButton(p.name, callback_data=f"pick:{match.id}:{p.id}")]
            for p in players
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await context.bot.send_message(
            chat_id=opponent_id,
            text=f"Выберите одного из игроков из пары",
            reply_markup=reply_markup
        )

# Обработка выбора игрока из пары
async def pick_player(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split(":")
    match_id = int(data[1])
    picked_id = int(data[2])

    with Session(engine) as session:
        match = get_match(session, match_id)
        if not match or not match.draft:
            await query.edit_message_text("Матч не найден")
            return

        draft = match.draft
        opponent_side = "a" if draft.turn == "b" else "b"
        chooser_id = query.from_user.id
        expected = match.captain_b_id if draft.turn == "a" else match.captain_a_id
        if chooser_id != expected:
            await query.edit_message_text("Не ваш выбор!")
            return

        # выбранный игрок идет в команду chooser
        chooser_side = "b" if draft.turn == "a" else "a"
        chooser_team = draft.get_team_list(chooser_side)
        chooser_team.append(picked_id)
        draft.set_team_list(chooser_side, chooser_team)

        # второй игрок идет к дающему
        pair = context.user_data.get("pair", [])
        other_id = [pid for pid in pair if pid != picked_id][0]
        giver_team = draft.get_team_list(draft.turn)
        giver_team.append(other_id)
        draft.set_team_list(draft.turn, giver_team)

        # обновляем пул
        pool = draft.get_pool_list()
        pool = [pid for pid in pool if pid not in pair]
        draft.set_pool_list(pool)

        # смена хода
        draft.turn = "a" if draft.turn == "b" else "b"

        session.commit()

        if not pool:
            match.status = MatchStatus.ACTIVE
            session.commit()
            await query.edit_message_text("Драфт завершен! Матч стартовал.")
        else:
            await query.edit_message_text("Выбор сделан. Теперь другой капитан дает пару.")

# --------------------------
# Завершение матча
# --------------------------

async def finish_match(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 3:
        await update.message.reply_text("Использование: /finish_match match_id scoreA-scoreB")
        return
    match_id = int(args[0])
    score = args[1]

    try:
        score_a, score_b = map(int, score.split("-"))
    except Exception:
        await update.message.reply_text("Неверный формат счета (пример: 3-2)")
        return

    actions = args[2:]  # "player1:goal" "player2:assist" и т.д.

    with Session(engine) as session:
        match = get_match(session, match_id)
        if not match:
            await update.message.reply_text("Матч не найден")
            return

        match.status = MatchStatus.FINISHED
        match.score_a = score_a
        match.score_b = score_b

        for act in actions:
            try:
                name, atype = act.split(":")
            except Exception:
                continue
            player = get_player_by_name(session, name)
            if player:
                session.add(Action(match_id=match.id, player_id=player.id, action_type=atype))

        session.commit()
        await update.message.reply_text(f"Матч #{match.id} завершен! Счет {score_a}-{score_b}")


import os
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler
)

from bot_commands import (
    start, add_player, create_match, rps_choice, rps_decision,
    give_pair, pick_player, finish_match
)

TOKEN = os.getenv("BOT_TOKEN")  # Токен бота из переменных окружения

def main():
    app = ApplicationBuilder().token(TOKEN).build()

    # --- 1. Диалоговые обработчики (ConversationHandlers) ---
    # Порядок важен: диалоги регистрируются перед обычными текстовыми командами
    app.add_handler(plan_match_handler)  # Пошаговое планирование матча (/plan_match)
    app.add_handler(edit_profile_handler)  # Изменение данных профиля (/edit_profile)
    app.add_handler(create_match_handler)  # Пошаговое создание матча на основе плана (/create_match)

    # --- 2. Публичные команды (доступны всем в любом чате) ---
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("my_chat_id", get_chat_id))
    app.add_handler(CommandHandler("top_scorers", top_scorers))
    app.add_handler(CommandHandler("top_assisters", top_assisters))
    app.add_handler(CommandHandler("top_frequent", top_frequent_players))
    app.add_handler(CommandHandler("top_winners", top_winners))

    # --- 3. Команды для ЛС (для обычных пользователей) ---
    app.add_handler(CommandHandler("register", register_player))
    app.add_handler(CommandHandler("my_profile", my_profile))
    app.add_handler(CommandHandler("player_form", player_form))

    # --- 4. Команды для администраторов (только в ЛС) ---
    app.add_handler(CommandHandler("list_planned", list_planned))
    app.add_handler(CommandHandler("start_draft", start_draft))
    app.add_handler(CommandHandler("finish_match", finish_match))
    app.add_handler(CommandHandler("done_actions", done_actions))

    # --- 5. Команды для капитанов (только в ЛС) ---
    app.add_handler(CommandHandler("rps", rps_command))
    app.add_handler(CommandHandler("rps_decision", rps_decision))

    # --- 6. Обработчики инлайн-кнопок (CallbackQueryHandlers) ---
    app.add_handler(CallbackQueryHandler(give_pair_callback, pattern=r'^give_pair:'))
    app.add_handler(CallbackQueryHandler(choose_from_pair_callback, pattern=r'^choose_from_pair:'))

    # --- 7. Системные и глобальные события (в самый конец) ---
    # Авторегистрация и сбор участников при голосовании в опросе группы
    app.add_handler(PollAnswerHandler(handle_poll_answer))
    # Сбор статистики матча по сообщениям админа в ЛС
    app.add_handler(
        MessageHandler(filters.TEXT & (~filters.COMMAND) & filters.ChatType.PRIVATE, private_message_dispatcher))

    print("Бот запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()
