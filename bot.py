import asyncio
import os
import shutil
import tempfile
import datetime
import re
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from extractor import extract_data_from_images
from contract_filler import fill_gpd_contract

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
TEMPLATE_PATH = os.getenv("TEMPLATE_PATH", "input_files/пример, который надо заполнять.docx")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

user_sessions = {}

class FormStates(StatesGroup):
    waiting_for_docs = State()
    confirm_data = State()
    edit_field = State()

def parse_bank_text(text: str) -> dict:
    """Извлекает БИК, Р/С и К/С из любого скопированного банковского текста"""
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
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Всё верно, скачать договор (.docx)", callback_data="generate_contract")],
        [
            InlineKeyboardButton(text="✏️ Исправить адрес / кв.", callback_data="edit_reg_address"),
            InlineKeyboardButton(text="✏️ Исправить ФИО", callback_data="edit_fio")
        ],
        [
            InlineKeyboardButton(text="💳 Ввести / изменить банк", callback_data="edit_bank"),
            InlineKeyboardButton(text="🔢 Номер договора", callback_data="edit_contract_num")
        ],
        [
            InlineKeyboardButton(text="➕ Догрузить фото", callback_data="add_more_photos"),
            InlineKeyboardButton(text="🔄 Сброс", callback_data="reset_session")
        ]
    ])
    return keyboard

def format_summary_message(data: dict) -> str:
    bik = data.get('bik') or '(не указан)'
    rs = data.get('rs') or '(не указан)'
    ks = data.get('ks') or '(не указан)'
    
    return (
        "📋 **Проверьте распознанные данные курьера:**\n\n"
        f"📑 **Номер договора:** № {data.get('contract_num', '1')}\n"
        f"👤 **ФИО:** {data.get('fio', '—')}\n"
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
        "👇 _Если всё верно, нажмите «Всё верно». Если нужно подправить квартиру или ввести реквизиты текстом — воспользуйтесь кнопками ниже._"
    )

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    if user_id in user_sessions:
        shutil.rmtree(user_sessions[user_id]["dir"], ignore_errors=True)
    user_sessions[user_id] = {
        "dir": tempfile.mkdtemp(prefix=f"courier_{user_id}_"),
        "photos": [],
        "bank_data": {},
        "extracted_data": None
    }
    await state.set_state(FormStates.waiting_for_docs)
    await message.answer(
        "👋 **Бот для автозаполнения договоров ГПХ с курьерами**\n\n"
        "📸 **Отправьте документы курьера:**\n"
        "1. Паспорт (разворот с фото)\n"
        "2. Основание для работы (ВНЖ / Патент / РВП)\n"
        "3. Штамп регистрации (или бланк миграционного учёта)\n"
        "4. ИНН и СНИЛС\n"
        "5. Банковские реквизиты — **скрином из банка** ИЛИ **просто текстом скопируйте сюда**\n\n"
        "Фото можно присылать альбомом или по очереди. "
        "Когда закончите — нажмите «⚡ Запустить распознавание» 👇",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Запустить распознавание", callback_data="process_photos")]
        ]),
        parse_mode="Markdown"
    )

@dp.message(FormStates.waiting_for_docs, F.photo)
async def handle_photo(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if user_id not in user_sessions:
        user_sessions[user_id] = {
            "dir": tempfile.mkdtemp(prefix=f"courier_{user_id}_"),
            "photos": [],
            "bank_data": {},
            "extracted_data": None
        }
    
    photo = message.photo[-1]
    file_info = await bot.get_file(photo.file_id)
    save_path = os.path.join(user_sessions[user_id]["dir"], f"photo_{len(user_sessions[user_id]['photos']) + 1}.jpg")
    await bot.download_file(file_info.file_path, save_path)
    user_sessions[user_id]["photos"].append(save_path)
    
    count = len(user_sessions[user_id]["photos"])
    await message.answer(
        f"✅ Принято фото #{count}.\n"
        f"Отправьте следующее фото, вставьте реквизиты текстом или запустите распознавание:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"⚡ Распознать ({count} фото)", callback_data="process_photos")]
        ])
    )

@dp.message(FormStates.waiting_for_docs, F.text)
async def handle_text_during_upload(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if user_id not in user_sessions:
        user_sessions[user_id] = {
            "dir": tempfile.mkdtemp(prefix=f"courier_{user_id}_"),
            "photos": [],
            "bank_data": {},
            "extracted_data": None
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
        await message.answer("📝 Заметка сохранена. Не забудьте загрузить фото документов.")

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
            
        # Если реквизиты были введены текстом ранее — объединяем
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
        await msg.edit_text(f"❌ Ошибка распознавания: {e}\nПопробуйте прислать фото повторно.")

@dp.callback_query(F.data.startswith("edit_"))
async def handle_edit_field_click(callback: types.CallbackQuery, state: FSMContext):
    field_to_edit = callback.data.replace("edit_", "")
    await state.update_data(editing_field=field_to_edit)
    await state.set_state(FormStates.edit_field)
    
    prompts = {
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
        
        if field == "bank":
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
        await message.answer("✅ Поле успешно обновлено!")
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
        await callback.message.answer("⚠️ Нет данных для договора. Отправьте /start")
        await callback.answer()
        return

    data = session["extracted_data"]
    data["contract_date"] = datetime.date.today()

    fio_clean = data.get("fio", "Курьер").replace(" ", "_")
    out_name = f"Договор_ГПХ_{fio_clean}.docx"
    out_path = os.path.join(session["dir"], out_name)

    fill_gpd_contract(TEMPLATE_PATH, out_path, data)

    doc_file = FSInputFile(out_path, filename=out_name)
    await callback.message.answer_document(
        document=doc_file,
        caption=f"🎉 **Договор готов!**\n"
                f"👤 Курьер: {data.get('fio')}\n"
                f"📄 Документ: Word (.docx) с оригинальными шрифтами и стилями.",
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "reset_session")
async def reset_session_callback(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if user_id in user_sessions:
        shutil.rmtree(user_sessions[user_id]["dir"], ignore_errors=True)
        del user_sessions[user_id]
    await state.clear()
    await callback.message.answer("🔄 Данные очищены. Чтобы начать заново, отправьте /start")
    await callback.answer()

async def main():
    print("Бот запускается...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
