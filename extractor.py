import json
import base64
import os
import re
import urllib.request
import urllib.error
import logging
from io import BytesIO
from typing import List, Dict, Any
from PIL import Image, ImageFile

logger = logging.getLogger("extractor")

# Разрешаем загрузку неполных/сжатых Telegram изображений
ImageFile.LOAD_TRUNCATED_IMAGES = True

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
    "ks": "корреспондентский счет 20 цифр (начинается на 301...)"
}
Внимание:
1. Если какого-то поля на фото нет — оставь пустую строку "".
2. Не придумывай данные, бери строго то, что видно на фотографиях.
3. Верни ТОЛЬКО JSON без каких-либо markdown-символов.
"""

def clean_val(val: str) -> str:
    if not val:
        return ""
    val = val.strip().strip('"').strip("'")
    if "=" in val:
        val = val.split("=", 1)[1].strip().strip('"').strip("'")
    return val

def safe_load_image(path: str) -> Image.Image:
    """Безопасная загрузка изображения с защитой от broken data stream"""
    with Image.open(path) as img:
        img.load()
        if img.mode != "RGB":
            return img.convert("RGB")
        return img.copy()

def image_to_clean_base64(path: str) -> str:
    """Конвертирует изображение в чистый Base64 JPEG"""
    try:
        img = safe_load_image(path)
        # Масштабируем гигантские фото, чтобы не превышать лимиты API
        max_size = 2048
        if max(img.size) > max_size:
            img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")

def parse_json_from_response(raw_text: str) -> Dict[str, Any]:
    """Надежный парсер JSON из ответа нейросети с очисткой markdown-тегов"""
    text = raw_text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    
    # Поиск первого { и последнего }
    start_idx = text.find("{")
    end_idx = text.rfind("}")
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        text = text[start_idx:end_idx + 1]
        
    return json.loads(text)

def get_available_gemini_models(gemini_key: str) -> List[str]:
    """Динамический опрос списка доступных моделей у Google для конкретного API-ключа"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={gemini_key}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            models = []
            for m in data.get("models", []):
                methods = m.get("supportedGenerationMethods", [])
                if "generateContent" in methods:
                    name = m.get("name", "").replace("models/", "")
                    models.append(name)
            
            # Приоритет отдаем быстрым flash-моделям
            flash_models = [m for m in models if "flash" in m.lower()]
            other_models = [m for m in models if "flash" not in m.lower()]
            found = flash_models + other_models
            if found:
                logger.info(f"Обнаружены доступные модели Gemini: {found[:4]}")
                return found
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="ignore")
        if "API_KEY_INVALID" in error_body or "not valid" in error_body:
            raise RuntimeError("Неверный ключ Gemini API. Проверьте GEMINI_API_KEY в переменных Bothost.")
        if "location is not supported" in error_body:
            raise RuntimeError("Google блокирует доступ с IP-адресов РФ. Подключите ProxyAPI (proxyapi.ru) или OpenRouter.")
    except Exception as e:
        logger.warning(f"Не удалось получить список моделей Gemini: {e}")

    # Fallback список с актуальными моделями (включая вечный алиас gemini-flash-latest)
    return [
        "gemini-flash-latest",
        "gemini-2.5-flash",
        "gemini-2.5-flash-preview",
        "gemini-3.8-flash",
        "gemini-2.0-flash-exp",
        "gemini-1.5-flash-latest",
        "gemini-flash-lite-latest"
    ]

def extract_via_gemini(image_paths: List[str], gemini_key: str) -> Dict[str, Any]:
    """Распознавание через Google Gemini Flash с автоопределением актуальной модели"""
    # 1. Подготовка изображений в base64
    parts = [{"text": EXTRACTION_PROMPT}]
    valid_images_count = 0
    for p in image_paths:
        try:
            b64_str = image_to_clean_base64(p)
            parts.append({
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": b64_str
                }
            })
            valid_images_count += 1
        except Exception as e:
            logger.warning(f"Файл {p} пропущен: {e}")

    if valid_images_count == 0:
        raise ValueError("Не удалось прочитать ни одного изображения из отправленных.")

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "temperature": 0.0,
            "responseMimeType": "application/json"
        }
    }
    encoded_payload = json.dumps(payload).encode("utf-8")

    # 2. Получаем актуальный список моделей для ключа
    candidate_models = get_available_gemini_models(gemini_key)
    last_err_details = None

    for model_name in candidate_models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={gemini_key}"
        req = urllib.request.Request(
            url,
            data=encoded_payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                text = result["candidates"][0]["content"]["parts"][0]["text"]
                return parse_json_from_response(text)
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8", errors="ignore")
            last_err_details = f"HTTP {e.code}: {error_body}"
            
            # Если неверный ключ или блокировка по РФ — сообщаем сразу
            if "API_KEY_INVALID" in error_body or "not valid" in error_body:
                raise RuntimeError("Неверный ключ Gemini API. Проверьте правильность GEMINI_API_KEY.")
            if "location is not supported" in error_body:
                raise RuntimeError("Google блокирует доступ с IP-серверов РФ. Подключите ProxyAPI (proxyapi.ru) или OpenRouter.")
            
            # Если 404 (модель выведена из эксплуатации или переименована) — пробуем следующую модель
            if e.code == 404:
                logger.info(f"Модель {model_name} вернула 404, переключаемся на следующую...")
                continue
            raise RuntimeError(f"Ошибка Google Gemini: {last_err_details}")
        except Exception as e:
            last_err_details = str(e)
            continue

    # 3. Дополнительная попытка через OpenAI-совместимый эндпоинт Google
    try:
        openai_url = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        openai_content = [{"type": "text", "text": EXTRACTION_PROMPT}]
        for p in image_paths:
            try:
                b64 = image_to_clean_base64(p)
                openai_content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"}
                })
            except Exception:
                pass
        
        oa_payload = json.dumps({
            "model": "gemini-flash-latest",
            "messages": [{"role": "user", "content": openai_content}],
            "temperature": 0.0
        }).encode("utf-8")
        
        oa_req = urllib.request.Request(
            openai_url,
            data=oa_payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {gemini_key}"
            },
            method="POST"
        )
        with urllib.request.urlopen(oa_req, timeout=45) as resp:
            oa_res = json.loads(resp.read().decode("utf-8"))
            oa_text = oa_res["choices"][0]["message"]["content"]
            return parse_json_from_response(oa_text)
    except Exception as e:
        logger.warning(f"Google OpenAI-compatible endpoint error: {e}")

    raise RuntimeError(
        f"Google Gemini не ответил ни на одну модель.\nДетали: {last_err_details}\n\n"
        "Рекомендация: если сервер Bothost находится в РФ, Google может блокировать запросы. "
        "Используйте ProxyAPI (proxyapi.ru) или OpenRouter."
    )

def extract_via_openai(image_paths: List[str], api_key: str, base_url: str = None) -> Dict[str, Any]:
    """Распознавание через OpenAI / ProxyAPI / OpenRouter"""
    from openai import OpenAI
    client = OpenAI(api_key=api_key, base_url=base_url)
    
    # Модель по умолчанию gpt-4o-mini или gpt-4o
    model_name = clean_val(os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
    
    content = [{"type": "text", "text": "Распознай данные документов курьера и верни структурированный JSON."}]
    for img_path in image_paths:
        try:
            b64 = image_to_clean_base64(img_path)
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{b64}",
                    "detail": "high"
                }
            })
        except Exception:
            pass
    
    response = client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": EXTRACTION_PROMPT},
            {"role": "user", "content": content}
        ],
        response_format={"type": "json_object"},
        temperature=0.0
    )
    raw_content = response.choices[0].message.content
    return parse_json_from_response(raw_content)

def extract_data_from_images(image_paths: List[str]) -> Dict[str, Any]:
    """Главная точка входа для извлечения данных из документов"""
    gemini_key = clean_val(os.getenv("GEMINI_API_KEY", ""))
    openai_key = clean_val(os.getenv("OPENAI_API_KEY", ""))
    base_url = clean_val(os.getenv("OPENAI_BASE_URL", "")) or None
    
    errors = []
    
    if gemini_key:
        try:
            return extract_via_gemini(image_paths, gemini_key)
        except Exception as e:
            logger.warning(f"Ошибка Gemini: {e}")
            errors.append(f"Gemini: {e}")

    if openai_key.startswith("http://") or openai_key.startswith("https://"):
        if not base_url:
            base_url = openai_key
        openai_key = ""

    if openai_key:
        try:
            return extract_via_openai(image_paths, openai_key, base_url)
        except Exception as e:
            logger.warning(f"Ошибка OpenAI/Proxy: {e}")
            errors.append(f"OpenAI/Proxy: {e}")

    if errors:
        raise RuntimeError("\n".join(errors))

    raise RuntimeError(
        "Не задан ключ распознавания в переменных окружения!\n\n"
        "1. Для Gemini: добавьте GEMINI_API_KEY (бесплатный на https://aistudio.google.com)\n"
        "2. Для ProxyAPI (для РФ): добавьте OPENAI_API_KEY и OPENAI_BASE_URL=https://api.proxyapi.ru/openai/v1\n"
        "3. Для OpenRouter: добавьте OPENAI_API_KEY и OPENAI_BASE_URL=https://openrouter.ai/api/v1"
    )
