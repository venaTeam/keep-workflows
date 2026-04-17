import os
import pathlib

from starlette.config import Config

# Current file: src/common/core/config.py
current_file = pathlib.Path(__file__).resolve()
# parent: src/common/core/
# parent.parent: src/common/
# parent.parent.parent: src/
# parent.parent.parent.parent: /app (root)
ROOT_DIR = current_file.parent.parent.parent.parent

env_file = ROOT_DIR / ".env"
# fallback to src/.env if root .env not found
if not env_file.exists():
    env_file = ROOT_DIR / "src" / ".env"

if env_file.exists():
    starlette_config = Config(str(env_file))
else:
    starlette_config = Config()

# Alias for backward compatibility
config = starlette_config
