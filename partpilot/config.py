"""設定僅從環境與專案根目錄 .env 載入。"""
from dataclasses import dataclass, field
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)


def _env(name, default=""):
    return os.environ.get(name) or default


@dataclass
class Config:
    api_key: str = field(default_factory=lambda: _env("LLM_API_KEY"), repr=False)
    base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL"))
    model: str = field(default_factory=lambda: _env("LLM_MODEL", "claude-opus-5-5"))
    fast_model: str = field(default_factory=lambda: _env("LLM_MODEL_FAST", ""))
    route: str = field(default_factory=lambda: _env("PARTPILOT_ROUTE", "off"))
    mode: str = field(default_factory=lambda: _env("LLM_MODE", "real"))
    raw_dir: str = field(default_factory=lambda: _env("PARTPILOT_RAW_DIR", str(ROOT / "data/sample")))
    clean_dir: str = field(default_factory=lambda: _env("PARTPILOT_CLEAN_DIR", str(ROOT / "data/clean")))
    state_dir: str = field(default_factory=lambda: _env("PARTPILOT_STATE_DIR", str(ROOT / "data/state")))
    init_password: str = field(default_factory=lambda: _env("PARTPILOT_INIT_PASSWORD"), repr=False)
    password: str = field(default_factory=lambda: _env("PARTPILOT_PASSWORD"), repr=False)

    def __post_init__(self):
        if self.mode not in {"real", "fake", "demo"}:
            raise ValueError("LLM_MODE 必須為 real、fake 或 demo")
        for key in ("raw_dir", "clean_dir", "state_dir"):
            path = Path(getattr(self, key)).expanduser()
            setattr(self, key, str(path if path.is_absolute() else ROOT / path))


def load_config():
    load_dotenv(ROOT / ".env", override=False)
    return Config()
