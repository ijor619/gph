import json
import base64
import os
import re
import urllib.request
import urllib.error
from io import BytesIO
from typing import List, Dict, Any
from PIL import Image, ImageFile

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
    if not val: return ""
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
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=90)
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")

def extract_via_gemini(image_paths: List[str], gemini_key: str) -> Dict[str, Any]:
    """Распознавание через Google Gemini Flash с защитой от поврежденных стримов"""
    loaded_images = []
    for p in image_paths:
        try:
            loaded_images.append(safe_load_image(p))
        except Exception as e:
            print(f"Предупреждение: файл {p} пропущен из-за ошибки чтения: {e}")

    if not loaded_images:
        raise ValueError("Не удалось прочитать ни одного изображения из отправленных.")

    last_err = None

    # 1. Попытка через SDK google-genai
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=gemini_key)
        contents = [EXTRACTION_PROMPT] + loaded_images

        for model_name in ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]:
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.0
                    )
                )
                return json.loads(response.text)
            except Exception as e:
                last_err = e
                err_text = str(e).lower()
                if "404" in err_text or "not_found" in err_text:
                    continue
                raise e
    except ImportError:
        pass

    # 2. Попытка через REST API (передаем очищенный base64)
    parts = [{"text": EXTRACTION_PROMPT}]
    for p in image_paths:
        try:
            b64_str = image_to_clean_base64(p)
            parts.append({
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": b64_str
                }
            })
        except Exception:
            pass

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"temperature": 0.0, "responseMimeType": "application/json"}
    }
    encoded_payload = json.dumps(payload).encode("utf-8")

    for model_name in ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]:
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
                return json.loads(text)
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 404:
                continue
            error_body = e.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Google Gemini Error {e.code}: {error_body}")
        except Exception as e:
            last_err = e
            continue

    raise RuntimeError(f"Ошибка Gemini API: {last_err}")

def extract_via_openai(image_paths: List[str], api_key: str, base_url: str = None) -> Dict[str, Any]:
    """Распознавание через OpenAI / ProxyAPI / OpenRouter"""
    from openai import OpenAI
    client = OpenAI(api_key=api_key, base_url=base_url)
    
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
        model="gpt-4o",
        messages=[
            {"role": "system", "content": EXTRACTION_PROMPT},
            {"role": "user", "content": content}
        ],
        response_format={"type": "json_object"},
        temperature=0.0
    )
    return json.loads(response.choices[0].message.content)

def extract_data_from_images(image_paths: List[str]) -> Dict[str, Any]:
    gemini_key = clean_val(os.getenv("GEMINI_API_KEY", ""))
    if gemini_key:
        try:
            return extract_via_gemini(image_paths, gemini_key)
        except Exception as e:
            raise RuntimeError(f"{e}")

    openai_key = clean_val(os.getenv("OPENAI_API_KEY", ""))
    base_url = clean_val(os.getenv("OPENAI_BASE_URL", "")) or None
    if openai_key.startswith("http://") or openai_key.startswith("https://"):
        if not base_url: base_url = openai_key
        openai_key = ""

    if openai_key:
        try:
            return extract_via_openai(image_paths, openai_key, base_url)
        except Exception as e:
            raise RuntimeError(f"Ошибка Vision API: {e}")

    raise RuntimeError(
        "Не задан ключ распознавания GEMINI_API_KEY!\n"
        "Получите бесплатный ключ за 30 сек на https://aistudio.google.com/app/apikey"
    )
