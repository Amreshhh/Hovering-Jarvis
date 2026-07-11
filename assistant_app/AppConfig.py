from dataclasses import dataclass
import os

from dotenv import load_dotenv, find_dotenv, set_key


class ConfigError(RuntimeError):
    pass


DEFAULT_SYSTEM_INSTRUCTION = (
    "You are a fast, minimalist assistant. Explain using layman's terms and avoid bombastic "
    "definitions. When defining words, include one example sentence. Answer in 1 or 2 sentences."
)


@dataclass(frozen=True)
class AppConfig:
    user_name: str
    wake_word: str
    groq_keys: list[str]
    gemini_keys: list[str]
    max_exchanges: int = 3
    groq_model: str = "llama-3.1-8b-instant"
    gemini_model: str = "gemini-2.5-flash"
    stt_model: str = "small.en"
    wake_threshold: float = 0.5
    tts_voice: str = "en-US-GuyNeural"
    tts_rate: int = 170
    # Whether responses open with "Hey <USER_NAME>, ..." or just answer directly.
    greet_user: bool = True
    # Preset instruction sent to the API on every call (both Groq and Gemini).
    system_instruction: str = DEFAULT_SYSTEM_INSTRUCTION
    # How long the widget stays open once unpinned (or if never pinned) before auto-closing.
    unpin_timeout_seconds: int = 5
    # "theme1" (light) or "theme2" (dark) - the theme the widget opens with. Gets
    # rewritten to .env automatically whenever the user switches themes, so it
    # always remembers the last one used, regardless of what's set here.
    initial_theme: str = "theme1"
    # If true, TTS playback starts muted every time the widget opens (still
    # unmutable from the widget itself). If false, it behaves as before.
    always_mute: bool = False


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


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in ("true", "1", "yes", "on")


def _parse_theme(value: str | None, default: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized in ("theme1", "light"):
        return "theme1"
    if normalized in ("theme2", "dark"):
        return "theme2"
    return default


def load_config() -> AppConfig:
    load_dotenv()

    user_name = os.getenv("USER_NAME", "User").strip() or "User"
    wake_word = os.getenv("WAKE_WORD", "alexa").strip().lower() or "alexa"

    groq_keys = _load_key_family("GROQ_API_KEY")
    gemini_keys = _load_key_family("GEMINI_API_KEY")

    greet_user = _parse_bool(os.getenv("GREET_USER"), True)
    system_instruction = os.getenv("SYSTEM_INSTRUCTION", "").strip() or DEFAULT_SYSTEM_INSTRUCTION
    try:
        unpin_timeout_seconds = int(os.getenv("UNPIN_TIMEOUT_SECONDS", "5"))
    except ValueError:
        unpin_timeout_seconds = 5
    initial_theme = _parse_theme(os.getenv("INITIAL_THEME"), "theme1")
    always_mute = _parse_bool(os.getenv("ALWAYS_MUTE"), False)

    return AppConfig(
        user_name=user_name,
        wake_word=wake_word,
        groq_keys=groq_keys,
        gemini_keys=gemini_keys,
        greet_user=greet_user,
        system_instruction=system_instruction,
        unpin_timeout_seconds=unpin_timeout_seconds,
        initial_theme=initial_theme,
        always_mute=always_mute,
    )


def save_theme_preference(theme: str) -> None:
    # Rewrites INITIAL_THEME in .env so the widget reopens with whichever
    # theme was last selected, instead of always resetting to the .env
    # default. Silently no-ops if there's no .env to write to (e.g. running
    # from a packaged build) - this is a nice-to-have, not a hard dependency.
    label = "light" if theme == "theme1" else "dark"
    try:
        dotenv_path = find_dotenv()
        if dotenv_path:
            set_key(dotenv_path, "INITIAL_THEME", label)
    except Exception:
        pass
