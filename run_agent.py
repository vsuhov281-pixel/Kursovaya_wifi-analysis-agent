"""Ручной запуск одного цикла агента (без веб-сервера).

    python run_agent.py
"""
from agent.agent import Agent
from agent.db import DB
from agent import config

if __name__ == "__main__":
    summary = Agent(DB(config.DB_PATH)).run()
    print("\nИтог:", summary)
