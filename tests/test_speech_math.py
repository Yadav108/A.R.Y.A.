"""Spoken maths, readable formulas on screen, and sentence splitting while streaming."""
import random
import re

import pytest

from arya.core.speech_math import latex_to_text, speakable

BS = chr(92)


@pytest.mark.parametrize("latex, screen", [
    (r"the damping ratio (\(\zeta\)) and (\(\omega_n\)).", "the damping ratio (ζ) and (ωₙ)."),
    (r"\[ \omega_n = \sqrt{\frac{k}{m}} \]", "ωₙ = √(k/m)"),
    (r"\[ s^2 + 2\zeta\omega_n s + \omega_n^2 = 0 \]", "s² + 2ζωₙ s + ωₙ² = 0"),
    (r"\(x \pm 1\)", "x ± 1"),
])
def test_latex_becomes_readable_unicode(latex, screen):
    assert latex_to_text(latex).strip() == screen


@pytest.mark.parametrize("text, spoken", [
    (r"\[ \omega_n = \sqrt{\frac{k}{m}} \]", "omega n equals the square root of k over m"),
    (r"\[ s^2 + 2\zeta\omega_n s + \omega_n^2 = 0 \]",
     "s squared plus 2 zeta omega n s plus omega n squared equals 0"),
    (r"The overshoot is \(100 \cdot e^{-\zeta\pi/\sqrt{1-\zeta^2}}\) percent.",
     "The overshoot is 100 times e to the power of minus zeta pi over the square root of 1 minus zeta squared percent."),
    ("The speed is 9.4 km/h and the acceleration 2.55 m/s2.",
     "The speed is 9.4 kilometres per hour and the acceleration 2.55 metres per second squared."),
    ("G(s) = K / (τ s + 1)", "G of s equals K over tau s plus 1"),
])
def test_maths_is_spoken_as_words(text, spoken):
    assert speakable(text) == spoken


@pytest.mark.parametrize("text", ["It costs $5 and that is fine.", "Plain sentence with no maths at all.",
                                  "A well-known x-ray."])
def test_ordinary_text_is_left_alone(text):
    assert speakable(text) == text
    assert latex_to_text(text) == text


ANSWER = (r"For a second-order system, the parameters are the damping ratio (\(\zeta\)) and the natural frequency "
          r"(\(\omega_n\)). Here's how they influence stability: 1. Damping Ratio (\(\zeta\)): it is the ratio "
          r"\[ \zeta = \frac{c}{c_c} \] of actual to critical damping. 2. Natural Frequency (\(\omega_n\)): it is "
          r"given by \[ \omega_n = \sqrt{\frac{k}{m}} \] where k is the stiffness. The characteristic equation is:"
          "\n\\[ s^2 + 2\\zeta\\omega_n s + \\omega_n^2 = 0 \\]\n"
          r"The system is stable if \(\zeta > 0\). If \(\zeta < 0.5\) the overshoot exceeds 16.3 percent.")


def test_streamed_answer_never_splits_decimals_or_speaks_latex():
    import arya.core.llm as llm
    from arya.core.tts import clean_for_speech

    for seed in range(100):                               # many random token boundaries
        rnd = random.Random(seed)
        tokens, i = [], 0
        while i < len(ANSWER):
            n = rnd.randint(1, 6)
            tokens.append(ANSWER[i:i + n])
            i += n
        buffer, sentences = "", []
        for tok in tokens:
            buffer += tok
            while (cut := llm._sentence_cut(buffer)) is not None:
                sentences.append(buffer[:cut])
                buffer = buffer[cut:]
        sentences.append(buffer)
        assert "".join(sentences) == ANSWER               # nothing lost or duplicated
        for s in sentences:
            screen = llm.latex_to_text(s)
            spoken = clean_for_speech(screen)
            assert BS not in screen and BS not in spoken
            assert "0. 5" not in spoken and "16. 3" not in spoken
            assert not re.search(r"(?:^|[:;])\s*\d{1,2}\.\s*$", spoken), f"list number cut off: {spoken!r}"


@pytest.mark.parametrize("text, parts", [
    ("It is 3.14 exactly. Next one.", ["It is 3.14 exactly.", " Next one."]),
    ("Version 2. Then go.", ["Version 2.", " Then go."]),
    ("Steps: 1. Open it. 2. Close it.", ["Steps: 1. Open it.", " 2. Close it."]),
    ("Wait for it...", ["Wait for it..."]),
])
def test_split_sentences(text, parts):
    import arya.core.llm as llm

    assert llm.split_sentences(text) == parts
