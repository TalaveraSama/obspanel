from pathlib import Path
import uvicorn

# Import the backend normally so every FastAPI route is registered before
# the server starts. This replaces the old exec/compatibility launcher.
from app_backend import app, CFG

if __name__ == "__main__":
    uvicorn.run(app, host=CFG["host"], port=CFG["port"], log_level="info")
