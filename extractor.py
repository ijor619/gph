import json
import base64
import os
import re
import urllib.request
import urllib.error
from typing import List, Dict, Any
from PIL import Image

EXTRACTION_PROMPT = """
Ты — профессиональный ассистент по распознаванию документов курьеров для заключения договоров ГПХ.
Тебе переданы фото документов: паспорт, ВНЖ / патент / РВП, штамп регистрации, ИНН, СНИЛС, банковские реквизиты.

Извлеки точные данные и верни ТОЛЬКО чистый валидный JSON:
{
    "fio": "Фамилия Имя Отчество полностью на русском языке (например, Абдунабиев Садамбек Зафарович)",
    "citizenship": "гражданство в родительном падеже (например, 'республики Таджикистан', 'республики Азербайджан', 'республики Узбекистан', 'Российской Федерации')",
    "birth_date": "дата рождения ДД.ММ.ГГГГ (например, 19.12.1996)",
    "birth_place": "место рождения (например, Таджикистан, Азербайджан)",
    "passport_str": "документ удостоверяющий личность с серией/номером и датой выдачи (например, 'паспорт 403106091, выдан 07.07.2020')",
    "work_doc_full": "основание для ведения трудовой деятельности для преамбулы (например, 'Вид На Жительство иностранного гражданина 83№1107116, выдан 11.04.2025' или 'Патент 78 № 1234567, выдан 01.02.2025')",
    "work_doc_table": "основание для таблицы реквизитов (например, 'Вид На Жительство иностранного гражданина: 83№1107116')",
    "stay_basis": "основание для п. 5 договора (например, 'Вида На Жительство иностранного гражданина 83№1107116')",
    "stay_issuer": "кем выдан документ пребывания для п. 5 (например, 'ГУ МВД России по г. Санкт-Петербургу и Ленинградской области, дата выдачи документа 11.04.2025')",
    "reg_address": "полный адрес регистрации со штампа (например, 'г. Санкт-Петербург, пр.Сизова дом 32, корп. 1 лит Б, кв.568')",
    "inn": "номер ИНН 12 цифр (например, 780458282597)",
    "snils": "номер СНИЛС (например, 212-101-038-64)",
    "bik": "БИК банка 9 цифр (например, 044030653)",
    "rs": "расчетный счет 20 цифр (например, 40820810755170726650)",
    "ks": "корреспондентский счет 20 цифр (например, 30101810500000000653)"
}
Внимание:
1. Если какого-то поля на фото нет — оставь пустую строку "".
2. Не придумывай данные, бери строго то, что видно на фотографиях.
3. Верни ТОЛЬКО JSON без каких-либо markdown-символов.
"""

def clean_val(val: str) -> str:
    if not val: return ""
    val = val.strip().strip('"').strip("'")
    if "=" in val:
        val = val.split("=", 1)[1].strip().strip('"').strip("'")
    return val

def extract_via_gemini(image_paths: List[str], gemini_key: str) -> Dict[str, Any]:
    """Бесплатное распознавание через Google Gemini Flash (0 руб, без карт)"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={gemini_key}"
    
    parts = [{"text": EXTRACTION_PROMPT}]
    for p in image_paths:
        with open(p, "rb") as f:
            b64_data = base64.b64encode(f.read()).decode("utf-8")
        mime = "image/jpeg" if p.lower().endswith((".jpg", ".jpeg")) else "image/png"
        parts.append({
            "inline_data": {
                "mime_type": mime,
                "data": b64_data
            }
        })
        
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"temperature": 0.0, "responseMimeType": "application/json"}
    }
    
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        result = json.loads(resp.read().decode("utf-8"))
        text = result["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)

def extract_via_openai(image_paths: List[str], api_key: str, base_url: str = None) -> Dict[str, Any]:
    """Распознавание через OpenAI / ProxyAPI / OpenRouter"""
    from openai import OpenAI
    client = OpenAI(api_key=api_key, base_url=base_url)
    
    content = [{"type": "text", "text": "Распознай данные документов курьера и верни структурированный JSON."}]
    for img_path in image_paths:
        with open(img_path, "rb") as img_file:
            b64 = base64.b64encode(img_file.read()).decode("utf-8")
            mime = "image/jpeg" if img_path.lower().endswith((".jpg", ".jpeg")) else "image/png"
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime};base64,{b64}",
                    "detail": "high"
                }
            })
    
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": EXTRACTION_PROMPT},
            {"role": "user", "content": content}
        ],
        response_format={"type": "json_object"},
        temperature=0.0
    )
    return json.loads(response.choices[0].message.content)

def extract_via_tesseract_ocr(image_paths: List[str]) -> Dict[str, Any]:
    """Локальный OCR (Tesseract)"""
    import pytesseract
    full_text = ""
    for path in image_paths:
        img = Image.open(path)
        full_text += "\n" + pytesseract.image_to_string(img, lang="rus+eng")

    data = {
        "fio": "",
        "citizenship": "республики Таджикистан",
        "birth_date": "",
        "birth_place": "Таджикистан",
        "passport_str": "",
        "work_doc_full": "",
        "work_doc_table": "",
        "stay_basis": "",
        "stay_issuer": "ГУ МВД России по г. Санкт-Петербургу и Ленинградской области",
        "reg_address": "",
        "inn": "",
        "snils": "",
        "bik": "",
        "rs": "",
        "ks": ""
    }

    inn_match = re.search(r'\b(78\d{10}|\d{12})\b', full_text)
    if inn_match: data["inn"] = inn_match.group(1)

    snils_match = re.search(r'\b(\d{3}[-\s]\d{3}[-\s]\d{3}\s*\d{2})\b', full_text)
    if snils_match:
        parts = re.findall(r'\d+', snils_match.group(1))
        if len(parts) >= 4:
            data["snils"] = f"{parts[0]}-{parts[1]}-{parts[2]} {parts[3]}"

    bik_match = re.search(r'\b(04\d{7})\b', full_text)
    if bik_match: data["bik"] = bik_match.group(1)
        
    rs_match = re.search(r'\b(408\d{17})\b', full_text)
    if rs_match: data["rs"] = rs_match.group(1)
        
    ks_match = re.search(r'\b(301\d{17})\b', full_text)
    if ks_match: data["ks"] = ks_match.group(1)

    fio_matches = re.findall(r'(?:ФИО|Получатель)\s*\n*[\'"]?([А-ЯЁ\s]{8,50})', full_text, re.IGNORECASE)
    if fio_matches:
        raw_fio = fio_matches[0].strip().replace("\n", " ")
        stop_words = {'пол', 'дата', 'рождения', 'место', 'мужской', 'женский', 'свидетельство', 'детали', 'документа'}
        words = [w.capitalize() for w in raw_fio.split() if w.lower() not in stop_words and w.isalpha()]
        if len(words) >= 2:
            data["fio"] = " ".join(words[:4])

    bdate_match = re.search(r'\b(\d{2}\.\d{2}\.\d{4})\b', full_text)
    if bdate_match: data["birth_date"] = bdate_match.group(1)

    mrz = re.search(r'([A-Z0-9]{9})\d[A-Z]{3}(\d{6})\d[MF]', full_text)
    if mrz:
        doc_num = mrz.group(1)
        data["passport_str"] = f"паспорт {doc_num}"

    vnzh = re.search(r'(83\s*№?\s*11\d{5})', full_text)
    if vnzh:
        v_num = vnzh.group(1).replace(" ", "")
        data["work_doc_full"] = f"Вид На Жительство иностранного гражданина {v_num}"
        data["work_doc_table"] = f"Вид На Жительство иностранного гражданина: {v_num}"
        data["stay_basis"] = f"Вида На Жительство иностранного гражданина {v_num}"

    return data

def extract_data_from_images(image_paths: List[str]) -> Dict[str, Any]:
    """
    Диспетчер распознавания:
    1. Проверяет GEMINI_API_KEY (бесплатный AI Google)
    2. Проверяет OPENAI_API_KEY (OpenAI / ProxyAPI / OpenRouter)
    3. Пробует системный Tesseract
    4. Если ничего не настроено — выбрасывает понятную инструкцию
    """
    gemini_key = clean_val(os.getenv("GEMINI_API_KEY", ""))
    if gemini_key:
        try:
            return extract_via_gemini(image_paths, gemini_key)
        except Exception as e:
            raise RuntimeError(f"Ошибка Gemini API: {e}. Проверьте правильность GEMINI_API_KEY.")

    openai_key = clean_val(os.getenv("OPENAI_API_KEY", ""))
    base_url = clean_val(os.getenv("OPENAI_BASE_URL", "")) or None
    if openai_key.startswith("http://") or openai_key.startswith("https://"):
        if not base_url: base_url = openai_key
        openai_key = ""

    if openai_key:
        try:
            return extract_via_openai(image_paths, openai_key, base_url)
        except Exception as e:
            raise RuntimeError(f"Ошибка Vision API: {e}. Проверьте правильность OPENAI_API_KEY.")

    # Пробуем локальный Tesseract
    try:
        data = extract_via_tesseract_ocr(image_paths)
        # Если Tesseract вернул пустые поля (не смог распознать фото с телефона)
        if not data.get("fio") and not data.get("inn"):
            raise ValueError("Локальный OCR не смог распознать текст с фото.")
        return data
    except Exception as e:
        raise RuntimeError(
            "Для распознавания фото документов боту требуется бесплатный ключ зрения Google Gemini!\n\n"
            "Как получить за 30 секунд (100% БЕСПЛАТНО, без карт и денег):\n"
            "1. Откройте в браузере: https://aistudio.google.com/app/apikey\n"
            "2. Войдите через Google и нажмите «Create API key»\n"
            "3. Скопируйте ключ (начинается на AIzaSy...)\n"
            "4. В панели Bothost добавьте переменную: GEMINI_API_KEY со значением этого ключа\n"
            "5. Перезапустите бота (кнопка Restart)."
        )
