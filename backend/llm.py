from groq import AsyncGroq
from config import GROQ_API_KEY, GROQ_MODEL

client = AsyncGroq(api_key=GROQ_API_KEY)

SystemPrompt = "You are an interview coach conducting a technical mock interview. Ask one question at a time, listen to the answer, give brief feedback, then ask a natural follow-up. Keep responses under 3 sentences."


async def stream_reply(history: list[dict]):
    stream = await client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "system", "content": SystemPrompt}] + history,
        stream=True,
        reasoning_format="hidden"
    )
    async for chunk in stream:
        token = chunk.choices[0].delta.content or ""
        if token:
            yield token