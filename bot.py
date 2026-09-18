import asyncio
import os
import shutil
import tempfile
import datetime
import re
import uuid
import logging
import html
from dotenv import load_dotenv

# Загрузка переменных окружения из .env
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
logger = logging.getLogger("courier_bot")

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from extractor import extract_data_from_images
from contract_filler import fill_gpd_contract, fill_pd_consent, get_russian_date, add_six_months, make_fio_initials

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip().strip('"').strip("'")
TEMPLATE_CONTRACT_PATH = os.getenv("TEMPLATE_PATH", os.path.join(BASE_DIR, "template.docx"))
TEMPLATE_PD_PATH = os.getenv("TEMPLATE_PD_PATH", os.path.join(BASE_DIR, "template_pd.docx"))

# Таймер автоудаления после отправки готовых файлов: 3 минуты (180 секунд)
AUTO_CLEANUP_SECONDS = 180

if not BOT_TOKEN:
    logger.warning("TELEGRAM_BOT_TOKEN не задан! Задайте его в переменных Bothost.")
    BOT_TOKEN = "1234567890:AAH_defaultdummytokenforloading000"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

user_sessions = {}

class FormStates(StatesGroup):
    waiting_for_docs = State()
    processing_docs = State()
    confirm_data = State()
    edit_field = State()

def get_default_contract_date() -> datetime.date:
    """Правило: текущая дата календаря + 1 день (завтрашний день)"""
    return datetime.date.today() + datetime.timedelta(days=1)

def parse_custom_date(text: str) -> datetime.date:
    text = text.strip().lower()
    today = datetime.date.today()
    if text in ["сегодня", "today"]:
        return today
    if text in ["завтра", "tomorrow"]:
        return today + datetime.timedelta(days=1)
    if text in ["послезавтра"]:
        return today + datetime.timedelta(days=2)
    for fmt in ["%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"]:
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return get_default_contract_date()

def parse_bank_text(text: str) -> dict:
    """Извлекает БИК, Р/С и К/С из текста любого банковского приложения"""
    details = {}
    bik_match = re.search(r'(?:БИК|BIK)?\s*[:=-]?\s*\b(04\d{7})\b', text, re.IGNORECASE)
    if bik_match:
        details['bik'] = bik_match.group(1)
        
    rs_match = re.search(r'(?:р/?с|расч[её]тный\s*сч[её]т|номер\s*сч[её]та|сч[её]т)?\s*[:=-]?\s*\b(4\d{19})\b', text, re.IGNORECASE)
    if rs_match:
        details['rs'] = rs_match.group(1)
        
    ks_match = re.search(r'(?:к/?с|корр?\.?\s*сч[её]т)?\s*[:=-]?\s*\b(301\d{17})\b', text, re.IGNORECASE)
    if ks_match:
        details['ks'] = ks_match.group(1)
        
    all_20 = re.findall(r'\b\d{20}\b', text)
    if 'rs' not in details:
        for num in all_20:
            if not num.startswith('301'):
                details['rs'] = num
                break
    if 'ks' not in details:
        for num in all_20:
            if num.startswith('301'):
                details['ks'] = num
                break
                
    if 'bik' not in details:
        bik_any = re.search(r'\b(04\d{7})\b', text)
        if bik_any:
            details['bik'] = bik_any.group(1)

    return details

def get_confirm_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Всё верно, сформировать оба документа", callback_data="generate_contract")],
        [InlineKeyboardButton(text="🔎 Проверить курьера в РКЛ МВД ↗️", callback_data="check_rkl")],
        [
            InlineKeyboardButton(text="📅 Изменить дату", callback_data="edit_date"),
            InlineKeyboardButton(text="🔢 Номер договора", callback_data="edit_contract_num")
        ],
        [
            InlineKeyboardButton(text="✏️ Исправить адрес / кв.", callback_data="edit_reg_address"),
            InlineKeyboardButton(text="✏️ Исправить ФИО", callback_data="edit_fio")
        ],
        [
            InlineKeyboardButton(text="💳 Ввести / изменить банк", callback_data="edit_bank"),
            InlineKeyboardButton(text="➕ Догрузить фото", callback_data="add_more_photos")
        ],
        [
            InlineKeyboardButton(text="🔄 Сброс", callback_data="reset_session")
        ]
    ])

def format_summary_message(data: dict) -> str:
    bik = data.get('bik') or '(не указан)'
    rs = data.get('rs') or '(не указан)'
    ks = data.get('ks') or '(не указан)'
    
    dt_start = data.get("contract_date") or get_default_contract_date()
    dt_end = add_six_months(dt_start)
    date_str = get_russian_date(dt_start)
    end_date_str = get_russian_date(dt_end)
    fio = data.get('fio', '—')
    fio_short = make_fio_initials(fio)
    
    return (
        "📋 <b>Проверьте данные перед формированием документов:</b>\n\n"
        f"📑 <b>Номер договора:</b> № {html.escape(str(data.get('contract_num', '1')))}\n"
        f"📅 <b>Дата подписания:</b> {html.escape(date_str)} <i>(+1 день к календарю)</i>\n"
        f"⏳ <b>Срок действия:</b> с {html.escape(date_str)} по {html.escape(end_date_str)} <i>(6 месяцев)</i>\n"
        f"✍️ <b>Строка подписи:</b> <code>{html.escape(fio_short)} /_______/ {html.escape(date_str)}</code>\n\n"
        f"👤 <b>ФИО:</b> {html.escape(str(fio))}\n"
        f"🌍 <b>Гражданство:</b> {html.escape(str(data.get('citizenship', '—')))}\n"
        f"🎂 <b>Дата и место рождения:</b> {html.escape(str(data.get('birth_date', '—')))}, {html.escape(str(data.get('birth_place', '—')))}\n"
        f"🪪 <b>Паспорт:</b> {html.escape(str(data.get('passport_str', '—')))}\n"
        f"📄 <b>Основание работы:</b> {html.escape(str(data.get('work_doc_full', '—')))}\n"
        f"📍 <b>Адрес регистрации:</b> {html.escape(str(data.get('reg_address', '—')))}\n"
        f"🔢 <b>ИНН:</b> <code>{html.escape(str(data.get('inn', '—')))}</code> | <b>СНИЛС:</b> <code>{html.escape(str(data.get('snils', '—')))}</code>\n\n"
        f"💳 <b>Банковские реквизиты:</b>\n"
        f"• БИК: <code>{html.escape(str(bik))}</code>\n"
        f"• Р/С: <code>{html.escape(str(rs))}</code>\n"
        f"• К/С: <code>{html.escape(str(ks))}</code>\n\n"
        f"🔒 <i>Файлы Word будут сформированы и автоматически удалены через 3 минуты после выдачи.</i>"
    )

def get_or_create_session(user_id: int, chat_id: int) -> dict:
    """Гарантирует существование сессии для пользователя"""
    if user_id not in user_sessions:
        user_sessions[user_id] = {
            "dir": tempfile.mkdtemp(prefix=f"courier_{user_id}_"),
            "photos": [],
            "cleanup_messages": [],
            "chat_id": chat_id,
            "status_msg_id": None,
            "notify_task": None,
            "bank_data": {},
            "contract_date": get_default_contract_date(),
            "extracted_data": None,
            "cleanup_task": None
        }
    return user_sessions[user_id]

async def trigger_cleanup_job(user_id: int, delay: int):
    """Задача автоудаления всех фото, файлов и сообщений"""
    await asyncio.sleep(delay)
    session = user_sessions.pop(user_id, None)
    if not session:
        return
        
    messages_to_delete = session.get("cleanup_messages", [])
    session_dir = session.get("dir")
    chat_id = session.get("chat_id")
    
    # 1. Удаление сообщений (фото, файлов docx) из чата Telegram
    for c_id, m_id in messages_to_delete:
        try:
            await bot.delete_message(chat_id=c_id, message_id=m_id)
            await asyncio.sleep(0.05)
        except Exception:
            pass

    # 2. Удаление файлов с сервера
    try:
        if session_dir and os.path.exists(session_dir):
            shutil.rmtree(session_dir, ignore_errors=True)
            logger.info(f"🧹 Сессия {user_id}: папка {session_dir} безвозвратно удалена.")
    except Exception as e:
        logger.error(f"Ошибка удаления файлов: {e}")
        
    try:
        target_chat = chat_id or (messages_to_delete[0][0] if messages_to_delete else None)
        if target_chat:
            await bot.send_message(
                chat_id=target_chat,
                text="🧹 <b>Безопасность (152-ФЗ)</b>: Прошло 3 минуты.\n"
                     "Все загруженные фотографии документов и сгенерированные файлы Word были <b>автоматически и безвозвратно удалены</b> из этого чата и с сервера.",
                parse_mode="HTML"
            )
    except Exception:
        pass

def start_or_reset_cleanup_timer(user_id: int, seconds: int = AUTO_CLEANUP_SECONDS):
    """Запускает или сбрасывает таймер автоудаления"""
    session = user_sessions.get(user_id)
    if not session:
        return
    old_task = session.get("cleanup_task")
    if old_task and not old_task.done():
        old_task.cancel()
    session["cleanup_task"] = asyncio.create_task(trigger_cleanup_job(user_id, seconds))

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    try:
        await state.clear()
        user_id = message.from_user.id
        
        if user_id in user_sessions:
            old_task = user_sessions[user_id].get("cleanup_task")
            if old_task and not old_task.done():
                old_task.cancel()
            shutil.rmtree(user_sessions[user_id]["dir"], ignore_errors=True)
            del user_sessions[user_id]
            
        get_or_create_session(user_id, message.chat.id)
        await state.set_state(FormStates.waiting_for_docs)
        
        welcome_html = (
            "👋 <b>Бот для автозаполнения договоров ГПХ и согласий на ПД курьеров</b>\n\n"
            "📸 <b>Отправьте фотографии документов курьера:</b>\n"
            "1. Паспорт (разворот с фото)\n"
            "2. Основание для работы (ВНЖ / Патент / РВП)\n"
            "3. Штамп регистрации (или бланк миграционного учёта)\n"
            "4. ИНН и СНИЛС\n"
            "5. Банковские реквизиты — <b>скрином из банка</b> ИЛИ <b>текстом сюда</b>\n\n"
            "💡 <i>Дата договора: +1 день к календарю, срок: 6 месяцев.</i>\n"
            "🔒 <i>Все фото и готовые документы удаляются через 3 минуты после выдачи.</i>\n\n"
            "Жду отправки фото..."
        )
        try:
            await message.answer(welcome_html, parse_mode="HTML")
        except Exception as parse_err:
            logger.error(f"HTML error in cmd_start: {parse_err}")
            await message.answer(
                "👋 Бот для автозаполнения договоров ГПХ и согласий на ПД курьеров\n\n"
                "📸 Отправьте фотографии документов курьера:\n"
                "1. Паспорт (разворот с фото)\n"
                "2. Основание для работы (ВНЖ / Патент / РВП)\n"
                "3. Штамп регистрации (или бланк миграционного учёта)\n"
                "4. ИНН и СНИЛС\n"
                "5. Банковские реквизиты — скрином из банка ИЛИ текстом сюда\n\n"
                "💡 Дата договора: +1 день к календарю, срок: 6 месяцев.\n"
                "🔒 Все фото и готовые документы удаляются через 3 минуты после выдачи.\n\n"
                "Жду отправки фото..."
            )
    except Exception as e:
        logger.exception(f"Unhandled error in cmd_start: {e}")

async def update_upload_status_message(chat_id: int, user_id: int):
    """Дебаунс: отправляет ровно одно свежее сообщение при загрузке фото курьера"""
    await asyncio.sleep(1.2)
    session = user_sessions.get(user_id)
    if not session:
        return
        
    count = len(session.get("photos", []))
    if count == 0:
        return
        
    text = (
        f"📸 <b>Принято фото документов: {count} шт.</b>\n\n"
        f"Можете отправить ещё документы/реквизиты или нажать кнопку ниже 👇"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⚡ Запустить распознавание ({count} фото)", callback_data="process_photos")],
        [InlineKeyboardButton(text="🗑 Очистить фото и начать заново", callback_data="clear_photos")]
    ])
    
    old_status_id = session.get("status_msg_id")
    if old_status_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=old_status_id)
        except Exception:
            pass
        session["status_msg_id"] = None
            
    sent_msg = await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    session["status_msg_id"] = sent_msg.message_id
    session["cleanup_messages"].append((chat_id, sent_msg.message_id))

@dp.message(F.photo)
async def handle_photo(message: types.Message, state: FSMContext):
    """Прием фотографий с уникальными именами файлов"""
    user_id = message.from_user.id
    session = get_or_create_session(user_id, message.chat.id)
    session["chat_id"] = message.chat.id
    await state.set_state(FormStates.waiting_for_docs)
    
    try:
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        photo_uid = uuid.uuid4().hex[:8]
        save_path = os.path.join(session["dir"], f"photo_{photo_uid}.jpg")
        await bot.download_file(file_info.file_path, save_path)
        
        session["photos"].append(save_path)
        session["cleanup_messages"].append((message.chat.id, message.message_id))
    except Exception as e:
        logger.error(f"Ошибка загрузки фото: {e}")
        await message.answer("⚠️ Ошибка при загрузке фото. Попробуйте отправить его повторно.")
        return

    # 10 минут на спокойную загрузку и проверку
    start_or_reset_cleanup_timer(user_id, seconds=600)
    
    old_task = session.get("notify_task")
    if old_task and not old_task.done():
        old_task.cancel()
    session["notify_task"] = asyncio.create_task(update_upload_status_message(message.chat.id, user_id))

@dp.message(F.document)
async def handle_document(message: types.Message, state: FSMContext):
    """Прием документов курьера, отправленных как файл"""
    doc = message.document
    mime = (doc.mime_type or "").lower()
    fname = (doc.file_name or "").lower()
    
    is_img = mime.startswith("image/") or fname.endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp"))
    if not is_img:
        await message.answer("⚠️ Пожалуйста, отправляйте документы курьера как <b>фотографии</b> (JPG, PNG).", parse_mode="HTML")
        return
        
    user_id = message.from_user.id
    session = get_or_create_session(user_id, message.chat.id)
    session["chat_id"] = message.chat.id
    await state.set_state(FormStates.waiting_for_docs)
    
    try:
        file_info = await bot.get_file(doc.file_id)
        ext = os.path.splitext(doc.file_name)[1] if doc.file_name else ".jpg"
        doc_uid = uuid.uuid4().hex[:8]
        save_path = os.path.join(session["dir"], f"doc_{doc_uid}{ext}")
        await bot.download_file(file_info.file_path, save_path)
        
        session["photos"].append(save_path)
        session["cleanup_messages"].append((message.chat.id, message.message_id))
    except Exception as e:
        logger.error(f"Ошибка загрузки файла документа: {e}")
        await message.answer("⚠️ Ошибка при загрузке файла. Попробуйте отправить повторно.")
        return

    start_or_reset_cleanup_timer(user_id, seconds=600)
    old_task = session.get("notify_task")
    if old_task and not old_task.done():
        old_task.cancel()
    session["notify_task"] = asyncio.create_task(update_upload_status_message(message.chat.id, user_id))

@dp.callback_query(F.data == "clear_photos")
async def clear_photos_callback(callback: types.CallbackQuery, state: FSMContext):
    """Очистка загруженных фото для начала заново"""
    user_id = callback.from_user.id
    session = user_sessions.get(user_id)
    if session:
        for p in session.get("photos", []):
            try:
                if os.path.exists(p): os.remove(p)
            except Exception:
                pass
        session["photos"] = []
        session["status_msg_id"] = None
        
    await callback.message.edit_text("🗑 Все загруженные фото удалены. Можете загрузить новые фото документов курьера:")
    await state.set_state(FormStates.waiting_for_docs)
    await callback.answer()

@dp.callback_query(F.data == "add_more_photos")
async def add_more_photos_callback(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(FormStates.waiting_for_docs)
    await callback.message.answer("📸 Отправьте дополнительные фото документов прямо в этот чат.")
    await callback.answer()

@dp.message(F.text)
async def handle_text_general(message: types.Message, state: FSMContext):
    """Универсальная обработка текстовых сообщений"""
    curr_state = await state.get_state()
    
    if curr_state == FormStates.processing_docs.state:
        await message.answer(
            "⏳ <b>Документы сейчас обрабатываются нейросетью.</b>\n"
            "Обычно это занимает около 1 минуты. Пожалуйста, дождитесь завершения...",
            parse_mode="HTML"
        )
        return

    if curr_state == FormStates.edit_field.state:
        await process_edited_field(message, state)
        return
        
    user_id = message.from_user.id
    session = get_or_create_session(user_id, message.chat.id)
    
    bank_parsed = parse_bank_text(message.text)
    if bank_parsed:
        session["bank_data"].update(bank_parsed)
        if session.get("extracted_data"):
            session["extracted_data"].update(bank_parsed)
        bik = bank_parsed.get("bik", "не найден")
        rs = bank_parsed.get("rs", "не найден")
        ks = bank_parsed.get("ks", "не найден")
        msg = await message.answer(
            "💳 <b>Банковские реквизиты распознаны из текста:</b>\n"
            f"• БИК: <code>{bik}</code>\n"
            f"• Р/С: <code>{rs}</code>\n"
            f"• К/С: <code>{ks}</code>\n\n"
            "Они будут автоматически подставлены в договор!",
            parse_mode="HTML"
        )
        session["cleanup_messages"].append((msg.chat.id, msg.message_id))
    else:
        if curr_state == FormStates.confirm_data.state:
            await message.answer("ℹ️ Вы находитесь на этапе проверки данных. Воспользуйтесь кнопками под карточкой курьера выше или отправьте /start.")
        else:
            await message.answer("📝 Текст принят. Загрузите фото документов курьера или отправьте /start.")

@dp.callback_query(F.data == "process_photos")
async def process_photos_callback(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    session = user_sessions.get(user_id)
    if not session or not session.get("photos"):
        await callback.message.answer("⚠️ Сначала загрузите хотя бы 1 фото документа курьера.")
        await callback.answer()
        return

    await state.set_state(FormStates.processing_docs)
    msg = await callback.message.answer(
        "⏳ <b>Распознаю документы курьера...</b> (обычно около 1 минуты, пожалуйста, подождите)",
        parse_mode="HTML"
    )
    await callback.answer()

    try:
        await bot.send_chat_action(chat_id=callback.message.chat.id, action="typing")
    except Exception:
        pass

    try:
        data = await asyncio.to_thread(extract_data_from_images, session["photos"])
        if "contract_num" not in data:
            data["contract_num"] = "1"
            
        data["contract_date"] = session.get("contract_date") or get_default_contract_date()
            
        if session.get("bank_data"):
            for k, v in session["bank_data"].items():
                if v:
                    data[k] = v
                    
        session["extracted_data"] = data
        await state.set_state(FormStates.confirm_data)

        try:
            await msg.delete()
        except Exception:
            pass

        summary_msg = await callback.message.answer(
            format_summary_message(data),
            parse_mode="HTML",
            reply_markup=get_confirm_keyboard()
        )
        session["cleanup_messages"].append((summary_msg.chat.id, summary_msg.message_id))
    except Exception as e:
        logger.exception("Extraction error")
        session["status_msg_id"] = None
        await state.set_state(FormStates.waiting_for_docs)
        
        raw_err = str(e)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Повторить попытку", callback_data="process_photos")],
            [InlineKeyboardButton(text="🗑 Очистить фото и загрузить заново", callback_data="clear_photos")]
        ])
        
        safe_err = html.escape(raw_err[:250])
        err_text = (
            f"❌ <b>Ошибка распознавания:</b>\n\n"
            f"<code>{safe_err}</code>\n\n"
            f"💡 <i>Нажмите «Повторить попытку» или отправьте недостающие данные текстом.</i>"
        )
        try:
            await msg.edit_text(err_text, reply_markup=keyboard, parse_mode="HTML")
        except Exception:
            try:
                await msg.delete()
            except Exception:
                pass
            await callback.message.answer(err_text, reply_markup=keyboard, parse_mode="HTML")

@dp.callback_query(F.data == "check_rkl")
async def check_rkl_callback(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    session = user_sessions.get(user_id)
    if not session or not session.get("extracted_data"):
        await callback.answer("⚠️ Нет данных курьера для проверки.", show_alert=True)
        return

    data = session["extracted_data"]
    fio = data.get("fio", "").strip()
    parts = fio.split()
    last_name = parts[0] if len(parts) > 0 else "—"
    first_name = parts[1] if len(parts) > 1 else "—"
    patronymic = parts[2] if len(parts) > 2 else ""
    
    bdate = data.get("birth_date", "—")
    pass_str = data.get("passport_str", "—")
    
    # Извлекаем номер паспорта (цифры/буквы серии и номера)
    m_pass = re.search(r'(?:паспорт\s*)?([A-Z0-9]{6,12})', pass_str, re.IGNORECASE)
    pass_num = m_pass.group(1) if m_pass else pass_str

    text = (
        "👮‍♂️ <b>Данные курьера для проверки в Реестре контролируемых лиц (РКЛ МВД):</b>\n\n"
        "💡 <i>Нажмите на значение в рамке, чтобы скопировать его в 1 клик для вставки на сайт МВД:</i>\n\n"
        f"• <b>Фамилия:</b> <code>{html.escape(last_name)}</code>\n"
        f"• <b>Имя:</b> <code>{html.escape(first_name)}</code>\n"
    )
    if patronymic:
        text += f"• <b>Отчество:</b> <code>{html.escape(patronymic)}</code>\n"
    text += (
        f"• <b>Дата рождения:</b> <code>{html.escape(bdate)}</code>\n"
        f"• <b>Серия и номер документа:</b> <code>{html.escape(pass_num)}</code>\n\n"
        "🌐 <i>Перейдите на официальный сайт МВД России по кнопке ниже:</i>"
    )

    rkl_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Открыть форму РКЛ на сайте мвд.рф ↗️", url="https://xn--b1aew.xn--p1ai/rkl")],
        [InlineKeyboardButton(text="✅ Всё проверено, сформировать документы", callback_data="generate_contract")]
    ])

    msg = await callback.message.answer(text, reply_markup=rkl_keyboard, parse_mode="HTML")
    session["cleanup_messages"].append((msg.chat.id, msg.message_id))
    await callback.answer()

@dp.callback_query(F.data.startswith("edit_"))
async def handle_edit_field_click(callback: types.CallbackQuery, state: FSMContext):
    field_to_edit = callback.data.replace("edit_", "")
    await state.update_data(editing_field=field_to_edit)
    await state.set_state(FormStates.edit_field)
    
    prompts = {
        "date": "Введите дату подписания в формате ДД.ММ.ГГГГ (например 20.09.2026) или напишите 'сегодня' / 'завтра':",
        "reg_address": "Введите точный адрес регистрации курьера (например: г. Санкт-Петербург, пр.Сизова дом 32, корп. 1 лит Б, кв. 1168):",
        "fio": "Введите ФИО курьера полностью на русском языке:",
        "contract_num": "Введите номер договора (например, 7):",
        "bank": "Отправьте текст реквизитов из банка (БИК, Р/С, К/С) — скопируйте как есть из приложения банка:"
    }
    prompt = prompts.get(field_to_edit, f"Введите новое значение для {field_to_edit}:")
    await callback.message.answer(f"✏️ {prompt}")
    await callback.answer()

async def process_edited_field(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    session = user_sessions.get(user_id)
    state_data = await state.get_data()
    field = state_data.get("editing_field")
    
    if session and session.get("extracted_data"):
        data = session["extracted_data"]
        
        if field == "date":
            new_dt = parse_custom_date(message.text)
            data["contract_date"] = new_dt
            session["contract_date"] = new_dt
        elif field == "bank":
            parsed = parse_bank_text(message.text)
            if parsed:
                data.update(parsed)
            else:
                lines = message.text.split()
                if len(lines) >= 1: data["bik"] = lines[0]
                if len(lines) >= 2: data["rs"] = lines[1]
                if len(lines) >= 3: data["ks"] = lines[2]
        else:
            data[field] = message.text.strip()
            
        await state.set_state(FormStates.confirm_data)
        await message.answer("✅ Данные обновлены!")
        summary_msg = await message.answer(
            format_summary_message(data),
            parse_mode="HTML",
            reply_markup=get_confirm_keyboard()
        )
        session["cleanup_messages"].append((summary_msg.chat.id, summary_msg.message_id))
    else:
        await message.answer("⚠️ Сессия не найдена. Нажмите /start")

@dp.callback_query(F.data == "generate_contract")
async def generate_contract_callback(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    session = user_sessions.get(user_id)
    if not session or not session.get("extracted_data"):
        await callback.message.answer("⚠️ Нет данных для формирования. Отправьте /start")
        await callback.answer()
        return

    data = session["extracted_data"]
    if not data.get("contract_date"):
        data["contract_date"] = get_default_contract_date()

    fio_clean = (data.get("fio") or "Курьер").strip().replace(" ", "_")
    session_dir = session["dir"]

    # 1. Формирование договора ГПХ
    contract_name = f"Договор_ГПХ_{fio_clean}.docx"
    contract_path = os.path.join(session_dir, contract_name)
    fill_gpd_contract(TEMPLATE_CONTRACT_PATH, contract_path, data)

    # 2. Формирование согласия на обработку ПД
    pd_name = f"Согласие_на_обработку_ПД_{fio_clean}.docx"
    pd_path = os.path.join(session_dir, pd_name)
    fill_pd_consent(TEMPLATE_PD_PATH, pd_path, data)

    # Отправка 1: Договор ГПХ
    doc_contract = FSInputFile(contract_path, filename=contract_name)
    msg_contract = await callback.message.answer_document(
        document=doc_contract,
        caption=f"📄 <b>1. Договор ГПХ</b> для курьера: <b>{html.escape(data.get('fio', 'Курьер'))}</b>\n"
                f"Заполнены: шапка, срок (+6 мес.), п.5, полная таблица реквизитов и подпись.",
        parse_mode="HTML"
    )
    session["cleanup_messages"].append((msg_contract.chat.id, msg_contract.message_id))

    # Отправка 2: Согласие на обработку ПД
    doc_pd = FSInputFile(pd_path, filename=pd_name)
    msg_pd = await callback.message.answer_document(
        document=doc_pd,
        caption=f"📑 <b>2. Согласие на обработку персональных данных</b>\n"
                f"Заполнены: ФИО, паспорт, адрес регистрации и строка подписи с датой.",
        parse_mode="HTML"
    )
    session["cleanup_messages"].append((msg_pd.chat.id, msg_pd.message_id))

    msg_notice = await callback.message.answer(
        "🔒 <b>Безопасность персональных данных курьера (152-ФЗ):</b>\n"
        "Ровно через <b>3 минуты</b> все загруженные фото и оба сгенерированных файла Word будут <b>автоматически удалены из этого чата</b> и стёрты с сервера.\n\n"
        "Успейте скачать файлы себе на устройство!\n"
        "Для следующего курьера отправьте /start",
        parse_mode="HTML"
    )
    session["cleanup_messages"].append((msg_notice.chat.id, msg_notice.message_id))
    
    # КРИТИЧЕСКИ ВАЖНО: Запускаем честный 3-минутный таймер С МОМЕНТА ВЫДАЧИ ФАЙЛОВ!
    start_or_reset_cleanup_timer(user_id, seconds=AUTO_CLEANUP_SECONDS)
    await callback.answer()

@dp.callback_query(F.data == "reset_session")
async def reset_session_callback(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if user_id in user_sessions:
        session = user_sessions[user_id]
        old_task = session.get("cleanup_task")
        if old_task and not old_task.done():
            old_task.cancel()
        shutil.rmtree(session["dir"], ignore_errors=True)
        del user_sessions[user_id]
    await state.clear()
    await callback.message.answer("🔄 Данные очищены. Чтобы начать заново, отправьте /start или пришлите новые фото.")
    await callback.answer()

async def main():
    logger.info("Запуск Telegram-бота курьеров...")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logger.warning(f"Could not delete webhook: {e}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
