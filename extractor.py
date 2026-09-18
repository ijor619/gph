import json
import base64
import os
from typing import List, Dict, Any
from PIL import Image

EXTRACTION_SYSTEM_PROMPT = """
Ты — профессиональный ассистент по распознаванию документов для заключения договоров ГПХ с курьерами.
Тебе на вход передаются фото документов курьера (паспорт, ВНЖ / патент / РВП, регистрация, ИНН, СНИЛС, банковские реквизиты).

Твоя задача — извлечь точные данные и вернуть ТОЛЬКО валидный JSON со следующей структурой:
{
    "fio": "Фамилия Имя Отчество полностью на русском языке (например, Муртузалиев Зия Азер оглу)",
    "citizenship": "гражданство в родительном падеже (например, 'республики Азербайджан', 'республики Таджикистан', 'Российской Федерации')",
    "birth_date": "дата рождения в формате ДД.ММ.ГГГГ (например, 21.08.1996)",
    "birth_place": "место рождения (например, Азербайджан, Таджикистан)",
    "passport_str": "наименование документа, серия, номер и дата выдачи (например, 'паспорт C05216090, выдан 26.07.2024')",
    "work_doc_full": "основание для ведения трудовой деятельности для преамбулы (например, 'Вид На Жительство иностранного гражданина 83№1110247, выдан 16.07.2025' или 'Патент 78 № 1234567, выдан 01.02.2025')",
    "work_doc_table": "основание для раздела реквизитов (например, 'Вид На Жительство иностранного гражданина: 83№1110247')",
    "stay_basis": "основание для п. 5 договора (например, 'Вида На Жительство иностранного гражданина 83№1110247')",
    "stay_issuer": "кем выдан документ пребывания для п. 5 (например, 'ГУ МВД России по г. Санкт-Петербургу и Ленинградской области, дата выдачи документа 16.07.2025')",
    "reg_address": "полный адрес регистрации со штампа (например, 'г. Санкт-Петербург, ул. Верхне-Каменская, дом 5, стр. 1, кв. 1163')",
    "inn": "номер ИНН (12 цифр, например, 781462655959)",
    "snils": "номер СНИЛС (например, 226-813-876 84)",
    "bik": "БИК банка курьера",
    "rs": "расчетный счет курьера 20 цифр",
    "ks": "корреспондентский счет банка 20 цифр"
}

Внимание:
1. Если какого-то поля на фото нет, оставь поле пустой строкой "".
2. Не придумывай данные, бери строго то, что видно на фотографиях.
3. Верни ТОЛЬКО чистый JSON, без markdown-кавычек и пояснений.
"""

def extract_data_from_images(image_paths: List[str], api_key: str = None) -> Dict[str, Any]:
    api_key = api_key or os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise ValueError("Не указан OPENAI_API_KEY в переменных Bothost.")

    api_key = api_key.strip().strip('"').strip("'")
    if api_key.lower().startswith("bearer "):
        api_key = api_key[7:].strip()

    base_url = os.getenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    if api_key.startswith("sk-or-"):
        base_url = "https://openrouter.ai/api/v1"
        
    model = os.getenv("OPENAI_MODEL", "openrouter/free")
    if api_key.startswith("sk-or-") and (not model or "gpt" in model):
        model = "openrouter/free"

    from openai import OpenAI
    client = OpenAI(
        api_key=api_key,
        base_url=base_url,
        default_headers={"HTTP-Referer": "https://bothost.ru", "X-Title": "Courier Bot"}
    )
    
    content = [{"type": "text", "text": "Распознай данные документов курьера и верни структурированный JSON."}]
    for img_path in image_paths:
        try:
            with Image.open(img_path) as img:
                img.thumbnail((1400, 1400))
                from io import BytesIO
                buf = BytesIO()
                img.convert("RGB").save(buf, format="JPEG", quality=75)
                b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        except Exception:
            with open(img_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
                
        content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/jpeg;base64,{b64}",
                "detail": "auto"
            }
        })
    
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": content}
        ],
        temperature=0.0,
        max_tokens=800
    )
    
    msg = response.choices[0].message
    res_text = getattr(msg, "content", None) or getattr(msg, "reasoning", None) or ""
    
    res_text = res_text.strip()
    if res_text.startswith("```json"):
        res_text = res_text[7:]
    elif res_text.startswith("```"):
        res_text = res_text[3:]
    if res_text.endswith("```"):
        res_text = res_text[:-3]
    res_text = res_text.strip()
    
    start_idx = res_text.find("{")
    end_idx = res_text.rfind("}")
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        res_text = res_text[start_idx:end_idx + 1]
        
    return json.loads(res_text)
