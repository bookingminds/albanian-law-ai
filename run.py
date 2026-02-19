"""Entry point to run the Albanian Law AI application."""

import os
import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=port,
        reload=os.environ.get("ENV", "development") == "development",
    )
