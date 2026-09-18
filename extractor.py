import json
import base64
import os
import re
from typing import List, Dict, Any
from PIL import Image

EXTRACTION_SYSTEM_PROMPT = """
You are an expert document OCR assistant for Russian civil contracts (GPH).
You are given photos of a courier's documents: passport, work permit (VNZh/patent), registration stamp, INN certificate, SNILS, and bank details.

CRITICAL INSTRUCTION:
Output ONLY a raw valid JSON object starting directly with { and ending with }.
NO thinking process, NO introductory text, NO markdown formatting.

Format:
{
    "fio": "Фамилия Имя Отчество полностью на русском языке (например, Абдунабиев Садамбек Зафарович)",
    "citizenship": "гражданство в родительном падеже (например, 'республики Таджикистан', 'республики Узбекистан', 'Российской Федерации')",
    "birth_date": "дата рождения ДД.ММ.ГГГГ (например, 19.12.1996)",
    "birth_place": "место рождения (например, Таджикистан, Узбекистан)",
    "passport_str": "документ удостоверяющий личность с серией/номером и датой выдачи (например, 'паспорт 403106091, выдан 07.07.2020')",
    "work_doc_full": "основание для работы (например, 'Вид На Жительство иностранного гражданина 83№1107116, выдан 11.04.2025' или 'Патент 78 № 1234567, выдан 01.02.2025')",
    "work_doc_table": "основание для таблицы реквизитов (например, 'Вид На Жительство иностранного гражданина: 83№1107116')",
    "stay_basis": "основание для п. 5 договора (например, 'Вида На Жительство иностранного гражданина 83№1107116')",
    "stay_issuer": "кем выдан документ пребывания для п. 5 (например, 'ГУ МВД России по г. Санкт-Петербургу и Ленинградской области, дата выдачи документа 11.04.2025')",
    "reg_address": "полный адрес регистрации со штампа (например, 'г. Санкт-Петербург, пр.Сизова дом 32, корп. 1 лит Б, кв.568')",
    "inn": "номер ИНН 12 цифр (например, 780458282597)",
    "snils": "номер СНИЛС (например, 212-101-038-64)",
    "bik": "БИК банка 9 цифр (например, 044030653)",
    "rs": "расчетный счет 20 цифр (например, 40820810755170726650)",
    "ks": "корреспондентский счет 20 цифр (начинается на 301...)"
}

Rules:
1. If a document is missing in the photos, leave its fields as empty string "".
2. Read all text, stamps, numbers carefully.
3. Start output with { and end with }.
"""

def enrich_data(data: dict) -> dict:
    if not isinstance(data, dict):
        return {}
        
    bp = (data.get('birth_place') or '').lower()
    cit = (data.get('citizenship') or '').strip()
    if not cit:
        if 'таджик' in bp:
            data['citizenship'] = 'республики Таджикистан'
        elif 'узбек' in bp:
            data['citizenship'] = 'республики Узбекистан'
        elif 'азерб' in bp:
            data['citizenship'] = 'республики Азербайджан'
        elif 'кыргыз' in bp or 'киргиз' in bp:
            data['citizenship'] = 'Кыргызской Республики'
        elif 'росси' in bp or 'рф' in bp or 'ленинград' in bp or 'москв' in bp:
            data['citizenship'] = 'Российской Федерации'
        
    wdf = (data.get('work_doc_full') or '').strip()
    if wdf:
        if not data.get('work_doc_table'):
            data['work_doc_table'] = wdf
        if not data.get('stay_basis'):
            if 'вид на жительство' in wdf.lower():
                data['stay_basis'] = 'Вида На Жительство ' + re.sub(r'(?i)вид на жительство\s*', '', wdf).strip()
            elif 'патент' in wdf.lower():
                data['stay_basis'] = 'патента ' + re.sub(r'(?i)патент\s*', '', wdf).strip()
            else:
                data['stay_basis'] = wdf
        if not data.get('stay_issuer'):
            data['stay_issuer'] = 'ГУ МВД России по г. Санкт-Петербургу и Ленинградской области'
            
    return data

def parse_json_safely(raw_text: str) -> Dict[str, Any]:
    if not raw_text or not isinstance(raw_text, str):
        return None
        
    cleaned = re.sub(r'<think>.*?</think>', '', raw_text, flags=re.DOTALL).strip()
    
    start_idx = cleaned.find("{")
    end_idx = cleaned.rfind("}")
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        candidate = cleaned[start_idx:end_idx + 1]
        try:
            res = json.loads(candidate)
            if isinstance(res, dict) and any(res.values()):
                return enrich_data(res)
        except Exception:
            candidate_clean = re.sub(r',\s*([\]}])', r'\1', candidate)
            try:
                res = json.loads(candidate_clean)
                if isinstance(res, dict) and any(res.values()):
                    return enrich_data(res)
            except Exception:
                pass

    data = {}
    fields = [
        "fio", "citizenship", "birth_date", "birth_place", "passport_str",
        "work_doc_full", "work_doc_table", "stay_basis", "stay_issuer",
        "reg_address", "inn", "snils", "bik", "rs", "ks"
    ]
    for field in fields:
        m = re.search(rf'[\"\']?{field}[\"\']?\s*[:=]\s*[\"\']([^\"\'\r\n}}]+)[\"\']', raw_text, re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            if val.lower() not in ["null", "none"]:
                data[field] = val

    if data.get("fio") or data.get("passport_str") or data.get("inn") or data.get("snils") or data.get("work_doc_full"):
        return enrich_data(data)
        
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
    
    valid_paths = [p for p in image_paths if os.path.exists(p)]
    if not valid_paths:
        raise ValueError("Нет доступных изображений для обработки.")

    content = [{"type": "text", "text": "Extract all courier document data strictly into JSON."}]
    for img_path in valid_paths:
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
            if parsed and isinstance(parsed, dict) and any(parsed.values()):
                return parsed
        except Exception:
            continue

    if last_raw_response:
        raise ValueError(f"Модель ответила: {last_raw_response[:120]}")
    
    raise ValueError("Не удалось распознать документы. Попробуйте отправить фото курьера еще раз.")
