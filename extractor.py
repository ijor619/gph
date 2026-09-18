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

def parse_json_safely(raw_text: str) -> Dict[str, Any]:
    if not raw_text or not isinstance(raw_text, str):
        return None
    text = raw_text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    
    start_idx = text.find("{")
    end_idx = text.rfind("}")
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        text = text[start_idx:end_idx + 1]
    else:
        return None
        
    try:
        return json.loads(text)
    except Exception:
        return None

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

    from openai import OpenAI
    client = OpenAI(
        api_key=api_key,
        base_url=base_url,
        default_headers={"HTTP-Referer": "https://bothost.ru", "X-Title": "Courier Bot"},
        timeout=45.0
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
    
    messages = [
        {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
        {"role": "user", "content": content}
    ]

    configured_model = os.getenv("OPENAI_MODEL", "").strip()
    if api_key.startswith("sk-or-"):
        models_to_try = [
            configured_model if configured_model and "gpt" not in configured_model else "google/gemma-4-26b-a4b-it:free",
            "qwen/qwen3.8-27b:free",
            "openrouter/free"
        ]
        models_to_try = [m for i, m in enumerate(models_to_try) if m and m not in models_to_try[:i]]
    else:
        models_to_try = [configured_model or "gpt-4o-mini"]

    last_raw_response = ""
    for model_name in models_to_try:
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=0.0,
                max_tokens=2000
            )
            msg = response.choices[0].message
            res_text = getattr(msg, "content", None) or getattr(msg, "reasoning", None) or ""
            last_raw_response = res_text
            
            parsed = parse_json_safely(res_text)
            if parsed and isinstance(parsed, dict) and (parsed.get("fio") or parsed.get("passport_str") or parsed.get("inn")):
                return parsed
        except Exception as e:
            err_msg = str(e)
            if "402" in err_msg or "credits" in err_msg:
                continue
            pass

    if last_raw_response:
        raise ValueError(f"Модель ответила текстом вместо JSON: {last_raw_response[:200]}")
    
    raise ValueError("Не удалось распознать документы. Попробуйте отправить фото курьера еще раз.")
