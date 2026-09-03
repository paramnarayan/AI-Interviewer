import json
import logging
import re
from groq import AsyncGroq
from config import GROQ_API_KEY, GROQ_MODEL, GROQ_SUMMARY_MODEL

log = logging.getLogger("interview")

SUMMARY_MODEL = GROQ_SUMMARY_MODEL
client = AsyncGroq(api_key=GROQ_API_KEY)


def SystemPrompt(profile: dict) -> str:
    name = profile.get("name")
    role = profile.get("role")
    company = profile.get("company")
    stack = ", ".join(profile.get("tech_stack", [])) or "general software engineering"
    company_line = f" at {company}" if company else " "

    resume_section = ""
    resume_text = (profile.get("resume_text") or "").strip()
    if resume_text:
        resume_section = (
            f"\n\n--- Candidate Resume (use this to personalise your questions) ---\n"
            f"{resume_text}\n"
            f"--- End of Resume ---\n\n"
            f"Reference the candidate's actual projects, technologies, and experience from "
            f"their resume when framing questions. Avoid asking about skills or projects not "
            f"mentioned in the resume unless the conversation naturally leads there."
        )

    return (
        f"You are an interview coach conducting a mock technical interview with {name}, "
        f"who is preparing for a {role} position{company_line}. "
        f"Their relevant tech stack: {stack}.{resume_section}\n\n"
        f"Ask one question at a time, tailored to their target role and stack. Reference their "
        f"previous answers when relevant — build follow-up questions naturally from what they've "
        f"already said rather than asking generic, disconnected questions. Give brief feedback after "
        f"each answer, then ask a natural follow-up. Keep responses under 3 sentences."
    )


async def stream_reply(history: list[dict], profile: dict):
    stream = await client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "system", "content": SystemPrompt(profile)}] + history,
        stream=True,
        reasoning_format="hidden",
    )
    async for chunk in stream:
        token = chunk.choices[0].delta.content or ""
        if token:
            yield token


async def generate_summary(session) -> dict:
    lines = []
    for turn in session.history:
        speaker = "Candidate" if turn["role"] == "user" else "Interviewer"
        lines.append(f"{speaker}: {turn['content']}")
    transcript_text = "\n".join(lines) if lines else "(no transcript available)"

    summary_prompt = f"""Based on this mock interview transcript for a {session.profile.get('role', 'technical')} position, provide a structured critique.

Respond with ONLY a valid JSON object. No markdown, no code fences, no explanation before or after.

Use exactly this structure:
{{
  "overall_impression": "2-3 sentence summary",
  "strengths": ["specific strength 1", "specific strength 2"],
  "areas_to_improve": ["specific area 1 with example from transcript"],
  "communication_notes": "notes on clarity, structure, pacing",
  "suggested_next_steps": ["actionable prep suggestion 1"]
}}

Transcript:
{transcript_text}"""

    response = await client.chat.completions.create(
        model=SUMMARY_MODEL,
        messages=[{"role": "user", "content": summary_prompt}],
        response_format={"type": "json_object"},
        max_tokens=1024,
    )
    raw = (response.choices[0].message.content or "").strip()
    log.debug("Summary raw response: %r", raw)
    if not raw:
        raise ValueError("Summary model returned empty content")

    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if match:
        raw = match.group(1)

    return json.loads(raw)
