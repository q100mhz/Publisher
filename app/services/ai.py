"""Подключение к нейросетям. У каждого пользователя свой провайдер, модель и API-ключ.

Большинство сервисов (DeepSeek, OpenAI, OpenRouter, многие российские и локальные модели)
понимают один и тот же формат запросов — «OpenAI-совместимый». Для них хватает адреса API.
Claude (Anthropic) подключается через собственный SDK Anthropic.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL
from ..crypto import decrypt, mask
from ..models import User, get_setting

PROVIDERS: dict[str, dict] = {
    "deepseek": {
        "label": "DeepSeek",
        "kind": "openai",
        "base_url": DEEPSEEK_BASE_URL,
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "default_model": DEEPSEEK_MODEL,
        "key_url": "https://platform.deepseek.com/api_keys",
        "temperature": True,
    },
    "openai": {
        "label": "OpenAI (ChatGPT)",
        "kind": "openai",
        "base_url": "https://api.openai.com/v1",
        "models": [],
        "default_model": "",
        "key_url": "https://platform.openai.com/api-keys",
        "temperature": True,
    },
    "anthropic": {
        "label": "Anthropic (Claude)",
        "kind": "anthropic",
        "base_url": "",
        "models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"],
        "default_model": "claude-opus-5-5",
        "key_url": "https://platform.claude.com/settings/keys",
        "temperature": False,  # у текущих моделей Claude температура не настраивается
    },
    "openrouter": {
        "label": "OpenRouter (сотни моделей по одному ключу)",
        "kind": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "models": [],
        "default_model": "",
        "key_url": "https://openrouter.ai/keys",
        "temperature": True,
    },
    "custom": {
        "label": "Другой OpenAI-совместимый API",
        "kind": "openai",
        "base_url": "",
        "models": [],
        "default_model": "",
        "key_url": "",
        "temperature": True,
    },
}

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
    key_source: str  # «личный» / «общий ключ сервера»

    @property
    def label(self) -> str:
        return PROVIDERS[self.provider]["label"]


def user_config(db, user: User) -> AIConfig:
    """Настройки нейросети пользователя. Бросает AIError с понятным текстом, если чего-то не хватает."""
    provider = get_setting(db, user.id, "ai_provider", "deepseek")
    if provider not in PROVIDERS:
        provider = "deepseek"
    info = PROVIDERS[provider]
    model = get_setting(db, user.id, "ai_model", "") or info["default_model"]
    base_url = (get_setting(db, user.id, "ai_base_url", "") if provider == "custom" else info["base_url"]).strip()
    temperature = None
    if info["temperature"]:
        try:
            temperature = float(get_setting(db, user.id, "ai_temperature", "1.0"))
        except ValueError:
            temperature = 1.0

    encrypted = get_setting(db, user.id, "ai_api_key", "")
    api_key, key_source = "", "личный"
    if encrypted:
        api_key = decrypt(encrypted) or ""
        if not api_key:
            raise AIError("Сохранённый API-ключ не читается (на сервере сменился SECRET_KEY). "
                          "Введите ключ заново в «Настройках».")
    elif provider == "deepseek" and user.is_admin and DEEPSEEK_API_KEY:
        api_key, key_source = DEEPSEEK_API_KEY, "общий ключ сервера"

    if not api_key:
        raise AIError("Не указан API-ключ. Откройте «Настройки» → «Нейросеть» и вставьте свой ключ.")
    if not model:
        raise AIError("Не указана модель. Откройте «Настройки» → «Нейросеть» и впишите название модели.")
    if info["kind"] == "openai" and not base_url:
        raise AIError("Не указан адрес API. Откройте «Настройки» → «Нейросеть».")
    return AIConfig(provider, model, api_key, base_url, temperature, key_source)


def describe(db, user: User) -> dict:
    """Что показать на странице настроек (сам ключ в браузер не уходит, только маска)."""
    encrypted = get_setting(db, user.id, "ai_api_key", "")
    key = decrypt(encrypted) if encrypted else None
    return {
        "provider": get_setting(db, user.id, "ai_provider", "deepseek"),
        "model": get_setting(db, user.id, "ai_model", ""),
        "base_url": get_setting(db, user.id, "ai_base_url", ""),
        "temperature": get_setting(db, user.id, "ai_temperature", "1.0"),
        "key_mask": mask(key) if key else "",
        "key_broken": bool(encrypted and not key),
        "server_key": bool(user.is_admin and DEEPSEEK_API_KEY),
    }


def complete(cfg: AIConfig, system: str, prompt: str, *, quick_test: bool = False) -> str:
    """Один запрос к нейросети; возвращает текст ответа."""
    if PROVIDERS[cfg.provider]["kind"] == "anthropic":
        return _complete_anthropic(cfg, system, prompt, quick_test)
    return _complete_openai(cfg, system, prompt, quick_test)


def _complete_openai(cfg: AIConfig, system: str, prompt: str, quick_test: bool) -> str:
    import openai

    client = openai.OpenAI(api_key=cfg.api_key, base_url=cfg.base_url, timeout=60 if quick_test else 300,
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
