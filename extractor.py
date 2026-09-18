import json
import base64
import os
import re
from typing import List, Dict, Any

EXTRACTION_SYSTEM_PROMPT = """
Ты — профессиональный ассистент по распознаванию документов для заключения договоров ГПХ (гражданско-правового характера) с курьерами.
Тебе на вход передаются фото документов курьера (паспорт иностранного гражданина или РФ, вид на жительство (ВНЖ) / патент / РВП, штамп регистрации по месту жительства / пребывания, свидетельство ИНН, СНИЛС, скриншот или текст банковских реквизитов).

Твоя задача — извлечь точные данные и вернуть ТОЛЬКО валидный JSON со следующей структурой:
{
    "fio": "Фамилия Имя Отчество полностью на русском языке (например, Абдунабиев Садамбек Зафарович)",
    "citizenship": "гражданство в родительном падеже (например, 'республики Таджикистан', 'республики Азербайджан', 'республики Узбекистан', 'Российской Федерации')",
    "birth_date": "дата рождения в формате ДД.ММ.ГГГГ (например, 19.12.1996)",
    "birth_place": "место рождения (например, Таджикистан, Азербайджан)",
    "passport_str": "наименование документа, серия, номер и дата выдачи (например, 'паспорт 403106091, выдан 07.07.2020')",
    "work_doc_full": "основание для ведения трудовой деятельности для преамбулы (например, 'Вид На Жительство иностранного гражданина 83№1107116, выдан 11.04.2025' или 'Патент 78 № 1234567, выдан 01.02.2025')",
    "work_doc_table": "основание для раздела реквизитов (например, 'Вид На Жительство иностранного гражданина: 83№1107116' или 'Патент: 78 № 1234567')",
    "stay_basis": "основание для п. 5 договора (например, 'Вида На Жительство иностранного гражданина 83№1107116' или 'патента 78 № 1234567')",
    "stay_issuer": "кем выдан документ пребывания для п. 5 (например, 'ГУ МВД России по г. Санкт-Петербургу и Ленинградской области, дата выдачи документа 11.04.2025')",
    "reg_address": "полный адрес регистрации со штампа (например, 'г. Санкт-Петербург, пр.Сизова дом 32, корп. 1 лит Б, кв.568')",
    "inn": "номер ИНН 12 цифр (например, 780458282597)",
    "snils": "номер СНИЛС (например, 212-101-038-64)",
    "bik": "БИК банка 9 цифр (например, 044030653)",
    "rs": "расчетный счет курьера 20 цифр (например, 40820810755170726650)",
    "ks": "корреспондентский счет банка 20 цифр (например, 30101810500000000653)"
}

Внимание:
1. Если какого-то поля на фото нет (например, банковских реквизитов), оставь поле пустым "" или "(заполнить)".
2. Не придумывай данные, бери строго то, что видно на фотографиях.
3. Верни ТОЛЬКО чистый JSON, без markdown-кавычек и пояснений.
"""

def clean_env_var(val: str) -> str:
    if not val:
        return ""
    val = val.strip().strip('"').strip("'")
    if "=" in val:
        val = val.split("=", 1)[1].strip().strip('"').strip("'")
    return val

def extract_data_from_images(image_paths: List[str], api_key: str = None) -> Dict[str, Any]:
    """
    Распознает комплект фото документов с помощью Vision API.
    Поддерживает стандартный OpenAI API или прокси (OPENAI_BASE_URL).
    """
    raw_key = api_key or os.getenv("OPENAI_API_KEY", "")
    raw_base = os.getenv("OPENAI_BASE_URL", "")

    api_key = clean_env_var(raw_key)
    base_url = clean_env_var(raw_base) or None

    # Защита от перепутанных полей (если URL попал в ключ)
    if api_key.startswith("http://") or api_key.startswith("https://"):
        if not base_url:
            base_url = api_key
        api_key = ""

    if not api_key:
        raise ValueError(
            "Не указан или некорректно заполнен OPENAI_API_KEY!\n"
            "Проверьте переменные окружения на Bothost:\n"
            "• OPENAI_API_KEY должен содержать ключ (начинается на sk- или ключ от ProxyAPI)\n"
            "• OPENAI_BASE_URL должен содержать только URL (например, https://api.proxyapi.ru/openai/v1), а не ключ!"
        )

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
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": content}
        ],
        response_format={"type": "json_object"},
        temperature=0.0
    )
    
    res_text = response.choices[0].message.content
    return json.loads(res_text)
