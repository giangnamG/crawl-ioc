import os
from pathlib import Path
from ioc_app.app import create_app


BASE_DIR = Path(__file__).resolve().parent


def load_dotenv(
    path: str | os.PathLike | None = None,
    *,
    override: bool = False,
    protected_keys: set[str] | None = None,
) -> None:
    env_path = Path(path) if path else Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    protected_keys = protected_keys or set()
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        if key in os.environ and (not override or key in protected_keys):
            continue
        value = value.strip().strip("\"'")
        os.environ[key] = value


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def app_environment() -> str:
    return (os.environ.get("APP_ENV") or os.environ.get("FLASK_ENV") or "development").strip().lower()


def app_debug_enabled() -> bool:
    return env_bool("FLASK_DEBUG", app_environment() != "production")


original_env_keys = set(os.environ)
load_dotenv(BASE_DIR / ".env")
load_dotenv(BASE_DIR / ".env.local", override=True, protected_keys=original_env_keys)
os.environ.setdefault("FLASK_SKIP_DOTENV", "1")
app = create_app(start_worker=env_bool("AUTO_WORKER_ENABLED", True))


if __name__ == "__main__":
    debug = app_debug_enabled()
    app.run(host="0.0.0.0", port=5000, debug=debug, use_reloader=debug)
