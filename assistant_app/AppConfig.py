from dataclasses import dataclass
import os

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class AppConfig:
    user_name: str
    wake_word: str
    groq_keys: list[str]
    gemini_keys: list[str]
    max_exchanges: int = 3
    groq_model: str = "llama-3.1-8b-instant"
    gemini_model: str = "gemini-2.5-flash"
    wake_threshold: float = 0.5
    tts_voice: str = "en-US-GuyNeural"
    tts_rate: int = 170


def _load_key_family(prefix: str) -> list[str]:
    first_key = os.getenv(f"{prefix}_1")
    if not first_key:
        raise ConfigError(f"Missing mandatory {prefix}_1 in environment.")

    keys = [first_key]
    for index in range(2, 10):
        key = os.getenv(f"{prefix}_{index}")
        if key:
            keys.append(key)
    return keys


def load_config() -> AppConfig:
    load_dotenv()

    user_name = os.getenv("USER_NAME", "User").strip() or "User"
    wake_word = os.getenv("WAKE_WORD", "alexa").strip().lower() or "alexa"

    groq_keys = _load_key_family("GROQ_API_KEY")
    gemini_keys = _load_key_family("GEMINI_API_KEY")

    return AppConfig(
        user_name=user_name,
        wake_word=wake_word,
        groq_keys=groq_keys,
        gemini_keys=gemini_keys,
    )
