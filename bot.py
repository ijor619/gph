import asyncio
import os
import shutil
import tempfile
import datetime
import re
import logging
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

# Таймер автоудаления: ровно 3 минуты (180 секунд) с момента загрузки
AUTO_CLEANUP_SECONDS = 180

if not BOT_TOKEN:
    logger.warning("TELEGRAM_BOT_TOKEN не задан! Задайте его в переменных Bothost.")
    BOT_TOKEN = "1234567890:AAH_defaultdummytokenforloading000"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

user_sessions = {}

class FormStates(StatesGroup):
    waiting_for_docs = State()
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
        "📋 **Проверьте данные перед формированием документов:**\n\n"
        f"📑 **Номер договора:** № {data.get('contract_num', '1')}\n"
        f"📅 **Дата подписания:** {date_str} *(+1 день к календарю)*\n"
        f"⏳ **Срок действия:** с {date_str} по {end_date_str} *(6 месяцев)*\n"
        f"✍️ **Строка подписи:** `{fio_short} /_______/ {date_str}`\n\n"
        f"👤 **ФИО:** {fio}\n"
        f"🌍 **Гражданство:** {data.get('citizenship', '—')}\n"
        f"🎂 **Дата рождения:** {data.get('birth_date', '—')} (место: {data.get('birth_place', '—')})\n"
        f"🪪 **Паспорт:** {data.get('passport_str', '—')}\n"
        f"📄 **Основание работы:** {data.get('work_doc_full', '—')}\n"
        f"🏠 **Адрес регистрации:** {data.get('reg_address', '—')}\n"
        f"🏛 **Кем выдан статус (п.5):** {data.get('stay_issuer', '—')}\n"
        f"🔢 **ИНН:** {data.get('inn', '—')}\n"
        f"🔢 **СНИЛС:** {data.get('snils', '—')}\n\n"
        f"💳 **Банковские реквизиты:**\n"
        f"  • БИК: `{bik}`\n"
        f"  • Р/С: `{rs}`\n"
        f"  • К/С: `{ks}`\n\n"
        "👇 _Нажмите «Сформировать оба документа» — бот пришлёт Договор ГПХ и Согласие на ПД._\n"
        "⏱ _Таймер безопасности удалит все фото через 3 минуты._"
    )

async def trigger_cleanup_job(user_id: int, delay_seconds: int = AUTO_CLEANUP_SECONDS):
    """
    Фоновый таймер автоудаления:
    1. Удаляет сообщения с фото документов прямо из чата Telegram.
    2. Безвозвратно удаляет временную папку с файлами на сервере Bothost.
    """
    logger.info(f"Таймер удаления: старт обратного отсчета {delay_seconds}с для пользователя {user_id}")
    await asyncio.sleep(delay_seconds)
    session = user_sessions.get(user_id)
    if not session:
        return

    session_dir = session.get("dir")
    photo_messages = list(session.get("photo_messages", []))
    status_msg_id = session.get("status_msg_id")
    chat_id = session.get("chat_id")
    
    # 1. Удаление фото документов из чата Telegram
    deleted_chat_count = 0
    for c_id, msg_id in photo_messages:
        try:
            await bot.delete_message(chat_id=c_id, message_id=msg_id)
            deleted_chat_count += 1
        except Exception:
            pass

    # 2. Удаление файлов с сервера
    try:
        if session_dir and os.path.exists(session_dir):
            shutil.rmtree(session_dir, ignore_errors=True)
            logger.info(f"🧹 Сессия {user_id}: папка {session_dir} безвозвратно удалена по таймеру 3 мин.")
    except Exception as e:
        logger.error(f"Ошибка удаления файлов: {e}")

    # 3. Очистка сессии из памяти
    if user_id in user_sessions:
        del user_sessions[user_id]
        
    try:
        target_chat = chat_id or (photo_messages[0][0] if photo_messages else None)
        if target_chat:
            await bot.send_message(
                chat_id=target_chat,
                text="🧹 **Безопасность**: Прошло 3 минуты. Все загруженные фотографии документов и временные файлы были автоматически и безвозвратно удалены."
            )
    except Exception:
        pass

def start_or_reset_cleanup_timer(user_id: int):
    """Запускает или сбрасывает таймер автоудаления на 3 минуты"""
    session = user_sessions.get(user_id)
    if not session:
        return
    old_task = session.get("cleanup_task")
    if old_task and not old_task.done():
        old_task.cancel()
    session["cleanup_task"] = asyncio.create_task(trigger_cleanup_job(user_id, AUTO_CLEANUP_SECONDS))

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    
    # Очистка предыдущей сессии если была
    if user_id in user_sessions:
        old_task = user_sessions[user_id].get("cleanup_task")
        if old_task and not old_task.done():
            old_task.cancel()
        shutil.rmtree(user_sessions[user_id]["dir"], ignore_errors=True)
        
    user_sessions[user_id] = {
        "dir": tempfile.mkdtemp(prefix=f"courier_{user_id}_"),
        "photos": [],
        "photo_messages": [],
        "chat_id": message.chat.id,
        "status_msg_id": None,
        "notify_task": None,
        "bank_data": {},
        "contract_date": get_default_contract_date(),
        "extracted_data": None,
        "cleanup_task": None
    }
    await state.set_state(FormStates.waiting_for_docs)
    await message.answer(
        "👋 **Бот для автозаполнения договоров ГПХ и согласий на ПД курьеров**\n\n"
        "📸 **Отправьте документы курьера:**\n"
        "1. Паспорт (разворот с фото)\n"
        "2. Основание для работы (ВНЖ / Патент / РВП)\n"
        "3. Штамп регистрации (или бланк миграционного учёта)\n"
        "4. ИНН и СНИЛС\n"
        "5. Банковские реквизиты — **скрином из банка** ИЛИ **текстом сюда**\n\n"
        "💡 _Дата договора автоматически ставится на +1 день вперёд от сегодняшней, а срок — на 6 месяцев._\n"
        "🔒 _Все добавленные фото удаляются через 3 минуты._\n\n"
        "Жду отправки фото...",
        parse_mode="Markdown"
    )

async def update_upload_status_message(chat_id: int, user_id: int):
    """Дебаунс: ждет завершения загрузки пачки фото и отправляет/редактирует ВСЕГО ОДНО сообщение"""
    await asyncio.sleep(1.0)
    session = user_sessions.get(user_id)
    if not session:
        return
        
    count = len(session["photos"])
    if count == 0:
        return
        
    text = (
        f"📸 **Принято фото: {count} шт.**\n\n"
        f"Можете отправить ещё документы/реквизиты или нажать кнопку ниже 👇"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⚡ Запустить распознавание ({count} фото)", callback_data="process_photos")]
    ])
    
    status_msg_id = session.get("status_msg_id")
    if status_msg_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_msg_id,
                text=text,
                reply_markup=keyboard,
                parse_mode="Markdown"
            )
            return
        except Exception:
            pass
            
    sent_msg = await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    session["status_msg_id"] = sent_msg.message_id

@dp.message(FormStates.waiting_for_docs, F.photo)
async def handle_photo(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if user_id not in user_sessions:
        user_sessions[user_id] = {
            "dir": tempfile.mkdtemp(prefix=f"courier_{user_id}_"),
            "photos": [],
            "photo_messages": [],
            "chat_id": message.chat.id,
            "status_msg_id": None,
            "notify_task": None,
            "bank_data": {},
            "contract_date": get_default_contract_date(),
            "extracted_data": None,
            "cleanup_task": None
        }
    
    session = user_sessions[user_id]
    session["chat_id"] = message.chat.id
    
    try:
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        save_path = os.path.join(session["dir"], f"photo_{len(session['photos']) + 1}.jpg")
        await bot.download_file(file_info.file_path, save_path)
        
        session["photos"].append(save_path)
        session["photo_messages"].append((message.chat.id, message.message_id))
    except Exception as e:
        logger.error(f"Ошибка загрузки фото: {e}")
        await message.answer(f"⚠️ Ошибка при загрузке одного из файлов. Попробуйте отправить его повторно.")
        return

    # Запускаем 3-минутный таймер автоудаления с момента загрузки
    start_or_reset_cleanup_timer(user_id)
    
    # Схлопываем уведомления в ОДНО сообщение через дебаунс (без спама)
    old_task = session.get("notify_task")
    if old_task and not old_task.done():
        old_task.cancel()
    session["notify_task"] = asyncio.create_task(update_upload_status_message(message.chat.id, user_id))

@dp.message(FormStates.waiting_for_docs, F.text)
async def handle_text_during_upload(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if user_id not in user_sessions:
        user_sessions[user_id] = {
            "dir": tempfile.mkdtemp(prefix=f"courier_{user_id}_"),
            "photos": [],
            "photo_messages": [],
            "chat_id": message.chat.id,
            "status_msg_id": None,
            "notify_task": None,
            "bank_data": {},
            "contract_date": get_default_contract_date(),
            "extracted_data": None,
            "cleanup_task": None
        }
    
    bank_parsed = parse_bank_text(message.text)
    if bank_parsed:
        user_sessions[user_id]["bank_data"].update(bank_parsed)
        bik = bank_parsed.get("bik", "не найден")
        rs = bank_parsed.get("rs", "не найден")
        ks = bank_parsed.get("ks", "не найден")
        await message.answer(
            f"💳 **Банковские реквизиты распознаны из текста:**\n"
            f"• БИК: `{bik}`\n"
            f"• Р/С: `{rs}`\n"
            f"• К/С: `{ks}`\n\n"
            f"Они будут автоматически подставлены в договор!",
            parse_mode="Markdown"
        )
    else:
        await message.answer("📝 Текст сохранен. Загрузите остальные фото документов.")

@dp.callback_query(F.data == "process_photos")
async def process_photos_callback(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    session = user_sessions.get(user_id)
    if not session or not session.get("photos"):
        await callback.message.answer("⚠️ Сначала загрузите хотя бы 1 фото документа.")
        await callback.answer()
        return

    msg = await callback.message.answer("⏳ Распознаю текст с документов... (обычно 5-10 секунд)")
    await callback.answer()

    try:
        data = extract_data_from_images(session["photos"])
        if "contract_num" not in data:
            data["contract_num"] = "1"
            
        data["contract_date"] = session.get("contract_date") or get_default_contract_date()
            
        if session.get("bank_data"):
            for k, v in session["bank_data"].items():
                if v:
                    data[k] = v
                    
        session["extracted_data"] = data
        await state.set_state(FormStates.confirm_data)

        await msg.delete()
        await callback.message.answer(
            format_summary_message(data),
            parse_mode="Markdown",
            reply_markup=get_confirm_keyboard()
        )
    except Exception as e:
        logger.exception("Extraction error")
        await msg.edit_text(f"❌ Ошибка распознавания: {e}\nПопробуйте прислать фото повторно.")

@dp.callback_query(F.data.startswith("edit_"))
async def handle_edit_field_click(callback: types.CallbackQuery, state: FSMContext):
    field_to_edit = callback.data.replace("edit_", "")
    await state.update_data(editing_field=field_to_edit)
    await state.set_state(FormStates.edit_field)
    
    prompts = {
        "date": "Введите дату подписания в формате ДД.ММ.ГГГГ (например 20.09.2026) или напишите 'сегодня' / 'завтра':",
        "reg_address": "Введите точный адрес регистрации курьера (например: г. Санкт-Петербург, пр.Сизова дом 32, корп. 1 лит Б, кв.568):",
        "fio": "Введите ФИО курьера полностью на русском языке:",
        "contract_num": "Введите номер договора (например, 7):",
        "bank": "Отправьте текст реквизитов из банка (БИК, Р/С, К/С) — скопируйте как есть из приложения банка:"
    }
    prompt = prompts.get(field_to_edit, f"Введите новое значение для {field_to_edit}:")
    await callback.message.answer(f"✏️ {prompt}")
    await callback.answer()

@dp.message(FormStates.edit_field, F.text)
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
        await message.answer(
            format_summary_message(data),
            parse_mode="Markdown",
            reply_markup=get_confirm_keyboard()
        )
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

    fio_clean = data.get("fio", "Курьер").replace(" ", "_")
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
    await callback.message.answer_document(
        document=doc_contract,
        caption=f"📄 **1. Договор ГПХ** для курьера: **{data.get('fio')}**\n"
                f"Заполнены: шапка, срок (+6 мес.), п.5, полная таблица реквизитов и подпись.",
        parse_mode="Markdown"
    )

    # Отправка 2: Согласие на обработку ПД
    doc_pd = FSInputFile(pd_path, filename=pd_name)
    await callback.message.answer_document(
        document=doc_pd,
        caption=f"📑 **2. Согласие на обработку персональных данных**\n"
                f"Заполнены: ФИО, паспорт, адрес регистрации и строка подписи с датой.",
        parse_mode="Markdown"
    )

    await callback.message.answer(
        "🔒 **Безопасность персональных данных курьера:**\n"
        "Все фото и файлы будут автоматически удалены через 3 минуты (а также удалены из этого чата).\n\n"
        "Для оформления следующего курьера отправьте /start"
    )
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
    await callback.message.answer("🔄 Данные очищены. Чтобы начать заново, отправьте /start")
    await callback.answer()

async def main():
    logger.info("Запуск Telegram-бота курьеров...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
