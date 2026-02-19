"""Entry point to run the Albanian Law AI application."""

import os
import uvicorn

port = int(os.environ.get("PORT", 8000))
is_dev = os.environ.get("ENV", "production") == "development"

if __name__ == "__main__":
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=port,
        reload=is_dev,
    )
