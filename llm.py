"""
llm.py - the AI "brain" of the assistant.
All LLM calls go through here, so switching providers later
(Gemini -> Claude -> OpenAI) only means editing this one file and .env.
"""
import os
from dotenv import load_dotenv

load_dotenv()  # reads the values from .env

PROVIDER = os.getenv("LLM_PROVIDER", "gemini")
MODEL = os.getenv("LLM_MODEL", "gemini-3.8-flash")

SYSTEM_PROMPT = """You are an expert DevOps and AWS cloud engineer assistant.
You help with AWS services, Terraform, Docker, CI/CD, Linux and cloud cost optimization.
Give clear, practical answers. When you show code or commands, use code blocks.
Always mention cost or security risks when they are relevant."""

RAG_INSTRUCTIONS = """

You also have excerpts from the team's knowledge base below.
- If they are relevant, base your answer on them and name the source file, e.g. (source: team_policies.md).
- Team policies in the knowledge base override general best practices.
- If the excerpts are not relevant, ignore them and answer normally.

KNOWLEDGE BASE EXCERPTS:
"""


def chat(messages, context=None, system=None):
    """
    Send the conversation to the LLM and return its reply as text.
    messages = [{"role": "user" or "assistant", "content": "..."}]
    context  = text retrieved from the knowledge base (optional)
    """
    system = system or SYSTEM_PROMPT
    if context:
        system += RAG_INSTRUCTIONS + context

    if PROVIDER == "gemini":
        return _chat_gemini(messages, system)
    raise ValueError(f"Unknown LLM_PROVIDER: {PROVIDER}")


def _chat_gemini(messages, system):
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

    # Gemini calls the assistant role "model"
    contents = [
        types.Content(
            role="user" if m["role"] == "user" else "model",
            parts=[types.Part(text=m["content"])],
        )
        for m in messages
    ]

    # Retry a few times if Google's servers are busy (503) or rate-limited (429)
    import time
    for attempt in range(4):
        try:
            response = client.models.generate_content(
                model=MODEL,
                contents=contents,
                config=types.GenerateContentConfig(system_instruction=system),
            )
            return response.text
        except Exception as e:
            busy = "503" in str(e) or "429" in str(e)
            if not busy or attempt == 3:
                raise
            time.sleep(2 ** attempt * 3)  # wait 3s, 6s, 12s