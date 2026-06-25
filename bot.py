import logging
import random
from typing import List, Optional
from html import escape

from config import TELEGRAM_TOKEN, ADMIN_IDS, RPS_OPTIONS, TARGET_GROUP_ID
from database import (
    SessionLocal, Player, Match, DraftState, Action, MatchStatus,
    Tournament, TournamentPairing, TournamentResult,
    create_db_tables, find_players, get_player_by_nickname, get_player_by_tgid,
    get_top_scorers_or_assisters, get_top_most_frequent_players, get_top_winners
)

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, PollAnswerHandler, ContextTypes, filters,
    ConversationHandler
)
from sqlalchemy.orm import Session

# --- Настройка логирования ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Глобальные константы стадий ---
ALLOWED_STAGES = ["1/128", "1/64", "1/32", "1/16", "1/8", "1/4", "1/2", "ФИНАЛ", "МАТЧ_ЗА_3_МЕСТО"]

# --- Состояния для ConversationHandler'ов ---
EDIT_CHOICE, EDIT_NICKNAME, EDIT_FULLNAME = range(3)
PLAN_FIELD, PLAN_DATETIME = range(3, 5)
SELECT_PLAN, SELECT_MATCH_TYPE, SELECT_REMATCH, CHOOSE_CAPTAIN_A, CHOOSE_CAPTAIN_B, CHOOSE_TOURNAMENT_PAIRING, MANUAL_ADD_PLAYERS = range(
    5, 12)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await help_command(update, context)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_type = update.effective_chat.type
    user_id = update.effective_user.id
    is_admin = user_id in ADMIN_IDS

    if chat_type in ['group', 'supergroup']:
        help_text = (
            "🏆 <b>Команды статистики футбольного сообщества (в группе):</b>\n\n"
            "🔹 /top_scorers — Лучшие бомбардиры\n"
            "🔹 /top_assisters — Лучшие ассистенты\n"
            "🔹 /top_frequent — Самые активные игроки\n"
            "🔹 /top_winners — Лидеры по победам\n"
            "🔹 /list_tournaments — Показать все турниры\n"
            "🔹 /view_bracket &lt;ID_турнира&gt; — Посмотреть сетку\n"
            "🔹 /tournament_history — Зал славы (архив победителей)\n\n"
            "💬 <i>Чтобы зарегистрироваться или настроить профиль, перейдите в личные сообщения с ботом.</i>"
        )
    else:
        help_text = (
            "⚽ <b>Добро пожаловать в футбольный бот сообщества «Футболямба»!</b>\n\n"
            "👤 <b>Команды для игроков (в ЛС):</b>\n"
            "🔸 /register &lt;Никнейм&gt; &lt;Имя Фамилия&gt; — Регистрация\n"
            "🔸 /my_profile — Мой профиль и общая сводка\n"
            "🔸 /my_stats — Моя детальная статистика\n"
            "🔸 /edit_profile — Изменить никнейм или ФИО\n"
            "🔸 /player_form &lt;Никнейм&gt; — Форма игрока (последние игры)\n"
            "🔸 /list_tournaments — Показать все турниры\n"
            "🔸 /view_bracket &lt;ID_турнира&gt; — Посмотреть сетку\n"
            "🔸 /tournament_history — Зал славы (архив победителей)\n"
        )
        if is_admin:
            help_text += (
                "\n👑 <b>Панель администратора (в ЛС):</b>\n"
                "🔹 /plan_match — Начать планирование матча (опрос в группу)\n"
                "🔹 /list_planned — Показать все запланированные игры\n"
                "🔹 /create_match — Создать матч на основе выбранного плана\n"
                "🔹 /list_active_matches — Показать активные матчи\n"
                "🔹 /start_draft &lt;ID_матча&gt; — Запустить драфт\n"
                "🔹 /finish_match &lt;ID_матча&gt; &lt;СчетА-СчетБ&gt; — Завершить матч\n"
                "🔹 /done_actions — Завершить ввод голевых действий\n"
                "🔹 /cancel_match &lt;ID_матча&gt; — Отменить зависший матч\n"
                "🔹 /create_tournament &lt;Имя&gt; &lt;BO1/BO2&gt; &lt;Кол-во_игроков&gt; — Создать турнир\n"
                "🔹 /add_pairing &lt;ID&gt; &lt;Слот_ID&gt; &lt;Стадия&gt; &lt;КэпА&gt; &lt;КэпБ&gt; [счет] — Изменить ячейку сетки\n"
                "🔹 /close_tournament &lt;ID&gt; — Архивировать турнир\n"
                "🔹 /add_player &lt;Никнейм&gt; &lt;Имя Фамилия&gt; — Зарегистрировать игрока без Telegram\n"
                "🔹 /list_players — Список всех зарегистрированных игроков\n"
                "🔹 /search_player &lt;Никнейм&gt; — Быстрый поиск игрока\n"
            )
    await update.message.reply_text(help_text, parse_mode='HTML')


# --- Математические функции Bracket Flow (Динамическая сетка любого размера) ---

def get_nearest_power_of_2(val: int) -> int:
    """Вычисляет ближайшую степень двойки, округляя вверх."""
    if val <= 2:
        return 2
    power = 1
    while power < val:
        power *= 2
    return power


def get_next_slot(slot_id: int, N: int) -> Optional[tuple[int, str]]:
    """Вычисляет ID следующего слота по формуле Single Elimination для сетки на N игроков."""
    rounds = []
    offset = 0
    k = N // 2
    while k >= 1:
        rounds.append({"offset": offset, "count": k})
        offset += k
        k = k // 2

    for r_idx, r in enumerate(rounds):
        start = r["offset"] + 1
        end = r["offset"] + r["count"]
        if start <= slot_id <= end:
            relative_id = slot_id - r["offset"]
            if r_idx == len(rounds) - 1:
                return None

            next_round = rounds[r_idx + 1]
            if relative_id % 2 != 0:
                next_rel = (relative_id + 1) // 2
                next_slot_id = next_round["offset"] + next_rel
                return next_slot_id, 'a'
            else:
                next_rel = relative_id // 2
                next_slot_id = next_round["offset"] + next_rel
                return next_slot_id, 'b'
    return None


def get_pairing_winner(pairing: TournamentPairing, session: Session) -> Optional[int]:
    """Вычисляет победителя в паре по сумме всех голов (ручных и сыгранных в боте)."""
    if pairing.manual_score_text:
        try:
            parts = pairing.manual_score_text.replace(' ', '').split(',')
            tot_a, tot_b = 0, 0
            for part in parts:
                sa, sb = map(int, part.split('-'))
                tot_a += sa
                tot_b += sb
            if tot_a > tot_b:
                return pairing.captain_a_id
            elif tot_b > tot_a:
                return pairing.captain_b_id
        except Exception:
            pass
    else:
        tot_a, tot_b = 0, 0
        for m in pairing.matches:
            if m.status == MatchStatus.FINISHED:
                if m.captain_a_id == pairing.captain_a_id:
                    tot_a += m.score_a
                    tot_b += m.score_b
                else:
                    tot_a += m.score_b
                    tot_b += m.score_a
        if tot_a > tot_b:
            return pairing.captain_a_id
        elif tot_b > tot_a:
            return pairing.captain_b_id
    return None


def trigger_advancement(session: Session, pairing: TournamentPairing):
    """Автоматически двигает победителя в финал, а проигравших в полуфинале — в матч за 3-е место."""
    total_pairings = session.query(TournamentPairing).filter_by(tournament_id=pairing.tournament_id).count()
    # Так как мы добавили 1 экстра-слот под матч за 3-е место, реальное N игроков вычисляется как:
    N = total_pairings

    flow = get_next_slot(pairing.slot_id, N)
    winner_tgid = get_pairing_winner(pairing, session)

    if winner_tgid:
        if flow:
            next_slot_id, next_side = flow
            next_pairing = session.query(TournamentPairing).filter_by(
                tournament_id=pairing.tournament_id,
                slot_id=next_slot_id
            ).first()
            if next_pairing:
                if next_side == 'a':
                    next_pairing.captain_a_id = winner_tgid
                else:
                    next_pairing.captain_b_id = winner_tgid

            # --- ЛОГИКА МАТЧА ЗА 3-Е МЕСТО ---
            # Если следующий слот — это ФИНАЛ (Слот N-1), значит текущая стадия была ПОЛУФИНАЛОМ.
            # Проигравший в полуфинале отправляется в матч за 3-е место (Слот N)!
            if next_slot_id == N - 1:
                loser_tgid = pairing.captain_b_id if winner_tgid == pairing.captain_a_id else pairing.captain_a_id
                third_place_pairing = session.query(TournamentPairing).filter_by(
                    tournament_id=pairing.tournament_id,
                    slot_id=N
                ).first()
                if third_place_pairing:
                    if next_side == 'a':
                        third_place_pairing.captain_a_id = loser_tgid
                    else:
                        third_place_pairing.captain_b_id = loser_tgid
                    session.commit()


def get_captain_name_or_placeholder(session: Session, slot_id: int, tournament_id: int, side: str) -> str:
    p = session.query(TournamentPairing).filter_by(tournament_id=tournament_id, slot_id=slot_id).first()
    if p:
        tg_id = p.captain_a_id if side == 'a' else p.captain_b_id
        if tg_id:
            player = get_player_by_tgid(session, tg_id)
            return escape(player.nickname) if player else f"ID {tg_id}"

    total_pairings = session.query(TournamentPairing).filter_by(tournament_id=tournament_id).count()
    N = total_pairings

    if slot_id == N:
        parent_slot = N - 3 if side == 'a' else N - 2
        return f"Проигравший Слот {parent_slot}"

    rounds = []
    offset = 0
    k = N // 2
    while k >= 1:
        rounds.append({"offset": offset, "count": k})
        offset += k
        k = k // 2

    for r_idx, r in enumerate(rounds):
        start = r["offset"] + 1
        end = r["offset"] + r["count"]
        if start <= slot_id <= end:
            if r_idx == 0:
                return "Ожидает"

            prev_round = rounds[r_idx - 1]
            relative_id = slot_id - r["offset"]
            parent_rel = (relative_id * 2) - 1 if side == 'a' else relative_id * 2
            parent_slot_id = prev_round["offset"] + parent_rel
            return f"Победитель Слот {parent_slot_id}"
    return "Ожидает"


# --- Вспомогательные функции (Helpers) ---

def rps_winner(choice_a: str, choice_b: str) -> Optional[str]:
    if choice_a == choice_b:
        return None
    if (choice_a == 'камень' and choice_b == 'ножницы') or \
            (choice_a == 'ножницы' and choice_b == 'бумага') or \
            (choice_a == 'бумага' and choice_b == 'камень'):
        return 'a'
    return 'b'


def player_list_to_display(session: Session, ids: List[int]) -> str:
    if not ids:
        return '(пусто)'
    players = session.query(Player).filter(Player.id.in_(ids)).all()
    players.sort(key=lambda p: p.full_name)
    return '\n'.join(f"- {escape(p.full_name)}" for p in players)


def generate_admin_rosters_helper(session: Session, match: Match) -> str:
    """Генерирует админу в ЛС удобные списки составов для быстрого копирования ников."""
    ds = match.draft
    if not ds:
        return ""
    team_a_ids = ds.get_team_list('a')
    team_b_ids = ds.get_team_list('b')

    color_a = ds.team_a_color if ds.team_a_color else "🔴"
    color_b = ds.team_b_color if ds.team_b_color else "🔵"

    text = f"📋 <b>Составы матча №{match.id} для быстрого копирования никнеймов игроков:</b>\n\n"

    text += f"{color_a} <b>КОМАНДА А:</b>\n"
    for pid in team_a_ids:
        p = session.get(Player, pid)
        if p:
            text += f"• <code>{escape(p.nickname)}</code> — {escape(p.full_name)} (@{escape(p.tg_username) or 'нет'})\n"

    text += f"\n{color_b} <b>КОМАНДА Б:</b>\n"
    for p_id in team_b_ids:
        p = session.get(Player, p_id)
        if p:
            text += f"• <code>{escape(p.nickname)}</code> — {escape(p.full_name)} (@{escape(p.tg_username) or 'нет'})\n"

    text += (
        f"\n💡 <b>Как вносить результативные действия (пакетом, одним сообщением):</b>\n"
        f"Вы можете записать все действия игрока за матч в одном сообщении через запятую.\n\n"
        f"<i>Формат:</i> <code>Никнейм: Х голов, Y ассистов</code>\n\n"
        f"<i>Примеры ввода:</i>\n"
        f"• <code>Goga270: 5 голов, 3 ассиста</code>\n"
        f"• <code>Denis_Liver: гол, пас</code>\n"
        f"• <code>Sanya_Smirnov: 3 гола</code>\n\n"
        f"Для завершения ввода обязательно отправьте команду:\n"
        f"<code>/done_actions</code>"
    )
    return text


def ensure_no_active_match(session: Session):
    active = session.query(Match).filter(
        Match.status.in_([MatchStatus.CREATED, MatchStatus.DRAFTING, MatchStatus.ACTIVE])).first()
    if active:
        raise ValueError(
            f"Нельзя создать новый матч, пока активен матч №{active.id}.\n"
            f"Чтобы закрыть его, администратор должен использовать команду:\n"
            f"`/cancel_match {active.id}`"
        )


def get_player_stats(session: Session, player: Player):
    stats = {"matches": 0, "wins": 0, "losses": 0, "goals": 0, "assists": 0, "win_streak": 0}

    stats['goals'] = session.query(Action).filter_by(player_id=player.id, action_type='goal').count()
    stats['assists'] = session.query(Action).filter_by(player_id=player.id, action_type='assist').count()

    player_matches = session.query(Match).filter(
        Match.players.contains(player),
        Match.status == MatchStatus.FINISHED
    ).order_by(Match.created_at.desc()).all()

    stats['matches'] = len(player_matches)
    current_streak, streak_broken = 0, False

    for match in player_matches:
        team_a_ids = match.draft.get_team_list('a')
        is_in_team_a = player.id in team_a_ids

        won = (is_in_team_a and match.score_a > match.score_b) or \
              (not is_in_team_a and match.score_b > match.score_a)

        lost = (is_in_team_a and match.score_a < match.score_b) or \
               (not is_in_team_a and match.score_b < match.score_a)

        if won:
            stats['wins'] += 1
            if not streak_broken:
                current_streak += 1
        elif lost:
            stats['losses'] += 1
            streak_broken = True

    stats['win_streak'] = current_streak
    return stats


# --- Обработчики команд ---

async def get_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text(f"ID этого чата: `{chat_id}`", parse_mode='MarkdownV2')


async def register_player(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != 'private':
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "⚠️ <b>Неверное использование команды.</b>\n\n"
            "Используйте формат:\n<code>/register &lt;Никнейм&gt; &lt;Имя Фамилия&gt;</code>\n\n"
            "Пример:\n<code>/register JohnDoe Иван Иванов</code>",
            parse_mode='HTML'
        )
        return
    nickname, full_name = args[0], " ".join(args[1:])
    tg_id = update.effective_user.id
    if len(nickname.split()) > 1:
        await update.message.reply_text("❌ Ошибка: Никнейм должен быть одним словом без пробелов.")
        return
    with SessionLocal() as session:
        if get_player_by_tgid(session, tg_id):
            await update.message.reply_text("Вы уже зарегистрированы в системе.")
            return
        if get_player_by_nickname(session, nickname):
            await update.message.reply_text(f"Никнейм '{nickname}' уже занят. Выберите другой.")
            return
        player = Player(tg_id=tg_id, nickname=nickname, full_name=full_name, tg_username=update.effective_user.username)
        session.add(player)
        session.commit()
        await update.message.reply_text(
            f"✅ <b>Регистрация успешно пройдена!</b>\n\n"
            f"• Никнейм: <code>{nickname}</code>\n"
            f"• Отображаемое имя: <b>{full_name}</b>",
            parse_mode='HTML'
        )


async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer, user = update.poll_answer, update.poll_answer.user
    if answer.option_ids and answer.option_ids[0] == 0:
        with SessionLocal() as session:
            if not get_player_by_tgid(session, user.id):
                nickname = user.username if user.username else f"user{user.id}"
                if get_player_by_nickname(session, nickname):
                    nickname = f"user{user.id}"
                new_player = Player(tg_id=user.id, tg_username=user.username, full_name=user.full_name,
                                    nickname=nickname)
                session.add(new_player)
                session.commit()
                logger.info(f"Автоматическая регистрация: {new_player.full_name} ({nickname})")
        if planned_match := next(
                (m for m in context.bot_data.get("planned_matches", []) if m["poll_id"] == answer.poll_id), None):
            planned_match["players"].add(user.id)
    else:
        if planned_match := next(
                (m for m in context.bot_data.get("planned_matches", []) if m["poll_id"] == answer.poll_id), None):
            planned_match["players"].discard(user.id)


# --- Логика поиска игроков ---

async def start_player_search(update: Update, context: ContextTypes.DEFAULT_TYPE, next_state_key: str, prompt: str):
    context.user_data['search_next_state'] = next_state_key
    if update.callback_query:
        await update.callback_query.edit_message_text(prompt, parse_mode='HTML')
    else:
        await update.message.reply_text(prompt, parse_mode='HTML')


async def handle_player_search_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text
    with SessionLocal() as session:
        players = find_players(session, query)

    next_state_key = context.user_data.get('search_next_state')
    current_state = CHOOSE_CAPTAIN_A if next_state_key == 'cap_a_nickname' else CHOOSE_CAPTAIN_B

    if not players:
        await update.message.reply_text("⚠️ Игроки не найдены в базе. Попробуйте другой запрос или /cancel.")
        return current_state

    if len(players) == 1:
        player = players[0]
        context.user_data[next_state_key] = player.nickname
        await update.message.reply_text(f"Найден игрок: <b>{player.full_name}</b> ({player.nickname}). Используем его.",
                                        parse_mode='HTML')
        del context.user_data['search_next_state']
        return await context.user_data['next_function'](update, context)

    buttons = [
        [InlineKeyboardButton(f"{p.full_name} (@{p.tg_username or 'нет'})", callback_data=f"select_player:{p.id}")]
        for p in players
    ]
    await update.message.reply_text(
        "Найдено несколько совпадений. Выберите нужного капитана:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return current_state


async def handle_player_selection_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    player_id = int(query.data.split(':')[1])

    with SessionLocal() as session:
        player = session.get(Player, player_id)
    if not player:
        return await query.edit_message_text("Ошибка: игрок не найден.")

    next_state_key = context.user_data['search_next_state']
    context.user_data[next_state_key] = player.nickname
    await query.edit_message_text(f"Вы выбрали капитаном: <b>{player.full_name}</b>.", parse_mode='HTML')
    del context.user_data['search_next_state']

    return await context.user_data['next_function'](update, context)


async def plan_match(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return ConversationHandler.END

    context.user_data['plan_info'] = {}
    await update.message.reply_text(
        "📅 <b>Планирование матча.</b>\n\n"
        "Шаг 1 из 2: Введите место проведения матча (например, Фили или Лужники №6):",
        parse_mode='HTML'
    )
    return PLAN_FIELD


async def plan_match_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['plan_info']['field'] = update.message.text.strip()
    await update.message.reply_text(
        "Шаг 2 из 2: Введите дату и время матча по МСК (например, 25 мая в 19:30):",
        parse_mode='HTML'
    )
    return PLAN_DATETIME


async def plan_match_datetime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dt_text = update.message.text.strip()
    plan_info = context.user_data.pop('plan_info')
    field = plan_info['field']

    announcement = f"Вы играете {dt_text} на поле {field}. Идёте?"

    try:
        poll_message = await context.bot.send_poll(
            chat_id=TARGET_GROUP_ID,
            question=announcement,
            options=["Иду", "Не иду"],
            is_anonymous=False
        )
        planned = context.bot_data.setdefault("planned_matches", [])
        planned.append({
            "poll_id": poll_message.poll.id,
            "text": announcement,
            "players": set(),
            "field": field,
            "datetime": dt_text
        })
        await update.message.reply_text("✅ Опрос успешно отправлен в группу!")
    except Exception as e:
        logger.error(f"Не удалось отправить опрос: {e}")
        await update.message.reply_text("❌ Не удалось отправить опрос. Проверьте права бота в группе.")
    return ConversationHandler.END


# --- Логика профиля игрока ---

async def my_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with SessionLocal() as session:
        player = get_player_by_tgid(session, update.effective_user.id)
        if not player:
            return await update.message.reply_text("Профиль не найден. Зарегистрируйтесь: /register.")
        stats = get_player_stats(session, player)
        win_rate = (stats['wins'] / stats['matches'] * 100) if stats['matches'] > 0 else 0
        profile_text = (
            f"👤 <b>Профиль игрока: {player.full_name}</b>\n"
            f"   - Никнейм: <code>{player.nickname}</code>\n"
            f"   - Telegram: @{player.tg_username or 'скрыт'}\n\n"
            f"📊 <b>Статистика:</b>\n"
            f"   - Матчей: <b>{stats['matches']}</b> | Победы/Поражения: <b>{stats['wins']}/{stats['losses']}</b> ({win_rate:.1f}%)\n"
            f"   - Голы: <b>{stats['goals']}</b> ⚽ | Ассисты: <b>{stats['assists']}</b> 🎯\n"
            f"   - Победный стрик: <b>{stats['win_streak']}</b> 🔥\n\n"
            f"Для изменения данных отправьте: <code>/edit_profile</code>"
        )
        await update.message.reply_text(profile_text, parse_mode='HTML')


async def edit_profile_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != 'private':
        await update.message.reply_text("⚠️ Редактировать профиль можно только в личных сообщениях с ботом.")
        return ConversationHandler.END

    with SessionLocal() as session:
        player = get_player_by_tgid(session, update.effective_user.id)
        if not player:
            await update.message.reply_text("❌ Профиль не найден. Зарегистрируйтесь: /register.")
            return ConversationHandler.END
        player.tg_username = update.effective_user.username
        session.commit()

    buttons = [[InlineKeyboardButton("✍️ Изменить Никнейм", callback_data=str(EDIT_NICKNAME))],
               [InlineKeyboardButton("👤 Изменить ФИО", callback_data=str(EDIT_FULLNAME))]]
    await update.message.reply_text("🛠 Что вы хотите изменить в своем профиле?",
                                    reply_markup=InlineKeyboardMarkup(buttons))
    return EDIT_CHOICE


async def edit_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = int(update.callback_query.data)
    await update.callback_query.answer()
    if choice == EDIT_NICKNAME:
        await update.callback_query.edit_message_text("Введите новый никнейм (одно слово, латиница):")
        return EDIT_NICKNAME
    elif choice == EDIT_FULLNAME:
        await update.callback_query.edit_message_text("Введите новое ФИО:")
        return EDIT_FULLNAME


async def edit_nickname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_nickname = update.message.text
    if len(new_nickname.split()) > 1:
        await update.message.reply_text("Никнейм должен быть одним словом. Попробуйте еще раз или /cancel.")
        return EDIT_NICKNAME
    with SessionLocal() as session:
        if get_player_by_nickname(session, new_nickname):
            await update.message.reply_text("Этот никнейм уже занят. Попробуйте другой или /cancel.")
            return EDIT_NICKNAME
        player = get_player_by_tgid(session, update.effective_user.id)
        player.nickname = new_nickname
        player.tg_username = update.effective_user.username
        session.commit()
    await update.message.reply_text(f"✅ Никнейм изменен на: <code>{new_nickname}</code>", parse_mode='HTML')
    return ConversationHandler.END


async def edit_fullname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with SessionLocal() as session:
        player = get_player_by_tgid(session, update.effective_user.id)
        player.full_name = update.message.text
        player.tg_username = update.effective_user.username
        session.commit()
    await update.message.reply_text(f"✅ ФИО успешно изменено на: <b>{update.message.text}</b>", parse_mode='HTML')
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Действие отменено.")
    context.user_data.clear()
    return ConversationHandler.END


async def list_planned(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS or update.effective_chat.type != 'private':
        return

    planned = context.bot_data.get("planned_matches", [])
    if not planned:
        return await update.message.reply_text("Нет запланированных матчей.")

    msg = "📋 <b>Запланированные матчи:</b>\n\n"
    for i, m in enumerate(planned, 1):
        msg += f"<b>{i}. {m['field']} ({m['datetime']})</b> — Участников: <b>{len(m['players'])}</b>\n\n"
    msg += "Чтобы создать матч, используйте команду:\n<code>/create_match</code>"
    await update.message.reply_text(msg, parse_mode='HTML')


# --- Логика создания матча ---

async def create_match_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS or update.effective_chat.type != 'private':
        return ConversationHandler.END

    planned = context.bot_data.get("planned_matches", [])
    if not planned:
        await update.message.reply_text("Нет запланированных матчей для создания. Сначала используйте `/plan_match`.")
        return ConversationHandler.END

    context.user_data['available_plans'] = planned

    buttons = []
    for i, m in enumerate(planned):
        buttons.append([InlineKeyboardButton(f"{m['field']} ({m['datetime']}) — {len(m['players'])} чел.",
                                             callback_data=f"plan:{i}")])

    await update.message.reply_text(
        "Выберите запланированный матч из опросов:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return SELECT_PLAN


async def create_match_select_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    plan_index = int(query.data.split(':')[1])
    selected_plan = context.user_data['available_plans'][plan_index]

    context.user_data['creation_data'] = {
        "plan": selected_plan,
        "is_rematch": False,
        "is_tournament": False,
        "pairing_id": None
    }

    buttons = [
        [
            InlineKeyboardButton("🏆 Турнирная игра", callback_data="match_type:tournament"),
            InlineKeyboardButton("🤝 Товарищеская игра", callback_data="match_type:friendly")
        ]
    ]

    await query.edit_message_text(
        f"Вы выбрали план: <b>{selected_plan['field']} ({selected_plan['datetime']})</b>.\n\n"
        f"Укажите тип матча:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode='HTML'
    )
    return SELECT_MATCH_TYPE


async def create_match_select_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    match_type = query.data.split(':')[1]
    creation_data = context.user_data['creation_data']
    plan = creation_data['plan']

    if match_type == 'tournament':
        creation_data['is_tournament'] = True

        with SessionLocal() as session:
            active_tournaments = session.query(Tournament).filter_by(is_active=True).all()
            available_pairings = []

            voter_tgids = plan['players']  # ID участников

            for t in active_tournaments:
                pairings = session.query(TournamentPairing).filter_by(tournament_id=t.id, is_completed=False).all()
                for pairing in pairings:
                    if pairing.captain_a_id in voter_tgids and pairing.captain_b_id in voter_tgids:
                        cap_a = get_player_by_tgid(session, pairing.captain_a_id)
                        cap_b = get_player_by_tgid(session, pairing.captain_b_id)
                        if cap_a and cap_b:
                            finished_matches = session.query(Match).filter_by(tournament_pairing_id=pairing.id,
                                                                              status=MatchStatus.FINISHED).all()
                            if t.match_format == "BO2":
                                if len(finished_matches) == 1:
                                    prev = finished_matches[0]
                                    label = f"Игра 2 (предыдущий счет: {prev.score_a}-{prev.score_b})"
                                elif pairing.manual_score_text and not pairing.is_completed:
                                    label = f"Игра 2 (прошлый счет: {pairing.manual_score_text})"
                                else:
                                    label = "Игра 1"
                            else:
                                label = "BO1"

                            available_pairings.append(
                                (pairing.id, t.name, pairing.stage, cap_a.nickname, cap_b.nickname, label))

            if not available_pairings:
                await query.edit_message_text(
                    "❌ Среди проголосовавших участников нет капитанов с несыгранной турнирной игрой.\n\n"
                    "Пожалуйста, выберите Товарищескую игру."
                )
                return ConversationHandler.END

            buttons = []
            for pid, t_name, stage, nick_a, nick_b, label in available_pairings:
                buttons.append([InlineKeyboardButton(
                    f"🏆 {t_name} | {stage} | {nick_a} vs {nick_b} ({label})",
                    callback_data=f"select_tour_pairing:{pid}"
                )])

            await query.edit_message_text(
                "Выберите доступную турнирную пару (на основе проголосовавших):",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
            return CHOOSE_TOURNAMENT_PAIRING

    else:
        creation_data['is_tournament'] = False

        # Интерактивный поиск Капитана А
        context.user_data['next_function'] = create_match_found_cap_a
        await start_player_search(
            update, context, "cap_a_nickname",
            "🔍 <b>Поиск Капитана А.</b>\nВведите поисковый запрос (ФИО, никнейм или @username):"
        )
        return CHOOSE_CAPTAIN_A


async def create_match_found_cap_a(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cap_a_nick = context.user_data.pop('cap_a_nickname')
    with SessionLocal() as session:
        cap_a = get_player_by_nickname(session, cap_a_nick)
        context.user_data['creation_data']['cap_a_id'] = cap_a.id

    context.user_data['next_function'] = create_match_found_cap_b
    await start_player_search(
        update, context, "cap_b_nickname",
        f"Капитан А: <b>{cap_a.full_name}</b>.\n\n"
        f"🔍 <b>Теперь введите запрос для поиска Капитана Б</b> (ФИО, никнейм или @username):"
    )
    return CHOOSE_CAPTAIN_B


async def create_match_found_cap_b(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cap_b_nick = context.user_data.pop('cap_b_nickname')
    with SessionLocal() as session:
        cap_b = get_player_by_nickname(session, cap_b_nick)
        context.user_data['creation_data']['cap_b_id'] = cap_b.id
        cap_a = session.get(Player, context.user_data['creation_data']['cap_a_id'])

    buttons = [
        [
            InlineKeyboardButton("🔄 Да, реванш", callback_data="friendly_rematch:true"),
            InlineKeyboardButton("🟢 Нет, обычный", callback_data="friendly_rematch:false")
        ]
    ]

    text = (
        f"Капитаны успешно выбраны!\n"
        f"🔴 Капитан А: <b>{cap_a.full_name}</b>\n"
        f"🔵 Капитан Б: <b>{cap_b.full_name}</b>\n\n"
        f"Этот товарищеский матч является реваншем (без игры в КНБ)?"
    )

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons),
                                                      parse_mode='HTML')
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode='HTML')
    return SELECT_REMATCH


async def choose_friendly_rematch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    is_rematch = query.data.split(':')[1] == 'true'
    context.user_data['creation_data']['is_rematch'] = is_rematch

    await query.edit_message_text(
        "Пришлите никнеймы дополнительных игроков через пробел.\n"
        "Если добавлять никого не нужно, отправьте команду `/skip`.",
        parse_mode='HTML'
    )
    return MANUAL_ADD_PLAYERS


async def choose_tournament_pairing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    pairing_id = int(query.data.split(':')[1])
    creation_data = context.user_data['creation_data']
    creation_data['pairing_id'] = pairing_id

    with SessionLocal() as session:
        pairing = session.get(TournamentPairing, pairing_id)
        t = pairing.tournament

        cap_a = get_player_by_tgid(session, pairing.captain_a_id)
        cap_b = get_player_by_tgid(session, pairing.captain_b_id)

        creation_data['cap_a_id'] = cap_a.id
        creation_data['cap_b_id'] = cap_b.id

        finished_matches = session.query(Match).filter_by(tournament_pairing_id=pairing.id,
                                                          status=MatchStatus.FINISHED).all()

        is_leg2 = False
        prev_score_str = ""
        if t.match_format == "BO2":
            if len(finished_matches) == 1:
                is_leg2 = True
                prev = finished_matches[0]
                prev_score_str = f"прошлый счет {prev.score_a}-{prev.score_b}"
            elif pairing.manual_score_text and not pairing.is_completed:
                is_leg2 = True
                prev_score_str = f"прошлый счет {pairing.manual_score_text}"

        if is_leg2:
            creation_data['is_rematch'] = True
            type_text = f"Турнир {t.name} (Игра 2, авто-реванш, {prev_score_str})"
        else:
            creation_data['is_rematch'] = False
            type_text = f"Турнир {t.name} ({pairing.stage})"

    await query.edit_message_text(
        f"Вы выбрали турнирную пару: <b>{cap_a.full_name} vs {cap_b.full_name}</b> ({type_text}).\n\n"
        f"Пришлите никнеймы дополнительных игроков через пробел.\n"
        f"Если добавлять никого не нужно, отправьте команду `/skip`.",
        parse_mode='HTML'
    )
    return MANUAL_ADD_PLAYERS


async def finalize_match_creation(update: Update, context: ContextTypes.DEFAULT_TYPE, manual_nicknames: list[str]):
    creation_data = context.user_data.get('creation_data')

    message_target = update.message or (update.callback_query.message if update.callback_query else None)
    if not message_target:
        logger.error("Не удалось найти объект сообщения.")
        return

    if not creation_data:
        await message_target.reply_text("Ошибка: сессия создания матча истекла. Начните заново.")
        return

    plan = creation_data['plan']
    is_rematch = creation_data.get('is_rematch', False)
    is_tournament = creation_data.get('is_tournament', False)
    pairing_id = creation_data.get('pairing_id')
    cap_a_db_id = creation_data.get('cap_a_id')
    cap_b_db_id = creation_data.get('cap_b_id')

    with SessionLocal() as session:
        try:
            ensure_no_active_match(session)

            cap_a = session.get(Player, cap_a_db_id)
            cap_b = session.get(Player, cap_b_db_id)

            if not (cap_a and cap_b and cap_a.tg_id and cap_b.tg_id):
                await message_target.reply_text("❌ Ошибка: Капитаны не найдены в базе данных.")
                return

            all_players = {cap_a, cap_b}
            for tg_id in plan['players']:
                if p := get_player_by_tgid(session, tg_id):
                    all_players.add(p)
            for nickname in manual_nicknames:
                if p := get_player_by_nickname(session, nickname):
                    all_players.add(p)
                else:
                    await message_target.reply_text(
                        f"⚠️ Игрок с никнеймом '{nickname}' не найден. Попросите его зарегистрироваться."
                    )
                    return

            player_pool = list(all_players)
            if len(player_pool) % 2 != 0:
                await message_target.reply_text(
                    f"❌ Ошибка: Нечетное количество игроков ({len(player_pool)})."
                )
                return

            match = Match(
                admin_id=update.effective_user.id,
                captain_a_id=cap_a.tg_id,
                captain_b_id=cap_b.tg_id,
                is_return_match=is_rematch,
                players=player_pool,
                tournament_pairing_id=pairing_id if is_tournament else None
            )
            session.add(match)
            session.flush()

            draft_pool_ids = [p.id for p in player_pool if p.id not in (cap_a.id, cap_b.id)]
            ds = DraftState(match_id=match.id)
            ds.set_pool_list(draft_pool_ids)
            ds.set_team_list('a', [cap_a.id])
            ds.set_team_list('b', [cap_b.id])
            session.add(ds)

            planned_matches = context.bot_data.get("planned_matches", [])
            if plan in planned_matches:
                planned_matches.remove(plan)

            session.commit()

            await message_target.reply_text(
                f"✅ <b>Матч №{match.id} успешно создан!</b>\n\n"
                f"Запустите драфт с помощью команды:\n"
                f"<code>/start_draft {match.id}</code>",
                parse_mode='HTML'
            )

            if TARGET_GROUP_ID:
                tour_info = ""
                rematch_info = ""
                if is_tournament and pairing_id:
                    pairing = session.get(TournamentPairing, pairing_id)
                    tour_info = f"\n🏆 <b>Турнир:</b> {pairing.tournament.name} ({pairing.stage})"

                    # Получаем информацию о прошлых играх в BO2
                    finished_matches = session.query(Match).filter_by(tournament_pairing_id=pairing.id,
                                                                      status=MatchStatus.FINISHED).all()
                    if pairing.tournament.match_format == "BO2":
                        if len(finished_matches) == 1:
                            prev = finished_matches[0]
                            rematch_info = f"\n🔄 <b>Ответный матч</b> (прошлый счет: {prev.score_a}-{prev.score_b})"
                        elif pairing.manual_score_text and not pairing.is_completed:
                            rematch_info = f"\n🔄 <b>Ответный матч</b> (прошлый счет: {pairing.manual_score_text})"

                # Единый красивый анонс без вывода команды /start_draft
                await context.bot.send_message(
                    chat_id=TARGET_GROUP_ID,
                    text=f"🔥 <b>Матч №{match.id} запланирован!</b>{tour_info}{rematch_info}\n\n"
                         f"👥 <b>Капитаны:</b>\n"
                         f"🔴 <b>Команда А:</b> {cap_a.full_name}\n"
                         f"🔵 <b>Команда Б:</b> {cap_b.full_name}\n\n"
                         f"Ожидается старт драфта администратором.",
                    parse_mode='HTML'
                )
        except ValueError as e:
            await message_target.reply_text(str(e))
        except Exception as e:
            logger.error(f"Ошибка при финализации: {e}")
            await message_target.reply_text("Произошла непредвиденная ошибка при сохранении.")
        finally:
            context.user_data.pop('creation_data', None)


async def finalize_match_creation_wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await finalize_match_creation(update, context, update.message.text.split())
    return ConversationHandler.END


async def create_match_skip_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await finalize_match_creation(update, context, [])
    return ConversationHandler.END


# --- Драфт ---

async def start_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS or update.effective_chat.type != 'private':
        return
    if not context.args:
        return await update.message.reply_text("Использование: <code>/start_draft &lt;ID_матча&gt;</code>",
                                               parse_mode='HTML')

    try:
        match_id = int(context.args[0])
    except ValueError:
        return await update.message.reply_text("ID матча должен быть числом.")

    with SessionLocal() as session:
        match = session.get(Match, match_id)
        if not match:
            return await update.message.reply_text("Матч не найден.")
        if match.status != MatchStatus.CREATED:
            return await update.message.reply_text("Матч уже в процессе драфта, активен или завершён.")

        match.status = MatchStatus.DRAFTING
        ds = match.draft
        if not ds:
            return await update.message.reply_text("Критическая ошибка: отсутствует состояние драфта.")

        if match.is_return_match:
            prev = session.query(Match).filter(
                ((Match.captain_a_id == match.captain_a_id) & (Match.captain_b_id == match.captain_b_id)) |
                ((Match.captain_a_id == match.captain_b_id) & (Match.captain_b_id == match.captain_a_id))
            ).filter(Match.id < match.id).order_by(Match.created_at.desc()).first()

            if prev and prev.draft and prev.draft.rps_winner:
                prev_winner_side = prev.draft.rps_winner
                prev_winner_tgid = prev.captain_a_id if prev_winner_side == 'a' else prev.captain_b_id
                new_winner_tgid = match.captain_b_id if prev_winner_tgid == match.captain_a_id else match.captain_a_id
                ds.rps_winner = 'a' if new_winner_tgid == match.captain_a_id else 'b'

                winner_side = ds.rps_winner.upper()
                msg = (
                    f"📋 <b>Драфт начат (ответный матч)!</b>\n\n"
                    f"Победитель прошлого преддрафта уступает право выбора.\n"
                    f"Автоматический победитель: <b>Капитан {winner_side}</b>."
                )

                await context.bot.send_message(chat_id=match.captain_a_id, text=msg, parse_mode='HTML')
                await context.bot.send_message(chat_id=match.captain_b_id, text=msg, parse_mode='HTML')

                win_chat = match.captain_a_id if ds.rps_winner == 'a' else match.captain_b_id
                await context.bot.send_message(
                    chat_id=win_chat,
                    text=f"👑 <b>Вы победили!</b> Выберите стратегию драфта в ЛС:\n"
                         f"• Отдать первую пару: <code>/rps_decision {match.id} give</code>\n"
                         f"• Выбирать первым: <code>/rps_decision {match.id} choose</code>",
                    parse_mode='HTML'
                )
                session.commit()
                return
            else:
                await update.message.reply_text(
                    "⚠️ Предыдущий матч для этих капитанов не найден в базе. Стандартная игра в КНБ.")

        session.commit()

        try:
            msg = (
                f"🎲 <b>Матч №{match.id}</b>: сыграйте в «Камень, Ножницы, Бумага»!\n\n"
                f"Отправьте боту в ЛС одну из команд:\n"
                f"• <code>/rps {match.id} камень</code>\n"
                f"• <code>/rps {match.id} ножницы</code>\n"
                f"• <code>/rps {match.id} бумага</code>"
            )
            await context.bot.send_message(chat_id=match.captain_a_id, text=msg, parse_mode='HTML')
            await context.bot.send_message(chat_id=match.captain_b_id, text=msg, parse_mode='HTML')
            await update.message.reply_text(f"✅ Приглашения сыграть в КНБ успешно отправлены капитанам.")
        except Exception as e:
            await update.message.reply_text(f"Не удалось отправить сообщение капитанам: {e}")


async def rps_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(Капитан в ЛС) /rps <match_id> <камень|ножницы|бумага>"""
    if len(context.args) < 2:
        return await update.message.reply_text(
            "⚠️ <b>Неверный формат команды.</b>\n\n"
            "Используйте:\n<code>/rps &lt;ID_матча&gt; &lt;камень/ножницы/бумага&gt;</code>\n\n"
            "Пример:\n<code>/rps 1 камень</code>",
            parse_mode='HTML'
        )

    try:
        match_id = int(context.args[0])
        choice = context.args[1].lower()
    except ValueError:
        return await update.message.reply_text("⚠️ ID матча должен быть числом. Пример: `/rps 1 камень`",
                                               parse_mode='HTML')

    if choice not in RPS_OPTIONS:
        return await update.message.reply_text(f"⚠️ Неверный выбор. Варианты: <b>{', '.join(RPS_OPTIONS)}</b>",
                                               parse_mode='HTML')

    with SessionLocal() as session:
        match = session.get(Match, match_id)
        if not match:
            return await update.message.reply_text(f"⚠️ Матч №{match_id} не найден.")
        if not match.draft:
            return await update.message.reply_text(f"⚠️ Драфт для матча №{match_id} не инициализирован.")

        user_id = update.effective_user.id
        if user_id == match.captain_a_id:
            side = 'a'
        elif user_id == match.captain_b_id:
            side = 'b'
        else:
            return await update.message.reply_text("⚠️ Вы не являетесь капитаном этого матча.")

        setattr(match.draft, f'rps_choice_{side}', choice)
        await update.message.reply_text(f"✅ Вы выбрали: <b>{choice}</b>. Ожидаем соперника...", parse_mode='HTML')

        ds = match.draft
        if ds.rps_choice_a and ds.rps_choice_b:
            winner = rps_winner(ds.rps_choice_a, ds.rps_choice_b)

            if winner is None:
                ds.rps_choice_a = None
                ds.rps_choice_b = None
                session.commit()

                retry_msg = (
                    f"🤝 <b>Ничья!</b> Оба выбрали <code>{choice}</code>.\n\n"
                    f"Повторите выбор:\n"
                    f"• <code>/rps {match.id} камень</code>\n"
                    f"• <code>/rps {match.id} ножницы</code>\n"
                    f"• <code>/rps {match.id} бумага</code>"
                )
                await context.bot.send_message(chat_id=match.captain_a_id, text=retry_msg, parse_mode='HTML')
                await context.bot.send_message(chat_id=match.captain_b_id, text=retry_msg, parse_mode='HTML')
            else:
                ds.rps_winner = winner
                winner_side_text = winner.upper()

                winner_tg_id = match.captain_a_id if winner == 'a' else match.captain_b_id
                winner_player = session.query(Player).filter(Player.tg_id == winner_tg_id).first()
                winner_name = winner_player.full_name if winner_player else f"Капитан {winner_side_text}"

                msg = f"🏁 <b>Результат КНБ:</b> победил <b>{winner_name}</b> (Капитан {winner_side_text})!"
                await context.bot.send_message(chat_id=match.captain_a_id, text=msg, parse_mode='HTML')
                await context.bot.send_message(chat_id=match.captain_b_id, text=msg, parse_mode='HTML')

                winner_chat = match.captain_a_id if winner == 'a' else match.captain_b_id
                await context.bot.send_message(
                    chat_id=winner_chat,
                    text=f"👑 <b>Вы победили в КНБ!</b> Выберите стратегию:\n\n"
                         f"🔸 Отдать первую пару: <code>/rps_decision {match.id} give</code> (соперник выбирает первым)\n"
                         f"🔸 Выбирать первым: <code>/rps_decision {match.id} choose</code> (вы выбираете первым)",
                    parse_mode='HTML'
                )
        session.commit()


async def rps_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        return await update.message.reply_text("⚠️ Ошибка. Пример использования: `/rps_decision 1 choose`",
                                               parse_mode='HTML')
    match_id, decision = int(context.args[0]), context.args[1].lower()
    if decision not in ('give', 'choose'):
        return
    with SessionLocal() as session:
        match = session.get(Match, match_id)
        if not (match and match.draft and match.draft.rps_winner):
            return
        expected_winner_tg = match.captain_a_id if match.draft.rps_winner == 'a' else match.captain_b_id
        if update.effective_user.id != expected_winner_tg:
            return
        ds = match.draft
        ds.turn = ds.rps_winner if decision == 'give' else ('a' if ds.rps_winner == 'b' else 'b')
        session.commit()
        await update.message.reply_text(f"Решение принято. Первым дает пару капитан {ds.turn.upper()}.")
        await proceed_to_draft_turn(context, session, match.id)


async def proceed_to_draft_turn(context: ContextTypes.DEFAULT_TYPE, session: Session, match_id: int):
    match = session.get(Match, match_id)
    ds = match.draft
    pool = ds.get_pool_list()

    if not pool:
        # Драфт закончен! Запускаем выбор цветов формы перед сохранением
        loser_side = 'b' if ds.rps_winner == 'a' else 'a'
        loser_tg = match.captain_b_id if ds.rps_winner == 'a' else match.captain_a_id
        winner_tg = match.captain_a_id if ds.rps_winner == 'a' else match.captain_b_id

        buttons = [
            [
                InlineKeyboardButton("🔴 Красный", callback_data=f"color_choice:{match.id}:{loser_side}:red"),
                InlineKeyboardButton("🔵 Синий", callback_data=f"color_choice:{match.id}:{loser_side}:blue")
            ],
            [
                InlineKeyboardButton("🤝 Отдать выбор сопернику",
                                     callback_data=f"color_choice:{match.id}:{loser_side}:yield")
            ]
        ]

        await context.bot.send_message(
            chat_id=loser_tg,
            text="🎨 <b>Драфт успешно завершен!</b>\n\nВы проиграли преддрафтовый выбор КНБ. Пожалуйста, выберите цвет формы вашей команды на игру:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode='HTML'
        )
        await context.bot.send_message(
            chat_id=winner_tg,
            text="⏳ Драфт завершен. Ожидайте, пока ваш соперник выбирает цвет формы..."
        )
        return

    giver_tg, receiver_tg = (match.captain_a_id, match.captain_b_id) if ds.turn == 'a' else (
        match.captain_b_id, match.captain_a_id)

    players = session.query(Player).filter(Player.id.in_(pool)).all()

    buttons = []
    row = []
    for p in players:
        btn = InlineKeyboardButton(p.full_name, callback_data=f"give_step1:{match.id}:{p.id}")
        row.append(btn)
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    kb = InlineKeyboardMarkup(buttons)
    await context.bot.send_message(
        chat_id=giver_tg,
        text=f"📋 Ваша очередь дать пару.\n\nВыберите <b>первого</b> игрока из пула:",
        reply_markup=kb
    )
    await context.bot.send_message(chat_id=receiver_tg, text="⏳ Ожидайте, соперник выбирает пару игроков...")


async def color_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Интерактивный выбор цветов формы после драфта."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split(':')
    match_id = int(parts[1])
    side = parts[2]
    choice = parts[3]

    other_side = 'b' if side == 'a' else 'a'

    with SessionLocal() as session:
        match = session.get(Match, match_id)
        if not (match and match.draft):
            return

        ds = match.draft

        # Проверка авторизации
        expected_tg = match.captain_a_id if side == 'a' else match.captain_b_id
        if update.effective_user.id != expected_tg:
            await query.answer("❌ Вы не можете выбирать цвет за соперника!", show_alert=True)
            return

        if choice == 'red':
            setattr(ds, f'team_{side}_color', "🔴")
            setattr(ds, f'team_{other_side}_color', "🔵")
            await query.edit_message_text("✅ Вы выбрали <b>Красный</b> цвет формы.", parse_mode='HTML')
            await context.bot.send_message(
                chat_id=match.captain_a_id if side == 'b' else match.captain_b_id,
                text="🎨 Соперник выбрал цвет формы. Вашей команде автоматически присвоен <b>Синий</b> цвет.",
                parse_mode='HTML'
            )
            session.commit()
            await publish_draft_results(context, session, match.id)

        elif choice == 'blue':
            setattr(ds, f'team_{side}_color', "🔵")
            setattr(ds, f'team_{other_side}_color', "🔴")
            await query.edit_message_text("✅ Вы выбрали <b>Синий</b> цвет формы.", parse_mode='HTML')
            await context.bot.send_message(
                chat_id=match.captain_a_id if side == 'b' else match.captain_b_id,
                text="🎨 Соперник выбрал цвет формы. Вашей команде автоматически присвоен <b>Красный</b> цвет.",
                parse_mode='HTML'
            )
            session.commit()
            await publish_draft_results(context, session, match.id)

        elif choice == 'yield':
            # Логика передачи выбора сопернику
            loser_side = 'b' if ds.rps_winner == 'a' else 'a'
            if side == loser_side:
                # Loser уступил выбор, теперь спрашиваем Winner
                winner_tg = match.captain_a_id if other_side == 'a' else match.captain_b_id
                await query.edit_message_text("🤝 Вы передали выбор цвета сопернику. Ожидайте...", parse_mode='HTML')

                buttons = [
                    [
                        InlineKeyboardButton("🔴 Красный", callback_data=f"color_choice:{match.id}:{other_side}:red"),
                        InlineKeyboardButton("🔵 Синий", callback_data=f"color_choice:{match.id}:{other_side}:blue")
                    ],
                    [
                        InlineKeyboardButton("🤝 Отдать выбор сопернику",
                                             callback_data=f"color_choice:{match.id}:{other_side}:yield")
                    ]
                ]
                await context.bot.send_message(
                    chat_id=winner_tg,
                    text="🎨 Соперник уступил вам право выбора цвета формы. Пожалуйста, выберите цвет формы:",
                    reply_markup=InlineKeyboardMarkup(buttons),
                    parse_mode='HTML'
                )
            else:
                # Оба капитана уступили право выбора -> рандомизируем цвета
                await query.edit_message_text(
                    "🤝 Вы также передали выбор цвета сопернику. Бот распределит цвета случайно.")

                if random.choice([True, False]):
                    ds.team_a_color = "🔴"
                    ds.team_b_color = "🔵"
                else:
                    ds.team_a_color = "🔵"
                    ds.team_b_color = "🔴"

                session.commit()

                random_msg = f"🎲 Оба капитана отказались от выбора. Цвета распределены случайно:\n\n• Команда А — {ds.team_a_color}\n• Команда Б — {ds.team_b_color}"
                await context.bot.send_message(chat_id=match.captain_a_id, text=random_msg, parse_mode='HTML')
                await context.bot.send_message(chat_id=match.captain_b_id, text=random_msg, parse_mode='HTML')
                await publish_draft_results(context, session, match.id)


async def publish_draft_results(context: ContextTypes.DEFAULT_TYPE, session: Session, match_id: int):
    """Публикует и рассылает результаты драфта после утверждения цветов."""
    match = session.get(Match, match_id)
    ds = match.draft
    match.status = MatchStatus.ACTIVE
    session.commit()

    team_a_ids, team_b_ids = ds.get_team_list('a'), ds.get_team_list('b')
    cap_a, cap_b = session.get(Player, ds.get_team_list('a')[0]), session.get(Player, ds.get_team_list('b')[0])

    color_a = ds.team_a_color if ds.team_a_color else "🔴"
    color_b = ds.team_b_color if ds.team_b_color else "🔵"

    await context.bot.send_message(chat_id=match.captain_a_id,
                                   text=f"✅ <b>Драфт завершен!</b>\n\n<b>Ваша команда ({color_a}):</b>\n{player_list_to_display(session, team_a_ids)}",
                                   parse_mode='HTML')
    await context.bot.send_message(chat_id=match.captain_b_id,
                                   text=f"✅ <b>Драфт завершен!</b>\n\n<b>Ваша команда ({color_b}):</b>\n{player_list_to_display(session, team_b_ids)}",
                                   parse_mode='HTML')

    if TARGET_GROUP_ID:
        tour_info = ""
        if match.tournament_pairing_id:
            pairing = match.pairing
            tour_info = f" ({pairing.tournament.name} | {pairing.stage})"

        await context.bot.send_message(chat_id=TARGET_GROUP_ID,
                                       text=f"✅ <b>Драфт завершен! Составы на матч №{match.id}{tour_info}:</b>\n\n"
                                            f"{color_a} <b>{escape(cap_a.full_name)} (Команда А)</b>\n"
                                            f"{player_list_to_display(session, team_a_ids)}\n\n"
                                            f"{color_b} <b>{escape(cap_b.full_name)} (Команда Б)</b>\n"
                                            f"{player_list_to_display(session, team_b_ids)}",
                                       parse_mode='HTML')


async def give_step1_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(Внутренняя) Шаг 1 драфта: Выбор первого игрока из пары."""
    query = update.callback_query
    await query.answer()

    _, mid_s, p1_s = query.data.split(':')
    match_id, p1_id = int(mid_s), int(p1_s)

    with SessionLocal() as session:
        match = session.get(Match, match_id)
        if not (match and match.draft):
            return await query.edit_message_text("Ошибка: матч не найден.")

        ds = match.draft

        # ЗАЩИТА: Проверка очереди
        expected_giver_tg = match.captain_a_id if ds.turn == 'a' else match.captain_b_id
        if update.effective_user.id != expected_giver_tg:
            await query.answer("❌ Сейчас не ваш ход!", show_alert=True)
            return

        pool = ds.get_pool_list()
        p1_obj = session.get(Player, p1_id)

        remaining_pool = [pid for pid in pool if pid != p1_id]
        players = session.query(Player).filter(Player.id.in_(remaining_pool)).all()

        buttons = []
        row = []
        for p in players:
            btn = InlineKeyboardButton(p.full_name, callback_data=f"give_step2:{match.id}:{p1_id},{p.id}")
            row.append(btn)
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)

        kb = InlineKeyboardMarkup(buttons)
        await query.edit_message_text(
            text=f"👤 Вы выбрали первого игрока: <b>{p1_obj.full_name}</b>.\n\n"
                 f"Теперь выберите <b>второго</b> игрока для пары:",
            reply_markup=kb,
            parse_mode='HTML'
        )


async def give_step2_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(Внутренняя) Шаг 2 драфта: Выбор второго игрока и отправка пары сопернику."""
    query = update.callback_query
    await query.answer()

    _, mid_s, pair_s = query.data.split(':')
    match_id = int(mid_s)
    p1_id, p2_id = [int(x) for x in pair_s.split(',')]

    with SessionLocal() as session:
        match = session.get(Match, match_id)
        if not (match and match.draft):
            return await query.edit_message_text("Ошибка: матч не найден.")

        ds = match.draft

        # ЗАЩИТА: Проверка очереди
        expected_giver_tg = match.captain_a_id if ds.turn == 'a' else match.captain_b_id
        if update.effective_user.id != expected_giver_tg:
            await query.answer("❌ Сейчас не ваш ход!", show_alert=True)
            return

        receiver_tg = match.captain_b_id if ds.turn == 'a' else match.captain_a_id
        p1_obj = session.get(Player, p1_id)
        p2_obj = session.get(Player, p2_id)

        buttons = [
            [InlineKeyboardButton(f"Выбрать {p1_obj.full_name}",
                                  callback_data=f"choose_from_pair:{match.id}:{p1_id},{p2_id}")],
            [InlineKeyboardButton(f"Выбрать {p2_obj.full_name}",
                                  callback_data=f"choose_from_pair:{match.id}:{p2_id},{p1_id}")]
        ]

        await query.edit_message_text(
            text=f"✅ Вы предложили сопернику пару: <b>{p1_obj.full_name}</b> & <b>{p2_obj.full_name}</b>.\n⏳ Ожидайте выбора..."
        )

        await context.bot.send_message(
            chat_id=receiver_tg,
            text=f"⚖️ Вам предложили пару.\n"
                 f"Команда соперника получит того игрока, которого вы <i>НЕ</i> выберете.\n\n"
                 f"Кого забираете к себе?",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode='HTML'
        )


async def choose_from_pair_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(Внутренняя) Обработка выбора игрока из пары."""
    query = update.callback_query
    await query.answer()

    _, mid_s, payload = query.data.split(':')
    match_id, (chosen, other) = int(mid_s), [int(p) for p in payload.split(',')]

    with SessionLocal() as session:
        match = session.get(Match, match_id)
        if not (match and match.draft):
            return

        ds = match.draft

        # ЗАЩИТА: Проверка очереди
        expected_chooser_tg = match.captain_b_id if ds.turn == 'a' else match.captain_a_id
        if update.effective_user.id != expected_chooser_tg:
            await query.answer("❌ Это выбор вашего соперника!", show_alert=True)
            return

        giver_side, chooser_side = ds.turn, ('b' if ds.turn == 'a' else 'a')
        for side, p_id in [(chooser_side, chosen), (giver_side, other)]:
            team_list = ds.get_team_list(side)
            team_list.append(p_id)
            ds.set_team_list(side, team_list)

        ds.set_pool_list([p for p in ds.get_pool_list() if p not in (chosen, other)])
        ds.turn = chooser_side
        session.commit()

        p_ch, p_ot = session.get(Player, chosen), session.get(Player, other)
        await query.edit_message_text(f"Вы выбрали {p_ch.full_name}. Игрок {p_ot.full_name} уходит сопернику.")

        team_a_ids, team_b_ids = ds.get_team_list('a'), ds.get_team_list('b')
        msg = (f"Пул оставшихся:\n{player_list_to_display(session, ds.get_pool_list())}\n\n"
               f"Команда A:\n{player_list_to_display(session, team_a_ids)}\n\n"
               f"Команда B:\n{player_list_to_display(session, team_b_ids)}")

        await context.bot.send_message(chat_id=match.captain_a_id, text=msg)
        await context.bot.send_message(chat_id=match.captain_b_id, text=msg)
        await proceed_to_draft_turn(context, session, match.id)


async def finish_match(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(Админ в ЛС) /finish_match <ID> <scoreA-scoreB> - Завершает матч и отправляет шпаргалку."""
    if update.effective_user.id not in ADMIN_IDS or update.effective_chat.type != 'private':
        return
    if len(context.args) < 2:
        return await update.message.reply_text(
            "Использование: `/finish_match <ID_матча> <счетА-счетБ>`\nПример: `/finish_match 1 3-2`",
            parse_mode='Markdown')

    try:
        match_id = int(context.args[0])
        score_a, score_b = map(int, context.args[1].split('-'))
    except ValueError:
        return await update.message.reply_text("Неверный формат. Пример: `/finish_match 1 5-3`", parse_mode='Markdown')

    with SessionLocal() as session:
        match = session.get(Match, match_id)
        if not match:
            return await update.message.reply_text("Матч с таким ID не найден.")

        match.status = MatchStatus.FINISHED
        match.score_a, match.score_b = score_a, score_b

        if match.tournament_pairing_id:
            pairing = match.pairing
            t = pairing.tournament

            finished_matches = session.query(Match).filter_by(tournament_pairing_id=pairing.id,
                                                              status=MatchStatus.FINISHED).all()
            finished_count = len(finished_matches)

            if t.match_format == "BO1":
                pairing.is_completed = True
            elif t.match_format == "BO2":
                if finished_count >= 2:
                    pairing.is_completed = True

            if pairing.is_completed:
                trigger_advancement(session, pairing)

        session.commit()

        # Сохраняем в сессии и отправляем шпаргалку
        context.user_data['finishing_match'] = match_id
        roster_text = generate_admin_rosters_helper(session, match)

        await update.message.reply_text(
            f"✅ <b>Матч №{match_id} успешно завершен ({score_a}-{score_b}).</b>\n\n"
            f"Внимание: Результаты будут опубликованы в группе <b>после внесения всей статистики</b> и ввода <code>/done_actions</code>!\n\n"
            f"{roster_text}",
            parse_mode='HTML'
        )


async def done_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(Админ в ЛС) /done_actions - Завершает ввод статистики и публикует единое сводное сообщение в группу."""
    if update.effective_user.id not in ADMIN_IDS or 'finishing_match' not in context.user_data:
        return

    match_id = context.user_data.pop('finishing_match')
    await update.message.reply_text(
        "✅ Запись голевых действий успешно завершена. Сводный результат отправлен в группу.")

    if TARGET_GROUP_ID:
        with SessionLocal() as session:
            match = session.get(Match, match_id)
            if not match:
                return

            cap_a = get_player_by_tgid(session, match.captain_a_id)
            cap_b = get_player_by_tgid(session, match.captain_b_id)

            score_a = match.score_a
            score_b = match.score_b

            color_a = match.draft.team_a_color if (match.draft and match.draft.team_a_color) else "🔴"
            color_b = match.draft.team_b_color if (match.draft and match.draft.team_b_color) else "🔵"

            winner_team_name = cap_a.full_name if score_a > score_b else cap_b.full_name if score_b > score_a else "Ничья"
            congrats = f"🏆 Поздравляем команду <b>{escape(winner_team_name)}</b> с победой!" if score_a != score_b else "🤝 Боевая ничья!"

            # Сбор голевых действий
            actions = session.query(Action).filter_by(match_id=match_id).all()
            actions_text = ""
            if actions:
                actions_text = "\n\n📊 <b>Результативные действия участников:</b>\n"
                players_actions = {}
                for action in actions:
                    players_actions.setdefault(action.player_id, []).append(action.action_type)

                for player_id, action_list in players_actions.items():
                    player = session.get(Player, player_id)
                    goals = action_list.count('goal')
                    assists = action_list.count('assist')

                    parts = []
                    if goals > 0:
                        parts.append(f"⚽ {goals}")
                    if assists > 0:
                        parts.append(f"🎯 {assists}")

                    actions_text += f"• <b>{escape(player.full_name)}</b> ({escape(player.nickname)}): {', '.join(parts)}\n"

            tour_info = ""
            if match.tournament_pairing_id:
                pairing = match.pairing
                tour_info = f"\n🏆 <b>Турнир:</b> {pairing.tournament.name} ({pairing.stage})"

            # Единое объявление
            group_msg = (
                f"🏁 <b>Матч №{match_id} завершен!</b>{tour_info}\n\n"
                f"{color_a} Команда А ({escape(cap_a.full_name)})  <b>{score_a} - {score_b}</b>  {color_b} Команда Б ({escape(cap_b.full_name)})\n\n"
                f"{congrats}"
                f"{actions_text}"
            )

            await context.bot.send_message(chat_id=TARGET_GROUP_ID, text=group_msg, parse_mode='HTML')


async def player_form(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(Любой в ЛС) Показывает форму за 5 последних матчей."""
    if update.effective_chat.type != 'private':
        return
    if not context.args:
        return await update.message.reply_text("Использование: `/player_form <Никнейм>`")
    nickname = context.args[0]
    with SessionLocal() as session:
        player = get_player_by_nickname(session, nickname)
        if not player:
            return await update.message.reply_text(f"Игрок с никнеймом '{nickname}' не найден.")
        last_matches = session.query(Action.match_id).filter(Action.player_id == player.id).distinct().order_by(
            Action.match_id.desc()).limit(5).all()
        if not last_matches:
            return await update.message.reply_text("Нет данных о матчах для этого игрока.")
        match_ids = [m[0] for m in last_matches]
        summary = f"📈 <b>Форма игрока {player.full_name}</b> (ник: <code>{player.nickname}</code>) за последние {len(match_ids)} игр:\n\n"
        for mid in sorted(match_ids, reverse=True):
            goals = session.query(Action).filter_by(player_id=player.id, match_id=mid, action_type='goal').count()
            assists = session.query(Action).filter_by(player_id=player.id, match_id=mid, action_type='assist').count()
            summary += f"- <b>Матч №{mid}</b>: Голы: <b>{goals}</b> ⚽ | Ассисты: <b>{assists}</b> 🎯\n"
        await update.message.reply_text(summary, parse_mode='HTML')


async def private_message_dispatcher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Распределяет текст админа в ЛС на финализацию матча или внесение статистики."""
    if update.effective_user.id in ADMIN_IDS:
        if 'match_creation_state' in context.user_data:
            await finalize_match_creation(update, context, update.message.text.split())
        elif 'finishing_match' in context.user_data:
            await record_action_message(update, context)


async def record_action_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(Внутренняя) Пакетный парсер результативных действий одной строкой."""
    match_id = context.user_data.get('finishing_match')
    text = update.message.text.strip()
    parts = text.split(':')
    if len(parts) < 2:
        await update.message.reply_text("⚠️ Ошибка формата. Пример: <code>Goga270: 2 гола, 1 ассист</code>",
                                        parse_mode='HTML')
        return

    nickname = parts[0].strip()
    actions_str = parts[1].strip()

    with SessionLocal() as session:
        player = get_player_by_nickname(session, nickname)
        if not player:
            await update.message.reply_text(f"⚠️ Игрок '<code>{nickname}</code>' не найден.", parse_mode='HTML')
            return

        segments = actions_str.split(',')
        recorded_actions = []

        for seg in segments:
            seg_clean = seg.strip().lower()
            if not seg_clean:
                continue

            # Ищем число (например "2 гола" -> 2)
            numbers = [int(s) for s in seg_clean.split() if s.isdigit()]
            count = numbers[0] if numbers else 1

            action_type = None
            if any(x in seg_clean for x in ["гол", "goal", "g"]):
                action_type = "goal"
            elif any(x in seg_clean for x in ["пас", "ассист", "голевая", "assist", "a"]):
                action_type = "assist"

            if action_type:
                for _ in range(count):
                    act = Action(match_id=match_id, player_id=player.id, action_type=action_type, minute=None)
                    session.add(act)
                emoji = "⚽" if action_type == "goal" else "🎯"
                label = "гол(а/ов)" if action_type == "goal" else "пас(а/ов)"
                recorded_actions.append(f"{emoji} {count} {label}")

        session.commit()
        if recorded_actions:
            await update.message.reply_text(
                f"✅ Успешно записано для <b>{player.full_name}</b>: {', '.join(recorded_actions)}", parse_mode='HTML')
        else:
            await update.message.reply_text(
                "⚠️ Не удалось распознать действия. Используйте слова: гол/пас/ассист/goal/assist.")

# --- Управление турнирными сетками (Админ) ---

async def create_tournament(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(Админ в ЛС) Создает турнир и автоматически генерирует всю сетку слотов (включая матч за 3-е место)."""
    if update.effective_user.id not in ADMIN_IDS or update.effective_chat.type != 'private':
        return

    if len(context.args) < 3:
        await update.message.reply_text(
            "⚠️ <b>Неверный формат.</b> Используйте:\n"
            "<code>/create_tournament &lt;Название&gt; &lt;BO1/BO2&gt; &lt;Кол-во_игроков&gt;</code>\n\n"
            "Пример:\n"
            "<code>/create_tournament Кубок_Футболямбы BO2 16</code>",
            parse_mode='HTML'
        )
        return

    name = context.args[0]
    fmt = context.args[1].upper()
    try:
        raw_players = int(context.args[2])
    except ValueError:
        await update.message.reply_text("❌ Ошибка: Количество игроков должно быть числом.", parse_mode='HTML')
        return

    # Автоматическое округление участников до ближайшей степени 2 (Система пропусков Byes)
    num_players = get_nearest_power_of_2(raw_players)

    if fmt not in ["BO1", "BO2"]:
        await update.message.reply_text("❌ Ошибка: Формат должен быть строго <code>BO1</code> или <code>BO2</code>.",
                                        parse_mode='HTML')
        return

    with SessionLocal() as session:
        active_count = session.query(Tournament).filter_by(is_active=True).count()
        if active_count >= 2:
            await update.message.reply_text(
                "❌ Ошибка: В системе не может быть более 2 активных турниров одновременно. Закройте один из них.")
            return

        t = Tournament(name=name, match_format=fmt)
        session.add(t)
        session.flush()

        # Генерация пустых ячеек сетки
        rounds = []
        offset = 0
        k = num_players // 2
        while k >= 1:
            rounds.append({"offset": offset, "count": k})
            offset += k
            k = k // 2

        total_rounds = len(rounds)
        STAGE_MAP = {
            0: "ФИНАЛ",
            1: "1/2",
            2: "1/4",
            3: "1/8",
            4: "1/16",
            5: "1/32",
            6: "1/64",
            7: "1/128"
        }

        for r_idx, r in enumerate(rounds):
            dist = total_rounds - 1 - r_idx
            stage_name = STAGE_MAP.get(dist, f"Раунд_{r_idx + 1}")

            for i in range(1, r["count"] + 1):
                slot_id = r["offset"] + i
                pairing = TournamentPairing(
                    tournament_id=t.id,
                    slot_id=slot_id,
                    stage=stage_name,
                    captain_a_id=None,
                    captain_b_id=None,
                    is_completed=False
                )
                session.add(pairing)

        # <<< ДОБАВЛЕНО: Генерация матча за 3-е место в Слот N >>>
        third_place_pairing = TournamentPairing(
            tournament_id=t.id,
            slot_id=num_players,
            stage="МАТЧ_ЗА_3_МЕСТО",
            captain_a_id=None,
            captain_b_id=None,
            is_completed=False
        )
        session.add(third_place_pairing)

        session.commit()

        info_notice = ""
        if num_players != raw_players:
            info_notice = f"ℹ️ Указанное количество участников ({raw_players}) округлено до ближайшей степени двойки: <b>{num_players}</b> (пропуски Byes).\n\n"

        await update.message.reply_text(
            f"✅ {info_notice}Турнир <b>{name}</b> успешно создан (ID: {t.id}, формат: {fmt}).\n"
            f"Сгенерировано <b>{num_players}</b> пустых слотов сетки (включая матч за 3-е место).",
            parse_mode='HTML'
        )


async def add_pairing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(Админ в ЛС) Обновляет капитанов или вносит результат в уже сгенерированный Слот сетки."""
    if update.effective_user.id not in ADMIN_IDS or update.effective_chat.type != 'private':
        return

    if len(context.args) < 5:
        await update.message.reply_text(
            "⚠️ <b>Использование:</b> <code>/add_pairing &lt;ID_турнира&gt; &lt;Слот_ID&gt; &lt;Стадия&gt; &lt;Ник_А&gt; &lt;Ник_Б&gt; [Счет]</code>\n\n"
            "Примеры:\n"
            "• Назначить капитанов на слот: <code>/add_pairing 1 1 1/8 Rasta Miron</code>\n"
            "• Сыграна 1-я игра BO2: <code>/add_pairing 1 2 1/8 Ded Sanek 6-6</code>\n"
            "• Сыграны обе игры BO2: <code>/add_pairing 1 8 1/8 Griro Doktor 3-3,3-2</code>",
            parse_mode='HTML'
        )
        return

    try:
        t_id = int(context.args[0])
        slot_id = int(context.args[1])
        stage = context.args[2]
        nick_a = context.args[3]
        nick_b = context.args[4]
        score_str = context.args[5] if len(context.args) > 5 else None
    except ValueError:
        await update.message.reply_text("❌ Ошибка: Неверный формат параметров.")
        return

    if stage not in ALLOWED_STAGES:
        await update.message.reply_text(f"❌ Ошибка: Неверная стадия. Разрешено только: {', '.join(ALLOWED_STAGES)}",
                                        parse_mode='HTML')
        return

    with SessionLocal() as session:
        t = session.get(Tournament, t_id)
        if not t:
            await update.message.reply_text(f"❌ Турнир с ID {t_id} не найден.")
            return

        cap_a = None if nick_a.lower() in ['none', '?', 'tbd'] else get_player_by_nickname(session, nick_a)
        cap_b = None if nick_b.lower() in ['none', '?', 'tbd'] else get_player_by_nickname(session, nick_b)

        if nick_a.lower() not in ['none', '?', 'tbd'] and not cap_a:
            await update.message.reply_text(f"❌ Капитан '{nick_a}' не найден в БД.")
            return
        if nick_b.lower() not in ['none', '?', 'tbd'] and not cap_b:
            await update.message.reply_text(f"❌ Капитан '{nick_b}' не найден в БД.")
            return

        pairing = session.query(TournamentPairing).filter_by(tournament_id=t.id, slot_id=slot_id).first()
        if not pairing:
            pairing = TournamentPairing(tournament_id=t.id, slot_id=slot_id, stage=stage)
            session.add(pairing)

        pairing.stage = stage
        pairing.captain_a_id = cap_a.tg_id if cap_a else None
        pairing.captain_b_id = cap_b.tg_id if cap_b else None
        pairing.manual_score_text = score_str

        if score_str:
            if t.match_format == "BO1":
                pairing.is_completed = True
            elif t.match_format == "BO2":
                if len(score_str.split(',')) >= 2:
                    pairing.is_completed = True
                else:
                    pairing.is_completed = False
        else:
            pairing.is_completed = False

        session.commit()

        if pairing.is_completed:
            trigger_advancement(session, pairing)
            session.commit()

        status_text = f"обновлена со счетом <b>{score_str}</b>" if score_str else "успешно обновлена"
        name_a = cap_a.full_name if cap_a else "TBD"
        name_b = cap_b.full_name if cap_b else "TBD"

        await update.message.reply_text(
            f"✅ Ячейка Слот {slot_id} (Пара <b>{name_a} vs {name_b}</b>) {status_text} в турнире <b>{t.name}</b>.",
            parse_mode='HTML'
        )


async def list_tournaments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(Для всех) Вывод списка турниров."""
    with SessionLocal() as session:
        tournaments = session.query(Tournament).all()
        if not tournaments:
            await update.message.reply_text("Турниры в системе отсутствуют.")
            return

        msg = "🏆 <b>Список турниров сообщества Футболямба:</b>\n\n"
        for t in tournaments:
            status = "🟢 Активен" if t.is_active else "🔴 В архиве"
            msg += f"• <b>ID {t.id}</b>: {t.name} (Формат: <code>{t.match_format}</code>) — {status}\n"
        msg += "\nДля просмотра сетки: <code>/view_bracket &lt;ID_турнира&gt;</code>"
        await update.message.reply_text(msg, parse_mode='HTML')


async def view_bracket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(Для всех) Вывод сетки матчей турнира."""
    if not context.args:
        await update.message.reply_text("Использование: <code>/view_bracket &lt;ID_турнира&gt;</code>",
                                        parse_mode='HTML')
        return

    try:
        t_id = int(context.args[0])
    except ValueError:
        return

    with SessionLocal() as session:
        t = session.get(Tournament, t_id)
        if not t:
            await update.message.reply_text("❌ Турнир не найден.")
            return

        pairings = session.query(TournamentPairing).filter_by(tournament_id=t.id).order_by(
            TournamentPairing.slot_id.asc()).all()
        if not pairings:
            await update.message.reply_text(f"Сетка турнира <b>{t.name}</b> пока пуста.", parse_mode='HTML')
            return

        N = len(pairings)

        def fmt_slot(p):
            name_a = get_captain_name_or_placeholder(session, p.slot_id, t.id, 'a')
            name_b = get_captain_name_or_placeholder(session, p.slot_id, t.id, 'b')

            if p.is_completed:
                score = p.manual_score_text if p.manual_score_text else ", ".join(
                    f"{m.score_a}-{m.score_b}" for m in p.matches if m.status == MatchStatus.FINISHED)
                return f"Слот {p.slot_id}: <b>{name_a} vs {name_b}</b> (✅ {score})"
            else:
                score_info = f" (прошлый счет: {p.manual_score_text})" if p.manual_score_text else ""
                m_scores = [f"{m.score_a}-{m.score_b}" for m in p.matches if m.status == MatchStatus.FINISHED]
                if m_scores:
                    score_info = f" ({', '.join(m_scores)})"
                return f"Слот {p.slot_id}: {name_a} vs {name_b} (⏳ В процессе{score_info})"

        rounds = []
        offset = 0
        k = N // 2
        while k >= 1:
            rounds.append({"offset": offset, "count": k})
            offset += k
            k = k // 2

        total_rounds = len(rounds)
        STAGE_MAP = {
            0: "ФИНАЛ",
            1: "1/2 ПОЛУФИНАЛЫ",
            2: "1/4 ЧЕТВЕРТЬФИНАЛЫ",
            3: "1/8 ФИНАЛА",
            4: "1/16 ФИНАЛА",
            5: "1/32 ФИНАЛА",
            6: "1/64 ФИНАЛА",
            7: "1/128 ФИНАЛА"
        }

        msg = f"🏆 <b>Турнирная сетка кубка: {t.name} ({t.match_format})</b> 🏆\n\n"

        for r_idx, r in enumerate(rounds):
            dist = total_rounds - 1 - r_idx
            stage_title = STAGE_MAP.get(dist, f"РАУНД {r_idx + 1}").upper()

            msg += f"🏁 <b>{stage_title}:</b>\n"

            for i in range(1, r["count"] + 1):
                slot_id = r["offset"] + i
                p = next((x for x in pairings if x.slot_id == slot_id), None)
                if p:
                    msg += f"  • {fmt_slot(p)}\n"
            msg += "\n"

        # Отдельный красивый вывод Матча за 3-е место (Слот N)
        third_p = next((x for x in pairings if x.slot_id == N), None)
        if third_p:
            msg += f"🥉 <b>МАТЧ ЗА 3-Е МЕСТО:</b>\n"
            msg += f"  • {fmt_slot(third_p)}\n"

        await update.message.reply_text(msg, parse_mode='HTML')


async def close_tournament(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS or update.effective_chat.type != 'private':
        return

    if not context.args:
        await update.message.reply_text("Использование: <code>/close_tournament &lt;ID_турнира&gt;</code>",
                                        parse_mode='HTML')
        return

    try:
        t_id = int(context.args[0])
    except ValueError:
        return

    with SessionLocal() as session:
        t = session.get(Tournament, t_id)
        if not t:
            await update.message.reply_text("Турнир не найден.")
            return

        t.is_active = False
        session.commit()
        await update.message.reply_text(f"✅ Турнир <b>{t.name}</b> переведен в архив (завершен).", parse_mode='HTML')


# --- Логика архива призеров (Зал Славы) ---

async def tournament_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(Для всех) Показывает архив победителей всех завершенных турниров."""
    with SessionLocal() as session:
        results = session.query(TournamentResult).order_by(TournamentResult.completed_at.desc()).all()
        if not results:
            await update.message.reply_text("В Зале славы пока нет завершенных кубков.")
            return

        msg = "🏆 <b>Зал славы футбольного сообщества «Футболямба»:</b>\n\n"
        for r in results:
            t = r.tournament
            p1 = session.query(Player).filter_by(tg_id=r.first_place_id).first()
            p2 = session.query(Player).filter_by(tg_id=r.second_place_id).first()
            p3 = session.query(Player).filter_by(tg_id=r.third_place_id).first()

            name_1 = p1.full_name if p1 else "Неизвестно"
            name_2 = p2.full_name if p2 else "Неизвестно"
            name_3 = p3.full_name if p3 else "Неизвестно"

            msg += (
                f"🛡 <b>Турнир «{t.name}»</b>:\n"
                f"  🥇 Золото (1-е место): <b>{name_1}</b>\n"
                f"  🥈 Серебро (2-е место): <b>{name_2}</b>\n"
                f"  🥉 Бронза (3-е место): <b>{name_3}</b>\n"
                f"  📅 Дата триумфа: <i>{r.completed_at.strftime('%d.%m.%Y')}</i>\n\n"
            )
        await update.message.reply_text(msg, parse_mode='HTML')


# --- Публичные лидерборды ---

async def post_leaderboard_public(update: Update, context: ContextTypes.DEFAULT_TYPE, title: str, data: list,
                                  unit: str):
    chat_id = update.effective_chat.id
    if not data:
        message = f"{title}\n\nПока нет данных для составления рейтинга."
    else:
        message = f"{title}\n\n"
        medals = ["🥇", "🥈", "🥉"]
        for i, (player, count) in enumerate(data):
            prefix = medals[i] if i < 3 else f"{i + 1}."
            message += f"{prefix} {player.full_name} - <b>{count}</b> {unit}\n"

    try:
        await context.bot.send_message(chat_id=chat_id, text=message, parse_mode='HTML')
    except Exception as e:
        logger.error(f"Не удалось отправить рейтинг в чат {chat_id}: {e}")


async def top_scorers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with SessionLocal() as session:
        top_data = get_top_scorers_or_assisters(session, action_type='goal', limit=5)
    await post_leaderboard_public(update, context, "🏆 <b>Топ-5 бомбардиров сообщества</b>", top_data, "голов")


async def top_assisters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with SessionLocal() as session:
        top_data = get_top_scorers_or_assisters(session, action_type='assist', limit=5)
    await post_leaderboard_public(update, context, "🎯 <b>Топ-5 ассистентов сообщества</b>", top_data, "ассистов")


async def top_frequent_players(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with SessionLocal() as session:
        top_data = get_top_most_frequent_players(session, limit=10)
    await post_leaderboard_public(update, context, "🏃‍♂️ <b>Топ-10 самых активных игроков</b>", top_data, "матчей")


async def top_winners(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with SessionLocal() as session:
        top_data = get_top_winners(session, limit=5)
    await post_leaderboard_public(update, context, "👑 <b>Топ-5 победителей</b>", top_data, "побед")


async def list_active_matches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS or update.effective_chat.type != 'private':
        return

    with SessionLocal() as session:
        active_statuses = [MatchStatus.CREATED, MatchStatus.DRAFTING, MatchStatus.ACTIVE]
        active_matches = (
            session.query(Match)
            .filter(Match.status.in_(active_statuses))
            .order_by(Match.id.asc())
            .all()
        )

        if not active_matches:
            await update.message.reply_text("Нет активных или зависших матчей в системе.")
            return

        response_text = "⚠️ <b>Список активных (незакрытых) матчей:</b>\n\n"
        for match in active_matches:
            cap_a = get_player_by_tgid(session, match.captain_a_id)
            cap_b = get_player_by_tgid(session, match.captain_b_id)

            name_a = cap_a.full_name if cap_a else f"ID {match.captain_a_id}"
            name_b = cap_b.full_name if cap_b else f"ID {match.captain_b_id}"

            response_text += (
                f"• <b>Матч №{match.id}</b>\n"
                f"   - Статус: <code>{match.status.value}</code>\n"
                f"   - Капитаны: {name_a} vs {name_b}\n"
                f"   - Для отмены этого матча: <code>/cancel_match {match.id}</code>\n\n"
            )
        response_text += "Чтобы закрыть матч принудительно, используйте указанную команду отмены."
        await update.message.reply_text(response_text, parse_mode='HTML')


async def cancel_match_by_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS or update.effective_chat.type != 'private':
        return

    if not context.args:
        await update.message.reply_text("Использование: `/cancel_match <ID_матча>`")
        return

    try:
        match_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID матча должен быть целым числом.")
        return

    with SessionLocal() as session:
        match = session.get(Match, match_id)
        if not match:
            await update.message.reply_text(f"Матч №{match_id} не найден.")
            return

        if match.status in [MatchStatus.FINISHED, MatchStatus.CANCELLED]:
            await update.message.reply_text(f"Матч №{match_id} уже закрыт в статусе: `{match.status.value}`.")
            return

        match.status = MatchStatus.CANCELLED
        session.commit()
        await update.message.reply_text(f"✅ Матч №{match_id} успешно отменен. Статус изменен на `CANCELLED`.")


async def my_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != 'private':
        return

    with SessionLocal() as session:
        player = get_player_by_tgid(session, update.effective_user.id)
        if not player:
            await update.message.reply_text("Вы не зарегистрированы. Пройдите регистрацию: `/register`.",
                                            parse_mode='Markdown')
            return

        stats = get_player_stats(session, player)
        matches_count = stats['matches']
        wins = stats['wins']
        losses = stats['losses']
        draws = matches_count - wins - losses
        win_rate = (wins / matches_count * 100) if matches_count > 0 else 0.0

        stats_text = (
            f"📊 <b>Детальная статистика игрока {player.full_name}</b> (@{player.tg_username or 'нет'}):\n"
            f"⚽ Никнейм в системе: <code>{player.nickname}</code>\n\n"
            f"🏃‍♂️ Сыграно матчей: <b>{matches_count}</b>\n"
            f"   - Побед: <b>{wins}</b> 🟢\n"
            f"   - Поражений: <b>{losses}</b> 🔴\n"
            f"   - Ничьих: <b>{draws}</b> 🤝\n"
            f"📈 Процент побед: <b>{win_rate:.1f}%</b>\n"
            f"🔥 Победная серия: <b>{stats['win_streak']}</b> игр\n\n"
            f"🥅 Результативность:\n"
            f"   - Забитые голы: <b>{stats['goals']}</b> ⚽\n"
            f"   - Голевые передачи: <b>{stats['assists']}</b> 🎯"
        )
        await update.message.reply_text(stats_text, parse_mode='HTML')


# --- Настройка подсказок команд Telegram ---

async def post_init(application) -> None:
    from telegram import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats, BotCommandScopeChat

    group_commands = [
        BotCommand("help", "📖 Справка по командам группы"),
        BotCommand("top_scorers", "🏆 Топ-5 бомбардиров"),
        BotCommand("top_assisters", "🎯 Топ-5 ассистентов"),
        BotCommand("top_frequent", "🏃‍♂️ Топ-10 активных игроков"),
        BotCommand("top_winners", "👑 Топ-5 победителей"),
        BotCommand("list_tournaments", "📊 Список всех турниров"),
        BotCommand("view_bracket", "👀 Показать сетку турнира"),
        BotCommand("tournament_history", "🏆 Зал славы (архив победителей)"),
        BotCommand("search_player", "🔍 Поиск игрока по никнейму или имени"),
    ]
    await application.bot.set_my_commands(group_commands, scope=BotCommandScopeAllGroupChats())

    private_commands = [
        BotCommand("help", "📖 Справка по командам бота"),
        BotCommand("register", "✍️ Быстрая регистрация"),
        BotCommand("my_profile", "👤 Просмотр профиля"),
        BotCommand("my_stats", "📊 Моя детальная статистика"),
        BotCommand("edit_profile", "🛠 Редактировать профиль"),
        BotCommand("player_form", "📈 Форма игрока (последние игры)"),
        BotCommand("list_tournaments", "📊 Список всех турниров"),
        BotCommand("view_bracket", "👀 Показать сетку конкретного турнира"),
        BotCommand("tournament_history", "🏆 Зал славы (архив победителей)"),
        BotCommand("search_player", "🔍 Поиск игрока по никнейму или имени"),
    ]
    await application.bot.set_my_commands(private_commands, scope=BotCommandScopeAllPrivateChats())

    admin_commands = [
        BotCommand("help", "👑 Меню администрирования"),
        BotCommand("plan_match", "📅 Запланировать матч (опрос)"),
        BotCommand("register", "✍️ Быстрая регистрация"),
        BotCommand("list_planned", "📋 Список запланированных матчей"),
        BotCommand("create_match", "⚽ Создать матч на основе опроса"),
        BotCommand("list_active_matches", "⚠️ Список активных игр"),
        BotCommand("create_tournament", "🏆 Создать турнир (<Имя> <BO1/BO2> <Игроки>)"),
        BotCommand("add_pairing", "➕ Добавить пару в турнирную сетку"),
        BotCommand("list_tournaments", "📊 Список всех турниров"),
        BotCommand("view_bracket", "👀 Показать сетку турнира"),
        BotCommand("close_tournament", "🔒 Архивировать турнир"),
        BotCommand("my_profile", "👤 Профиль"),
        BotCommand("my_stats", "📊 Моя детальная статистика"),
        BotCommand("edit_profile", "🛠 Настройка профиля"),
        BotCommand("add_player", "➕ Зарегистрировать игрока без Telegram"),
        BotCommand("list_players", "👥 Список всех зарегистрированных игроков"),
        BotCommand("search_player", "🔍 Поиск игрока по никнейму или имени"),
        BotCommand("add_tournament_result", "🏆 Занести призеров турнира в Зал Славы")
    ]
    for admin_id in ADMIN_IDS:
        try:
            await application.bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as e:
            logger.warning(f"Не удалось установить персональные команды для админа {admin_id}: {e}")


# --- ЧАСТЬ 3. НОВЫЙ ФУНКЦИОНАЛ ДЛЯ РАБОТЫ С ИГРОКАМИ ---

async def add_player_by_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(Админ в ЛС) /add_player <Никнейм> <Имя Фамилия> - Принудительно регистрирует игрока без TG ID."""
    if update.effective_user.id not in ADMIN_IDS or update.effective_chat.type != 'private':
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "⚠️ <b>Неверный формат команды.</b>\n\n"
            "Используйте:\n<code>/add_player &lt;Никнейм&gt; &lt;Имя Фамилия&gt;</code>\n\n"
            "Пример:\n<code>/add_player Buba Буба</code>",
            parse_mode='HTML'
        )
        return

    nickname, full_name = args[0], " ".join(args[1:])
    if len(nickname.split()) > 1:
        await update.message.reply_text("❌ Ошибка: Никнейм должен быть одним словом (желательно латиницей).")
        return

    with SessionLocal() as session:
        if get_player_by_nickname(session, nickname):
            await update.message.reply_text(f"❌ Игрок с никнеймом '<code>{nickname}</code>' уже существует.",
                                            parse_mode='HTML')
            return

        # tg_id=None и tg_username=None для игроков без Telegram
        player = Player(tg_id=None, tg_username=None, nickname=nickname, full_name=full_name)
        session.add(player)
        session.commit()
        await update.message.reply_text(
            f"✅ <b>Виртуальный игрок успешно добавлен!</b>\n\n"
            f"• Никнейм для ввода: <code>{nickname}</code>\n"
            f"• Имя отображения: <b>{full_name}</b>\n\n"
            f"Теперь его можно добавлять в турнирные сетки и указывать при создании матчей.",
            parse_mode='HTML'
        )


async def list_players(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(Админ в ЛС) /list_players - Выводит полный список всех зарегистрированных игроков."""
    if update.effective_user.id not in ADMIN_IDS or update.effective_chat.type != 'private':
        return

    with SessionLocal() as session:
        players = session.query(Player).order_by(Player.full_name.asc()).all()
        if not players:
            await update.message.reply_text("В системе пока нет зарегистрированных игроков.")
            return

        msg = f"👥 <b>Зарегистрированные игроки сообщества ({len(players)} чел.):</b>\n\n"
        for p in players:
            tg_username_escaped = escape(p.tg_username) if p.tg_username else None
            tg_info = f"(@{tg_username_escaped})" if tg_username_escaped else "<i>(без ТГ)</i>"
            msg += f"• <b>{escape(p.full_name)}</b> — Никнейм: <code>{escape(p.nickname)}</code> {tg_info}\n"

        if len(msg) > 4000:
            for i in range(0, len(msg), 4000):
                await update.message.reply_text(msg[i:i + 4000], parse_mode='HTML')
        else:
            await update.message.reply_text(msg, parse_mode='HTML')


async def search_player_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(Для всех) /search_player <запрос> - Поиск игрока по никнейму, имени или юзернейму."""
    if not context.args:
        await update.message.reply_text(
            "Использование: <code>/search_player &lt;запрос&gt;</code>\n"
            "Пример: <code>/search_player goga</code>",
            parse_mode='HTML'
        )
        return

    query = " ".join(context.args)
    with SessionLocal() as session:
        players = find_players(session, query)
        if not players:
            await update.message.reply_text(f"🔍 По запросу «{query}» совпадений не найдено.")
            return

        msg = f"🔍 <b>Результаты поиска по запросу «{query}» ({len(players)}):</b>\n\n"
        for p in players:
            tg_username_escaped = escape(p.tg_username) if p.tg_username else None
            tg_info = f"(@{tg_username_escaped})" if tg_username_escaped else "<i>(без ТГ)</i>"
            msg += f"• <b>{escape(p.full_name)}</b> — Никнейм: <code>{escape(p.nickname)}</code> {tg_info}\n"

        await update.message.reply_text(msg, parse_mode='HTML')


async def add_tournament_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(Админ в ЛС) /add_tournament_result <ID_турнира> <Ник_1> [Ник_2] [Ник_3]"""
    if update.effective_user.id not in ADMIN_IDS or update.effective_chat.type != 'private':
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "⚠️ <b>Неверный формат.</b> Используйте:\n"
            "<code>/add_tournament_result &lt;ID_турнира&gt; &lt;Ник_1-го&gt; [Ник_2-го] [Ник_3-го]</code>\n\n"
            "Примеры:\n"
            "• Все призеры: <code>/add_tournament_result 1 Andro Lysyy Torokh</code>\n"
            "• Только чемпион: <code>/add_tournament_result 2 Sanek2 none none</code>",
            parse_mode='HTML'
        )
        return

    try:
        t_id = int(context.args[0])
        nick_1 = context.args[1]
        nick_2 = context.args[2] if len(context.args) > 2 else 'none'
        nick_3 = context.args[3] if len(context.args) > 3 else 'none'
    except ValueError:
        await update.message.reply_text("❌ Ошибка: ID турнира должен быть числом.")
        return

    with SessionLocal() as session:
        t = session.get(Tournament, t_id)
        if not t:
            await update.message.reply_text(f"❌ Турнир с ID {t_id} не найден.")
            return

        p1 = get_player_by_nickname(session, nick_1) if nick_1.lower() not in ['none', '?', 'tbd'] else None
        p2 = get_player_by_nickname(session, nick_2) if nick_2.lower() not in ['none', '?', 'tbd'] else None
        p3 = get_player_by_nickname(session, nick_3) if nick_3.lower() not in ['none', '?', 'tbd'] else None

        if nick_1.lower() not in ['none', '?', 'tbd'] and not p1:
            await update.message.reply_text(f"❌ Игрок '{nick_1}' не найден в БД.")
            return
        if nick_2.lower() not in ['none', '?', 'tbd'] and not p2:
            await update.message.reply_text(f"❌ Игрок '{nick_2}' не найден в БД.")
            return
        if nick_3.lower() not in ['none', '?', 'tbd'] and not p3:
            await update.message.reply_text(f"❌ Игрок '{nick_3}' не найден в БД.")
            return

        # Проверяем, нет ли уже занесенных результатов по этому турниру
        existing = session.query(TournamentResult).filter_by(tournament_id=t.id).first()
        if existing:
            await update.message.reply_text(f"❌ Результаты для турнира #{t_id} уже внесены.")
            return

        res = TournamentResult(
            tournament_id=t.id,
            first_place_id=p1.tg_id if p1 else None,
            second_place_id=p2.tg_id if p2 else None,
            third_place_id=p3.tg_id if p3 else None
        )
        session.add(res)

        # Переводим турнир в статус завершенного (архив)
        t.is_active = False
        session.commit()

        name_1 = p1.full_name if p1 else "Неизвестно"
        name_2 = p2.full_name if p2 else "Неизвестно"
        name_3 = p3.full_name if p3 else "Неизвестно"

        await update.message.reply_text(
            f"✅ <b>Результаты турнира «{t.name}» внесены в Зал Славы!</b>\n\n"
            f"🥇 Золото: <b>{name_1}</b>\n"
            f"🥈 Серебро: <b>{name_2}</b>\n"
            f"🥉 Бронза: <b>{name_3}</b>",
            parse_mode='HTML'
        )

def build_app():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    edit_profile_handler = ConversationHandler(
        entry_points=[CommandHandler("edit_profile", edit_profile_start)],
        states={
            EDIT_CHOICE: [CallbackQueryHandler(edit_choice)],
            EDIT_NICKNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_nickname)],
            EDIT_FULLNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_fullname)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
    )

    plan_match_handler = ConversationHandler(
        entry_points=[CommandHandler("plan_match", plan_match)],
        states={
            PLAN_FIELD: [MessageHandler(filters.TEXT & ~filters.COMMAND, plan_match_field)],
            PLAN_DATETIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, plan_match_datetime)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
    )

    create_match_handler = ConversationHandler(
        entry_points=[CommandHandler("create_match", create_match_start)],
        states={
            SELECT_PLAN: [CallbackQueryHandler(create_match_select_plan, pattern=r'^plan:')],
            SELECT_MATCH_TYPE: [CallbackQueryHandler(create_match_select_type, pattern=r'^match_type:')],
            CHOOSE_CAPTAIN_A: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_player_search_query),
                CallbackQueryHandler(handle_player_selection_callback, pattern=r'^select_player:'),
            ],
            CHOOSE_CAPTAIN_B: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_player_search_query),
                CallbackQueryHandler(handle_player_selection_callback, pattern=r'^select_player:'),
            ],
            SELECT_REMATCH: [CallbackQueryHandler(choose_friendly_rematch, pattern=r'^friendly_rematch:')],
            CHOOSE_TOURNAMENT_PAIRING: [
                CallbackQueryHandler(choose_tournament_pairing, pattern=r'^select_tour_pairing:')],
            MANUAL_ADD_PLAYERS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, finalize_match_creation_wrapper),
                CommandHandler("skip", create_match_skip_manual)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True
    )

    # 1. Диалоги
    app.add_handler(plan_match_handler)
    app.add_handler(edit_profile_handler)
    app.add_handler(create_match_handler)

    # 2. Публичные команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("my_chat_id", get_chat_id))
    app.add_handler(CommandHandler("top_scorers", top_scorers))
    app.add_handler(CommandHandler("top_assisters", top_assisters))
    app.add_handler(CommandHandler("top_frequent", top_frequent_players))
    app.add_handler(CommandHandler("top_winners", top_winners))
    app.add_handler(CommandHandler("list_tournaments", list_tournaments))
    app.add_handler(CommandHandler("view_bracket", view_bracket))
    app.add_handler(CommandHandler("tournament_history", tournament_history))
    app.add_handler(CommandHandler("search_player", search_player_command))

    # 3. Личные сообщения (Игроки)
    app.add_handler(CommandHandler("register", register_player))
    app.add_handler(CommandHandler("my_profile", my_profile))
    app.add_handler(CommandHandler("my_stats", my_stats))
    app.add_handler(CommandHandler("player_form", player_form))

    # 4. Команды администраторов
    app.add_handler(CommandHandler("list_planned", list_planned))
    app.add_handler(CommandHandler("start_draft", start_draft))
    app.add_handler(CommandHandler("finish_match", finish_match))
    app.add_handler(CommandHandler("done_actions", done_actions))
    app.add_handler(CommandHandler("create_tournament", create_tournament))
    app.add_handler(CommandHandler("add_pairing", add_pairing))
    app.add_handler(CommandHandler("close_tournament", close_tournament))
    app.add_handler(CommandHandler("list_active_matches", list_active_matches))
    app.add_handler(CommandHandler("cancel_match", cancel_match_by_id))
    app.add_handler(CommandHandler("add_player", add_player_by_admin))
    app.add_handler(CommandHandler("list_players", list_players))
    app.add_handler(CommandHandler("add_tournament_result", add_tournament_result))

    # 5. Команды для капитанов
    app.add_handler(CommandHandler("rps", rps_command))
    app.add_handler(CommandHandler("rps_decision", rps_decision))

    # 6. Обработка кнопок
    app.add_handler(CallbackQueryHandler(give_step1_callback, pattern=r'^give_step1:'))
    app.add_handler(CallbackQueryHandler(give_step2_callback, pattern=r'^give_step2:'))
    app.add_handler(CallbackQueryHandler(choose_from_pair_callback, pattern=r'^choose_from_pair:'))
    app.add_handler(CallbackQueryHandler(color_choice_callback, pattern=r'^color_choice:'))

    # 7. Системное
    app.add_handler(PollAnswerHandler(handle_poll_answer))
    app.add_handler(
        MessageHandler(filters.TEXT & (~filters.COMMAND) & filters.ChatType.PRIVATE, private_message_dispatcher))

    return app


if __name__ == "__main__":
    create_db_tables()
    app = build_app()
    logger.info("Запуск бота сообщества «Футболямба»...")
    app.run_polling()