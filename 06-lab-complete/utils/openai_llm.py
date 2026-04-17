"""OpenAI LLM wrapper — dùng khi OPENAI_API_KEY được set."""
from openai import OpenAI

_client: OpenAI | None = None


def get_client(api_key: str) -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=api_key)
    return _client


def ask(question: str, history: list[dict] | None = None,
        model: str = "gpt-4o-mini", api_key: str = "") -> str:
    """
    Gọi OpenAI Chat Completions API.

    history: list of {"role": "user"/"assistant", "content": "..."}
    """
    client = get_client(api_key)

    messages = [
        {"role": "system", "content": "You are a helpful AI assistant. Keep answers concise and under 80 words."}
    ]
    if history:
        for msg in history[-10:]:  # giữ 10 messages gần nhất để tránh vượt context
            messages.append({"role": msg["role"], "content": msg["content"]})
    else:
        messages.append({"role": "user", "content": question})

    response = client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=100,
        temperature=0.7,
    )
    return response.choices[0].message.content
