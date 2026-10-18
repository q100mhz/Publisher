"""Подключение к нейросетям. У каждого пользователя свой провайдер, модель и API-ключ.

Большинство сервисов (DeepSeek, Qwen, Gemini, OpenAI, OpenRouter, локальные модели) понимают
один и тот же формат запросов — «OpenAI-совместимый». Для них хватает адреса API.
Claude (Anthropic) подключается через собственный SDK Anthropic, картинки Qwen — через API Qwen.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL
from ..crypto import decrypt, mask
from ..models import AIUsage, User, get_setting, now
from ..security import UnsafeURL, check_url

QWEN_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

# images — как сервис рисует картинки по тому же ключу: None — не умеет,
# "openai" — формат OpenAI Images, "gemini" — то же с ответом в base64, "qwen" — собственный API Qwen.
# editable_base — пользователь может (или должен, если base_url пуст) указать свой адрес API.
PROVIDERS: dict[str, dict] = {
    "deepseek": {
        "label": "DeepSeek", "kind": "openai", "base_url": DEEPSEEK_BASE_URL, "editable_base": False,
        "models": ["deepseek-chat", "deepseek-reasoner"], "default_model": DEEPSEEK_MODEL,
        "key_url": "https://platform.deepseek.com/api_keys", "temperature": True,
        "images": None, "image_models": [],
    },
    "qwen": {
        "label": "Qwen (Alibaba Cloud Model Studio)", "kind": "openai", "base_url": QWEN_BASE_URL,
        "editable_base": True,
        "models": ["qwen3.8-max", "qwen-plus", "qwen-max", "qwen-turbo"], "default_model": "qwen-plus",
        "key_url": "https://www.alibabacloud.com/help/en/model-studio/get-api-key", "temperature": True,
        "images": "qwen", "image_models": ["qwen-image-2.0-pro", "qwen-image-3.0-pro"],
    },
    "gemini": {
        "label": "Google Gemini", "kind": "openai", "base_url": GEMINI_BASE_URL, "editable_base": False,
        "models": ["gemini-3.8-flash"], "default_model": "gemini-3.8-flash",
        "key_url": "https://aistudio.google.com/app/apikey", "temperature": True,
        "images": "gemini", "image_models": ["gemini-2.5-flash-image", "gemini-3-pro-image-preview"],
    },
    "openai": {
        "label": "OpenAI (ChatGPT)", "kind": "openai", "base_url": "https://api.openai.com/v1",
        "editable_base": False, "models": [], "default_model": "",
        "key_url": "https://platform.openai.com/api-keys", "temperature": True,
        "images": "openai", "image_models": ["gpt-image-1", "dall-e-3"],
    },
    "anthropic": {
        "label": "Anthropic (Claude)", "kind": "anthropic", "base_url": "", "editable_base": False,
        "models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"], "default_model": "claude-opus-5-5",
        "key_url": "https://platform.claude.com/settings/keys",
        "temperature": False,  # у текущих моделей Claude температура не настраивается
        "images": None, "image_models": [],
    },
    "openrouter": {
        "label": "OpenRouter (сотни моделей по одному ключу)", "kind": "openai",
        "base_url": "https://openrouter.ai/api/v1", "editable_base": False, "models": [], "default_model": "",
        "key_url": "https://openrouter.ai/keys", "temperature": True,
        "images": None, "image_models": [],
    },
    "custom": {
        "label": "Другой OpenAI-совместимый API", "kind": "openai", "base_url": "", "editable_base": True,
        "models": [], "default_model": "", "key_url": "", "temperature": True,
        "images": "openai", "image_models": [],
    },
}


def _base_url(provider: str, custom: str) -> str:
    info = PROVIDERS[provider]
    return (custom.strip() if info["editable_base"] and custom.strip() else info["base_url"]).strip()


# Модели Claude, для которых Anthropic умеет сама переотправить запрос другой модели,
# если основная откажется отвечать (защитные фильтры могут сработать и на безобидный текст)
CLAUDE_FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5", "claude-fable-5-1"}


class AIError(Exception):
    pass


@dataclass
class AIConfig:
    provider: str
    model: str
    api_key: str
    base_url: str
    temperature: float | None
    via_base: bool = False  # запрос идёт через базовый ключ сервиса

    @property
    def label(self) -> str:
        return PROVIDERS[self.provider]["label"]


def _own_key(db, user_id: int, prefix: str, provider: str) -> str:
    """Свой ключ пользователя для конкретного сервиса ("" — не указан)."""
    encrypted = get_setting(db, user_id, f"{prefix}:{provider}", "")
    if not encrypted:
        return ""
    key = decrypt(encrypted)
    if not key:
        raise AIError("Сохранённый API-ключ не читается (на сервере сменился SECRET_KEY). "
                      "Введите ключ заново в «Настройках».")
    return key


def base_usage_this_month(db, user_id: int) -> int:
    from sqlalchemy import func, select

    month_start = now().replace(day=1, hour=0, minute=0, second=0)
    return db.scalar(select(func.count(AIUsage.id)).where(
        AIUsage.user_id == user_id, AIUsage.via_base.is_(True), AIUsage.ok.is_(True),
        AIUsage.kind != "test", AIUsage.created_at >= month_start)) or 0


def base_available(user: User) -> bool:
    return bool(user.base_ai_allowed and DEEPSEEK_API_KEY)


def user_config(db, user: User) -> AIConfig:
    """Настройки нейросети пользователя. Бросает AIError с понятным текстом, если чего-то не хватает.

    Порядок выбора ключа: свой ключ выбранного сервиса → (только для DeepSeek) базовый ключ сервиса,
    если администратор разрешил его этому пользователю и месячный лимит не исчерпан.
    """
    provider = get_setting(db, user.id, "ai_provider", "deepseek")
    if provider not in PROVIDERS:
        provider = "deepseek"
    info = PROVIDERS[provider]
    model = get_setting(db, user.id, "ai_model", "") or info["default_model"]
    base_url = _base_url(provider, get_setting(db, user.id, "ai_base_url", ""))
    temperature = None
    if info["temperature"]:
        try:
            temperature = float(get_setting(db, user.id, "ai_temperature", "1.0"))
        except ValueError:
            temperature = 1.0

    api_key, via_base = _own_key(db, user.id, "ai_api_key", provider), False
    if not api_key and provider == "deepseek" and base_available(user):
        if user.ai_monthly_limit is not None and base_usage_this_month(db, user.id) >= user.ai_monthly_limit:
            raise AIError(f"Лимит генераций через базовый ключ сервиса на этот месяц исчерпан "
                          f"({user.ai_monthly_limit}). Добавьте свой ключ в «Настройках» или обратитесь к администратору.")
        api_key, via_base, base_url = DEEPSEEK_API_KEY, True, DEEPSEEK_BASE_URL
        model = model or DEEPSEEK_MODEL

    if not api_key:
        if provider == "deepseek":
            raise AIError("Не указан ключ DeepSeek. Вставьте свой ключ в «Настройках» → «Нейросеть» "
                          "или попросите администратора разрешить базовый ключ сервиса.")
        raise AIError(f"Не указан API-ключ для {info['label']}. Откройте «Настройки» → «Нейросеть» и вставьте ключ.")
    if not model:
        raise AIError("Не указана модель. Откройте «Настройки» → «Нейросеть» и впишите название модели.")
    if info["kind"] == "openai" and not base_url:
        raise AIError("Не указан адрес API. Откройте «Настройки» → «Нейросеть».")
    return AIConfig(provider, model, api_key, base_url, temperature, via_base)


def describe(db, user: User) -> dict:
    """Что показать на странице настроек (ключи в браузер не уходят, только маски)."""
    masks, broken = {}, False
    for provider in PROVIDERS:
        encrypted = get_setting(db, user.id, f"ai_api_key:{provider}", "")
        if encrypted:
            key = decrypt(encrypted)
            masks[provider] = mask(key) if key else "не читается — введите заново"
            broken = broken or not key
    return {
        "provider": get_setting(db, user.id, "ai_provider", "deepseek"),
        "model": get_setting(db, user.id, "ai_model", ""),
        "base_url": get_setting(db, user.id, "ai_base_url", ""),
        "temperature": get_setting(db, user.id, "ai_temperature", "1.0"),
        "key_masks": masks,
        "key_broken": broken,
        "base_allowed": base_available(user),
        "base_limit": user.ai_monthly_limit,
        "base_used": base_usage_this_month(db, user.id) if base_available(user) else 0,
    }


def log_usage(user_id: int, kind: str, provider: str, model: str, via_base: bool, ok: bool, error: str = "") -> None:
    """Записывает обращение к нейросети в учёт (дашборд админки и лимиты)."""
    from ..db import session_scope

    with session_scope() as db:
        db.add(AIUsage(user_id=user_id, kind=kind, provider=provider, model=model[:100], via_base=via_base,
                       ok=ok, error=error[:500]))


# ---------- Картинки ----------

IMAGE_PROVIDERS = {
    "": "Не указан — использовать основную нейросеть",
    "openai": "OpenAI (картинки)",
    "gemini": "Google Gemini (картинки)",
    "qwen": "Qwen (картинки)",
    "custom": "Другой API в формате OpenAI Images",
}
IMAGE_MODEL_HINTS = {key: PROVIDERS[key]["image_models"] for key in IMAGE_PROVIDERS if key}
IMAGE_SIZES = ["1024x1024", "1536x1024", "1024x1536", "1792x1024", "1024x1792"]


@dataclass
class ImageConfig:
    label: str
    kind: str      # openai / gemini / qwen — формат запроса
    model: str
    api_key: str
    base_url: str
    size: str
    separate: bool  # True — отдельный API для картинок, False — тот же, что для текста


def image_config(db, user: User) -> ImageConfig:
    """Нейросеть для картинок.

    Если отдельный API для картинок не заполнен, запрос на картинку уходит в основную нейросеть
    (тот же ключ) — как дополнительный промпт. Это работает, только если основной сервис умеет рисовать.
    """
    provider = get_setting(db, user.id, "img_provider", "")
    key = _own_key(db, user.id, "img_api_key", provider) if provider else ""
    model = get_setting(db, user.id, "img_model", "").strip()
    size = get_setting(db, user.id, "img_size", "") or IMAGE_SIZES[0]

    if provider in PROVIDERS and key:
        base_url = _base_url(provider, get_setting(db, user.id, "img_base_url", ""))
        if not base_url:
            raise AIError("Не указан адрес API для картинок. Откройте «Настройки» → «Картинки».")
        kind, label, separate = PROVIDERS[provider]["images"], IMAGE_PROVIDERS[provider], True
    else:
        main = user_config(db, user)
        kind = PROVIDERS[main.provider]["images"]
        if not kind:
            raise AIError(f"Отдельный API для картинок не указан, а основная нейросеть ({main.label}) "
                          "картинки создавать не умеет. Откройте «Настройки» → «Картинки» и укажите сервис и ключ.")
        key, base_url, label, separate = main.api_key, main.base_url, main.label, False
        model = model or (PROVIDERS[main.provider]["image_models"] or [""])[0]
    if not model:
        raise AIError("Не указана модель для картинок. Откройте «Настройки» → «Картинки».")
    return ImageConfig(label, kind, model, key, base_url, size, separate)


def describe_images(db, user: User) -> dict:
    masks = {}
    for provider in IMAGE_PROVIDERS:
        encrypted = get_setting(db, user.id, f"img_api_key:{provider}", "") if provider else ""
        if encrypted:
            key = decrypt(encrypted)
            masks[provider] = mask(key) if key else "не читается — введите заново"
    return {
        "provider": get_setting(db, user.id, "img_provider", ""),
        "model": get_setting(db, user.id, "img_model", ""),
        "base_url": get_setting(db, user.id, "img_base_url", ""),
        "size": get_setting(db, user.id, "img_size", "") or IMAGE_SIZES[0],
        "key_masks": masks,
    }


def _safe(url: str, what: str) -> str:
    """Адрес сервиса не должен вести во внутреннюю сеть сервера."""
    try:
        return check_url(url)
    except UnsafeURL as e:
        raise AIError(f"{what}: {e}")


def _download(url: str) -> bytes:
    import httpx

    try:
        for _ in range(6):  # перенаправления — вручную, с проверкой каждого адреса
            _safe(url, "Ссылка на картинку")
            result = httpx.get(url, timeout=60, follow_redirects=False)
            if result.is_redirect and result.headers.get("location"):
                url = str(result.url.join(result.headers["location"]))
                continue
            result.raise_for_status()
            if len(result.content) > 30 * 1024 * 1024:
                raise AIError("Картинка от нейросети слишком большая (больше 30 МБ)")
            return result.content
        raise AIError("Слишком много перенаправлений при скачивании картинки")
    except httpx.HTTPError as e:
        raise AIError(f"Картинка создана, но скачать её не удалось: {e}")


def generate_image(cfg: ImageConfig, prompt: str) -> bytes:
    """Создаёт одну картинку и возвращает её байты."""
    if cfg.kind == "qwen":
        return _generate_image_qwen(cfg, prompt)
    import base64

    import openai

    client = openai.OpenAI(api_key=cfg.api_key, base_url=_safe(cfg.base_url, "Адрес API картинок"),
                           timeout=240, max_retries=1)
    params = {"model": cfg.model, "prompt": prompt, "size": cfg.size, "n": 1}
    if cfg.kind == "gemini":
        params["response_format"] = "b64_json"  # Gemini отдаёт картинку прямо в ответе
    try:
        response = client.images.generate(**params)
    except openai.AuthenticationError:
        raise AIError("Ключ API для картинок не подошёл. Проверьте его в «Настройках».")
    except openai.NotFoundError:
        raise AIError(f"Модель «{cfg.model}» или адрес для картинок не найдены у сервиса {cfg.label}.")
    except openai.RateLimitError:
        raise AIError("Превышен лимит запросов или закончились деньги на балансе сервиса картинок.")
    except openai.APIConnectionError as e:
        raise AIError(f"Не удалось связаться с {cfg.base_url}: {e}")
    except openai.APIStatusError as e:
        raise AIError(f"Сервис картинок вернул ошибку {e.status_code}: {e.message}")
    if not response.data:
        raise AIError("Сервис картинок вернул пустой ответ")
    item = response.data[0]
    if getattr(item, "b64_json", None):
        return base64.b64decode(item.b64_json)
    if getattr(item, "url", None):
        return _download(item.url)
    raise AIError("Сервис картинок не вернул ни картинку, ни ссылку на неё")


def _generate_image_qwen(cfg: ImageConfig, prompt: str) -> bytes:
    """Qwen рисует через собственный API Model Studio (синхронный вызов multimodal-generation)."""
    import httpx

    root = cfg.base_url.rstrip("/")
    root = root[: -len("/compatible-mode/v1")] if root.endswith("/compatible-mode/v1") else root
    url = _safe(f"{root}/api/v1/services/aigc/multimodal-generation/generation", "Адрес API Qwen")
    body = {
        "model": cfg.model,
        "input": {"messages": [{"role": "user", "content": [{"text": prompt}]}]},
        "parameters": {"watermark": False, "size": cfg.size.replace("x", "*")},
    }
    try:
        response = httpx.post(url, json=body, timeout=240, headers={"Authorization": f"Bearer {cfg.api_key}"})
    except httpx.HTTPError as e:
        raise AIError(f"Не удалось связаться с {root}: {e}")
    try:
        data = response.json()
    except ValueError:
        raise AIError(f"Qwen вернул непонятный ответ (HTTP {response.status_code})")
    if response.status_code >= 400 or data.get("code"):
        if response.status_code == 401:
            raise AIError("Ключ API Qwen не подошёл. Проверьте его в «Настройках».")
        raise AIError(f"Qwen вернул ошибку {data.get('code') or response.status_code}: {data.get('message', '')}")
    try:
        image_url = data["output"]["choices"][0]["message"]["content"][0]["image"]
    except (KeyError, IndexError, TypeError):
        raise AIError("Qwen не вернул ссылку на картинку")
    return _download(image_url)


def complete(cfg: AIConfig, system: str, prompt: str, *, quick_test: bool = False) -> str:
    """Один запрос к нейросети; возвращает текст ответа."""
    if PROVIDERS[cfg.provider]["kind"] == "anthropic":
        return _complete_anthropic(cfg, system, prompt, quick_test)
    return _complete_openai(cfg, system, prompt, quick_test)


def _complete_openai(cfg: AIConfig, system: str, prompt: str, quick_test: bool) -> str:
    import openai

    client = openai.OpenAI(api_key=cfg.api_key, base_url=_safe(cfg.base_url, "Адрес API нейросети"),
                           timeout=60 if quick_test else 300,
                           max_retries=1)
    params = {"model": cfg.model, "messages": [{"role": "system", "content": system},
                                                {"role": "user", "content": prompt}]}
    if cfg.temperature is not None:
        params["temperature"] = cfg.temperature
    if quick_test:
        params["max_tokens"] = 20
    try:
        response = client.chat.completions.create(**params)
    except openai.AuthenticationError:
        raise AIError("Ключ API не подошёл (ошибка авторизации). Проверьте ключ в «Настройках».")
    except openai.NotFoundError:
        raise AIError(f"Модель «{cfg.model}» не найдена у провайдера {cfg.label}. Проверьте название модели.")
    except openai.RateLimitError:
        raise AIError("Превышен лимит запросов или закончились деньги на балансе провайдера.")
    except openai.APIConnectionError as e:
        raise AIError(f"Не удалось связаться с {cfg.base_url}: {e}")
    except openai.APIStatusError as e:
        raise AIError(f"Провайдер вернул ошибку {e.status_code}: {e.message}")
    if not response.choices:
        raise AIError("Нейросеть вернула пустой ответ")
    return response.choices[0].message.content or ""


def _complete_anthropic(cfg: AIConfig, system: str, prompt: str, quick_test: bool) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=cfg.api_key, max_retries=1)
    params = {
        "model": cfg.model,
        # Модели Claude сначала «думают», и это тоже входит в лимит — оставляем запас на длинную статью
        "max_tokens": 1024 if quick_test else 32000,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }
    if quick_test:
        params["output_config"] = {"effort": "low"}
    if cfg.model in CLAUDE_FALLBACK_MODELS:
        params["betas"] = ["server-side-fallback-2026-07-01"]
        params["fallbacks"] = "default"
    try:
        # Стриминг — чтобы длинная генерация не упиралась в таймаут HTTP-запроса
        with client.beta.messages.stream(**params) as stream:
            message = stream.get_final_message()
    except anthropic.AuthenticationError:
        raise AIError("Ключ API Anthropic не подошёл. Проверьте ключ в «Настройках».")
    except anthropic.NotFoundError:
        raise AIError(f"Модель «{cfg.model}» не найдена у Anthropic. Проверьте название модели.")
    except anthropic.RateLimitError:
        raise AIError("Превышен лимит запросов Anthropic. Попробуйте позже.")
    except anthropic.APIConnectionError as e:
        raise AIError(f"Не удалось связаться с Anthropic: {e}")
    except anthropic.APIStatusError as e:
        raise AIError(f"Anthropic вернул ошибку {e.status_code}: {e.message}")

    if message.stop_reason == "refusal":
        raise AIError("Claude отказался обрабатывать этот текст (сработали защитные фильтры).")
    text = "".join(block.text for block in message.content if block.type == "text")
    if message.stop_reason == "max_tokens" and not quick_test:
        raise AIError("Ответ оказался слишком длинным и был обрезан. Сократите исходный текст или промпт.")
    return text
