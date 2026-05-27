from arya.core.llm import _get_client


MODEL_NAME = "llama-3.1-8b-instant"

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
        "definition and key points for this concept. Format as: "
        "Definition, Key points, Common exam traps. Keep it sharp and direct."
    ),
}


async def explain_concept(topic: str, level: str = "simple") -> str:
    selected_level = level if level in SYSTEM_PROMPTS else "simple"
    system_prompt = SYSTEM_PROMPTS[selected_level]

    try:
        response = _get_client().chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Explain: {topic}"},
            ],
        )
    except Exception as exc:
        raise RuntimeError(f"Failed to explain concept: {exc}") from exc

    return response.choices[0].message.content or ""


async def formula_lookup(topic: str) -> str:
    system_prompt = (
        "You are ARYA, an engineering reference assistant for a Mechatronics "
        "student. When given a topic, provide the most relevant formula(s) with:\n"
        "1. The formula in clear notation\n"
        "2. What each variable means\n"
        "3. Units\n"
        "4. One practical example of when to use it\n"
        "Keep it concise and exam-ready. Cover these domains when relevant:\n"
        "control theory, signal processing, mechanics, electronics,\n"
        "thermodynamics, mathematics, embedded systems."
    )

    try:
        response = _get_client().chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Give me the formula for: {topic}"},
            ],
        )
    except Exception as exc:
        raise RuntimeError(f"Failed to look up formula: {exc}") from exc

    return response.choices[0].message.content or ""


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

    try:
        response = _get_client().chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": f"Generate {num_questions} flashcard questions on: {topic}",
                },
            ],
        )
    except Exception as exc:
        raise RuntimeError(f"Failed to generate flashcard quiz: {exc}") from exc

    raw = response.choices[0].message.content or ""
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
