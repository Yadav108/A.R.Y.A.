from arya.core.llm import INTENT_MODEL_NAME as MODEL_NAME, _complete, grounding_prompt, knowledge_for

SYSTEM_PROMPTS = {
    "simple": (
        "You are ARYA, a study assistant. Explain the concept in simple "
        "terms as if teaching a curious 16-year-old. Use one analogy. "
        "Keep it under 4 sentences."
    ),
    "technical": (
        "You are ARYA, a study assistant. Explain the concept with full "
        "technical depth for a Mechatronics engineering student. Include "
        "relevant equations or systems if applicable. Keep it under 6 sentences."
    ),
    "exam": (
        "You are ARYA, a study assistant. Give a concise exam-ready "
        "definition, the key points and one common exam trap for this concept, "
        "in at most five plain spoken sentences. No lists, headings or markdown."
    ),
}
_SPOKEN_STYLE = (" Your answer is spoken aloud: plain sentences only, no lists, numbering, headings, "
                 "markdown or LaTeX. Never invent numbers or examples.")


async def explain_concept(topic: str, level: str = "simple") -> str:
    selected_level = level if level in SYSTEM_PROMPTS else "simple"
    passages = await knowledge_for(topic)
    system_prompt = SYSTEM_PROMPTS[selected_level] + _SPOKEN_STYLE + grounding_prompt(passages, topic)

    try:
        response = await _complete(
            MODEL_NAME,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Explain: {topic}"},
            ],
        )
    except Exception as exc:
        raise RuntimeError(f"Failed to explain concept: {exc}") from exc

    return response.content


async def formula_lookup(topic: str) -> str:
    system_prompt = (
        "You are ARYA, an engineering reference assistant for a Mechatronics student. "
        "Give the formula for the topic and say in one sentence what each symbol means and its unit. "
        "At most three plain sentences: no lists, numbering, headings, markdown, worked examples or "
        "example numbers. If the course excerpts contain the formula, copy it exactly as written there; "
        "if they don't, give the standard textbook formula and don't guess."
    )
    passages = await knowledge_for(f"formula for {topic}")
    system_prompt += grounding_prompt(passages, topic)

    try:
        response = await _complete(
            MODEL_NAME,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Give me the formula for: {topic}"},
            ],
        )
    except Exception as exc:
        raise RuntimeError(f"Failed to look up formula: {exc}") from exc

    return response.content


async def flashcard_quiz(topic: str, num_questions: int = 3) -> str:
    if num_questions < 1:
        return "Please specify at least 1 question."

    capped = False
    requested_questions = num_questions
    if num_questions > 10:
        num_questions = 10
        capped = True

    system_prompt = (
        "You are ARYA, a study assistant running a voice-based flashcard quiz "
        "for a Mechatronics engineering student. Generate exactly "
        f"{num_questions} "
        "questions on the given topic. Format strictly as:\n"
        "Q1: [question]\n"
        "A1: [answer]\n"
        "Q2: [question]\n"
        "A2: [answer]\n"
        "Keep questions concise and exam-relevant.\n"
        "Answers should be 1-2 sentences maximum.\n"
        "Cover conceptual, formula-based, and application questions."
    )
    passages = await knowledge_for(topic)
    if passages:
        system_prompt += grounding_prompt(passages) + " Base the questions on these course excerpts."

    try:
        response = await _complete(
            MODEL_NAME,
            [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": f"Generate {num_questions} flashcard questions on: {topic}",
                },
            ],
        )
    except Exception as exc:
        raise RuntimeError(f"Failed to generate flashcard quiz: {exc}") from exc

    raw = response.content
    lines = [line.strip() for line in raw.splitlines() if line.strip()]

    qa_pairs: dict[int, dict[str, str]] = {}
    for line in lines:
        if ":" not in line:
            continue
        prefix, value = line.split(":", 1)
        prefix = prefix.strip()
        value = value.strip()
        if len(prefix) < 2:
            continue

        marker = prefix[0].upper()
        number_text = prefix[1:].strip()
        if marker not in {"Q", "A"} or not number_text.isdigit():
            continue

        idx = int(number_text)
        if idx < 1:
            continue
        if idx not in qa_pairs:
            qa_pairs[idx] = {"question": "", "answer": ""}
        if marker == "Q":
            qa_pairs[idx]["question"] = value
        else:
            qa_pairs[idx]["answer"] = value

    formatted_lines: list[str] = [f"Here are {num_questions} flashcard questions on {topic}:"]
    for i in range(1, num_questions + 1):
        question = qa_pairs.get(i, {}).get("question", "").strip()
        answer = qa_pairs.get(i, {}).get("answer", "").strip()
        if not question:
            question = "Question not parsed."
        if not answer:
            answer = "Answer not parsed."
        formatted_lines.append(f"Q{i}: {question} Answer: {answer}")

    if capped:
        formatted_lines.append(
            f"Note: requested {requested_questions} questions, capped at 10."
        )

    return "\n".join(formatted_lines)
