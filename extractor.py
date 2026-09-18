import json
import base64
import os
import re
from typing import List, Dict, Any
from PIL import Image

EXTRACTION_SYSTEM_PROMPT = """
You are an expert document OCR assistant.
CRITICAL: Output ONLY a valid JSON object starting with { and ending with }.
NO thinking, NO reasoning, NO introductory text, NO markdown fences. Start directly with { and end with }.

Format:
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

Attention:
If a field is not visible on the photos, leave it as empty string "".
Never invent data, extract strictly what is visible.
Output MUST be raw valid JSON only.
"""

def parse_json_safely(raw_text: str) -> Dict[str, Any]:
    if not raw_text or not isinstance(raw_text, str):
        return None
        
    cleaned = re.sub(r'<think>.*?</think>', '', raw_text, flags=re.DOTALL).strip()
    
    start_idx = cleaned.find("{")
    end_idx = cleaned.rfind("}")
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        candidate = cleaned[start_idx:end_idx + 1]
    else:
        return None
        
    try:
        return json.loads(candidate)
    except Exception:
        candidate = re.sub(r',\s*([\]}])', r'\1', candidate)
        try:
            return json.loads(candidate)
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
    
    unique_paths = []
    seen_sizes = set()
    for p in image_paths:
        try:
            sz = os.path.getsize(p)
            if sz not in seen_sizes:
                seen_sizes.add(sz)
                unique_paths.append(p)
        except Exception:
            unique_paths.append(p)

    content = [{"type": "text", "text": "Extract courier document data strictly into JSON."}]
    for img_path in unique_paths:
        try:
            with Image.open(img_path) as img:
                img.thumbnail((1200, 1200))
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
            "qwen/qwen3.8-27b:free",
            "google/gemma-4-26b-a4b-it:free",
            "openrouter/free"
        ]
        if configured_model and configured_model not in models_to_try:
            models_to_try.insert(0, configured_model)
    else:
        models_to_try = [configured_model or "gpt-4o-mini"]

    last_raw_response = ""
    for model_name in models_to_try:
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=0.0,
                max_tokens=2500
            )
            msg = response.choices[0].message
            res_text = getattr(msg, "content", None) or getattr(msg, "reasoning", None) or ""
            last_raw_response = res_text
            
            parsed = parse_json_safely(res_text)
            if parsed and isinstance(parsed, dict) and (parsed.get("fio") or parsed.get("passport_str") or parsed.get("inn") or parsed.get("work_doc_full")):
                return parsed
        except Exception:
            continue

    if last_raw_response:
        raise ValueError(f"Модель ответила: {last_raw_response[:120]}")
    
    raise ValueError("Не удалось распознать документы. Попробуйте отправить фото курьера еще раз.")
