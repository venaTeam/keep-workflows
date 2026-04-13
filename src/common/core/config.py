import pathlib

from starlette.config import Config

ROOT = pathlib.Path(__file__).resolve().parent.parent  # app/
BASE_DIR = ROOT.parent  # ./

try:
    starlette_config = Config(BASE_DIR / ".env")
except FileNotFoundError:
    starlette_config = Config()

# Alias for backward compatibility if needed, but we should migrate
config = starlette_config
