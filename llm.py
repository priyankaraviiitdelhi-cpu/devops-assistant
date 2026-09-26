"""
llm.py - the AI "brain" of the assistant.
All LLM calls go through here, so switching providers later
(Gemini -> Claude -> OpenAI) only means editing this one file and .env.
"""
import os
from dotenv import load_dotenv

load_dotenv()  # reads the values from .env

PROVIDER = os.getenv("LLM_PROVIDER", "gemini")
MODEL = os.getenv("LLM_MODEL", "gemini-2.5-flash")

SYSTEM_PROMPT = """You are an expert DevOps and AWS cloud engineer assistant.
You help with AWS services, Terraform, Docker, CI/CD, Linux and cloud cost optimization.
Give clear, practical answers. When you show code or commands, use code blocks.
Always mention cost or security risks when they are relevant."""


def chat(messages):
    """
    Send the conversation to the LLM and return its reply as text.
    messages = [{"role": "user" or "assistant", "content": "..."}]
    """
    if PROVIDER == "gemini":
        return _chat_gemini(messages)
    raise ValueError(f"Unknown LLM_PROVIDER: {PROVIDER}")


def _chat_gemini(messages):
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

    response = client.models.generate_content(
        model=MODEL,
        contents=contents,
        config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
    )
    return response.text