"""Start the bot + dashboard: python run.py  ->  http://127.0.0.1:8000"""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run("bot.server:app", host="127.0.0.1", port=int(os.getenv("PORT", "8000")))
