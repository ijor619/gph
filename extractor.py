import json
import base64
import os
import re
import socket
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

def clean_api_key(val: str) -> str:
    """Удаляет лишние обертки, пробелы, кавычки и слова вроде Bearer/export"""
    if not val:
        return ""
    val = val.strip().strip('"').strip("'")
    if "export " in val.lower():
        val = re.sub(r"(?i)export\s+", "", val).strip()
    if "=" in val:
        val = val.split("=", 1)[1].strip().strip('"').strip("'")
    if val.lower().startswith("bearer "):
        val = val[7:].strip().strip('"').strip("'")
    if ":" in val and not val.startswith("http"):
        val = val.split(":", 1)[1].strip().strip('"').strip("'")
    return val

def validate_api_key(val: str, key_name: str = "OPENAI_API_KEY") -> str:
    key = clean_api_key(val)
    if "..." in key or "…" in key:
        raise RuntimeError(
            f"❌ Ключ {key_name} содержит многоточие ('...').\n\n"
            "Вы скопировали скрытый ключ из общей таблицы ProxyAPI!\n"
            "В целях безопасности ProxyAPI показывает полный ключ только один раз — в окне сразу после создания.\n\n"
            "👉 Решение: перейдите на https://proxyapi.ru, нажмите «Создать ключ» и нажмите кнопку «Скопировать» во всплывающем окне."
        )
    return key

def safe_load_image(path: str) -> Image.Image:
    """Безопасная загрузка изображения с защитой от broken data stream"""
    with Image.open(path) as img:
        img.load()
        if img.mode != "RGB":
            return img.convert("RGB")
        return img.copy()

def image_to_clean_base64(path: str) -> str:
    """Конвертирует изображение в легковесный Base64 JPEG для быстрой передачи по сети"""
    try:
        img = safe_load_image(path)
        # 1400px идеально сохраняет мелкий шрифт документов и штампов, но весит в 10 раз меньше
        max_size = 1400
        if max(img.size) > max_size:
            img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=75, optimize=True)
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
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            models = []
            for m in data.get("models", []):
                methods = m.get("supportedGenerationMethods", [])
                if "generateContent" in methods:
                    name = m.get("name", "").replace("models/", "")
                    models.append(name)
            
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
    """Распознавание через Google Gemini Flash с быстрым таймаутом (12 сек)"""
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
            with urllib.request.urlopen(req, timeout=12) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                text = result["candidates"][0]["content"]["parts"][0]["text"]
                return parse_json_from_response(text)
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8", errors="ignore")
            last_err_details = f"HTTP {e.code}: {error_body}"
            
            if "API_KEY_INVALID" in error_body or "not valid" in error_body:
                raise RuntimeError("Неверный ключ Gemini API. Проверьте правильность GEMINI_API_KEY.")
            if "location is not supported" in error_body:
                raise RuntimeError("Google блокирует доступ с IP-серверов РФ. Подключите ProxyAPI (proxyapi.ru) или OpenRouter.")
            
            if e.code == 404:
                logger.info(f"Модель {model_name} вернула 404, пробуем следующую...")
                continue
            raise RuntimeError(f"Ошибка Google Gemini: {last_err_details}")
        except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
            last_err_details = f"Таймаут подключения к Google: {e}"
            logger.warning(f"Сетевой таймаут к Google ({e}). Домен недоступен с IP хостинга.")
            break
        except Exception as e:
            last_err_details = str(e)
            continue

    raise RuntimeError(
        f"Серверы Google Gemini не отвечают с IP-адреса хостинга Bothost (РФ).\n"
        f"Детали ошибки: {last_err_details}\n\n"
        "💡 **Решение для серверов в РФ (займет 1 минуту):**\n"
        "Подключите шлюз ProxyAPI (https://proxyapi.ru) или бесплатный OpenRouter (https://openrouter.ai):\n"
        "1. Укажите OPENAI_API_KEY=ваш_ключ\n"
        "2. Укажите OPENAI_BASE_URL=https://api.proxyapi.ru/openai/v1\n"
        "После этого распознавание заработает за 4–6 секунд!"
    )

def extract_via_openai(image_paths: List[str], api_key: str, base_url: str = None) -> Dict[str, Any]:
    """Распознавание через OpenAI / ProxyAPI / OpenRouter с защитой от ошибки 402"""
    from openai import OpenAI
    
    # Автоопределение провайдера по формату ключа (sk-or-... = OpenRouter, sk-px-... = ProxyAPI)
    is_openrouter = api_key.startswith("sk-or-") or "openrouter" in (base_url or "").lower()
    if is_openrouter:
        base_url = "https://openrouter.ai/api/v1"
        default_model = "openrouter/free"
        client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers={
                "HTTP-Referer": "https://bothost.ru",
                "X-Title": "Courier Contract Bot"
            },
            timeout=35.0
        )
    else:
        # Для ProxyAPI
        if not base_url or "api.openai.com" in base_url:
            base_url = "https://api.proxyapi.ru/openai/v1"
        default_model = "gpt-4o-mini"
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=30.0)
        
    model_name = clean_val(os.getenv("OPENAI_MODEL", "")) or default_model
    if is_openrouter and (not model_name or model_name.startswith("gpt-4o")):
        # Для OpenRouter бесплатная модель openrouter/free
        model_name = "openrouter/free"
    
    content = [{"type": "text", "text": "Распознай данные документов курьера и верни структурированный JSON."}]
    for img_path in image_paths:
        try:
            b64 = image_to_clean_base64(img_path)
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{b64}",
                    "detail": "auto"
                }
            })
        except Exception:
            pass
            
    messages = [
        {"role": "system", "content": EXTRACTION_PROMPT},
        {"role": "user", "content": content}
    ]

    # ВАЖНО: max_tokens=800 обязательно! 
    # Без этого ProxyAPI резервирует максимальный лимит 4096 токенов и отклоняет запрос с 402 Insufficient balance!
    call_kwargs = {
        "model": model_name,
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": 800
    }
    
    try:
        try:
            response = client.chat.completions.create(
                response_format={"type": "json_object"},
                **call_kwargs
            )
        except Exception as sub_err:
            sub_msg = str(sub_err).lower()
            if "response_format" in sub_msg or "json" in sub_msg or "not supported" in sub_msg:
                # Если модель не поддерживает response_format (например openrouter/free)
                response = client.chat.completions.create(**call_kwargs)
            else:
                raise sub_err

        raw_content = response.choices[0].message.content
        return parse_json_from_response(raw_content)

    except Exception as e:
        err_str = str(e)
        if "402" in err_str or "Insufficient balance" in err_str:
            raise RuntimeError(
                "❌ **Ошибка 402: Недостаточно средств на балансе ProxyAPI.**\n\n"
                "💡 **Как получить 100% БЕСПЛАТНО:**\n\n"
                "👉 **Способ 1 (Самый простой — бесплатный навсегда OpenRouter):**\n"
                "1. Зайдите на https://openrouter.ai и нажмите Sign In (через Google или почту).\n"
                "2. Перейдите в раздел Keys (https://openrouter.ai/keys) и нажмите **Create Key**.\n"
                "3. В панели Bothost укажите:\n"
                "   • `OPENAI_API_KEY` = скопированный ключ `sk-or-v1-...`\n"
                "   • `OPENAI_BASE_URL` = `https://openrouter.ai/api/v1`\n"
                "   • `OPENAI_MODEL` = `openrouter/free`\n"
                "*(Баланс 0 руб, карт не нужно, всё работает бесплатно!)*\n\n"
                "👉 **Способ 2 (Активировать бесплатный баланс в ProxyAPI):**\n"
                "В личном кабинете https://proxyapi.ru привяжите Telegram через бота @proxyapi_bot. "
                "Сервис сразу начислит приветственный баланс (этого хватит на сотни документов)."
            )
        if "401" in err_str or "Invalid API Key" in err_str:
            raise RuntimeError(
                "❌ **Ошибка 401: Неверный ключ API.**\n\n"
                "Скопируйте ключ заново без лишних символов и пробелов."
            )
        raise e

def extract_data_from_images(image_paths: List[str]) -> Dict[str, Any]:
    """Главная точка входа для извлечения данных из документов"""
    openai_key = clean_api_key(os.getenv("OPENAI_API_KEY", ""))
    base_url = clean_val(os.getenv("OPENAI_BASE_URL", "")) or None
    gemini_key = clean_api_key(os.getenv("GEMINI_API_KEY", ""))
    
    if openai_key.startswith("http://") or openai_key.startswith("https://"):
        if not base_url:
            base_url = openai_key
        openai_key = ""

    # Принудительная маршрутизация по префиксу ключа
    if openai_key.startswith("sk-or-"):
        base_url = "https://openrouter.ai/api/v1"
    elif openai_key.startswith("sk-px-"):
        base_url = "https://api.proxyapi.ru/openai/v1"

    errors = []

    # 1. Приоритет №1: ProxyAPI / OpenRouter
    if openai_key:
        try:
            valid_key = validate_api_key(openai_key, "OPENAI_API_KEY")
            return extract_via_openai(image_paths, valid_key, base_url)
        except Exception as e:
            logger.warning(f"Ошибка OpenAI/Proxy: {e}")
            errors.append(f"{e}")

    # 2. Приоритет №2: Google Gemini (только если не задан OpenAI)
    if not openai_key and gemini_key and gemini_key.lower() not in ["none", "off", "disabled", "false", "0", "no", "null"]:
        try:
            valid_gem_key = validate_api_key(gemini_key, "GEMINI_API_KEY")
            return extract_via_gemini(image_paths, valid_gem_key)
        except Exception as e:
            logger.warning(f"Ошибка Gemini: {e}")
            errors.append(f"{e}")

    if errors:
        raise RuntimeError("\n\n".join(errors))

    raise RuntimeError(
        "Не задан ключ распознавания в переменных окружения!\n\n"
        "Для бесплатной работы подключите OpenRouter (https://openrouter.ai):\n"
        "• OPENAI_API_KEY = sk-or-v1-...\n"
        "• OPENAI_BASE_URL = https://openrouter.ai/api/v1\n"
        "• OPENAI_MODEL = openrouter/free"
    )
