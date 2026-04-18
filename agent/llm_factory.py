"""
llm_factory.py
Returns the correct LangChain chat model based on LLM_PROVIDER env var.
"""

import os
import warnings

warnings.filterwarnings("ignore", category=UserWarning)

from dotenv import load_dotenv

load_dotenv()


def get_llm(temperature: float = 0.1):
    provider = os.getenv("LLM_PROVIDER", "ollama").lower()

    if provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            temperature=temperature,
        )

    # Default: Ollama
    from langchain_ollama import ChatOllama
    return ChatOllama(
        model=os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b"), # Ollama model name
        base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        temperature=temperature,
        num_predict=4096,   # increased from 1024 — deepseek-r1 needs more for reasoning
        num_ctx=8192,       # context window
    )