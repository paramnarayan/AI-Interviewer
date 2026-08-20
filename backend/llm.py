from fastapi import responses
from groq import AsyncGroq
from config import GROQ_API_KEY, GROQ_MODEL
import json
client = AsyncGroq(api_key=GROQ_API_KEY)



def SystemPrompt(profile:dict)->str:
    name=profile.get("name")
    role=profile.get("role")
    company=profile.get("company")
    stack=", ".join(profile.get("tech_stack", [])) or "general software engineering"
    company_line= f" at {company}" if company else" " 
    return f"""You are an interview coach conducting a mock technical interview with {name}, 
who is preparing for a {role} position{company_line}. Their relevant tech stack: {stack}.

Ask one question at a time, tailored to their target role and stack. Reference their 
previous answers when relevant — build follow-up questions naturally from what they've 
already said rather than asking generic, disconnected questions. Give brief feedback after 
each answer, then ask a natural follow-up. Keep responses under 3 sentences."""


async def stream_reply(history: list[dict],profile:dict):
    systemprompt= SystemPrompt(profile)
    stream = await client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "system", "content":systemprompt}] + history,
        stream=True,
        reasoning_format="hidden"
    )
    async for chunk in stream:
        token = chunk.choices[0].delta.content or ""
        if token:
            yield token


async def generate_summary(session)->dict:
    transcript_text= ""

    summary_prompt= f"""Based on this mock interview transcript for a {session.profile.get('role', 'technical')} position, provide a structured critique. Respond ONLY in this JSON format:

{{
  "overall_impression": "2-3 sentence summary",
  "strengths": ["specific strength 1", "specific strength 2", ...],
  "areas_to_improve": ["specific area 1 with example from transcript", ...],
  "communication_notes": "notes on clarity, structure (e.g. STAR method usage), pacing",
  "suggested_next_steps": ["actionable prep suggestion 1", ...]
}}
Transcript:
{transcript_text}"""

    response=await client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role":"user","content":summary_prompt}]
    )   
    raw=response.choices[0].message.content
    return json.loads(raw)
