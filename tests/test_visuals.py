"""Plots and block diagrams."""
import pytest

from arya.knowledge import visuals as V


def test_parse_poly_and_transfer_function():
    assert V.parse_poly("s^2 + 2s + 10") == [1, 2, 10]
    assert V.parse_poly("s**2+0.6*s+4") == [1, 0.6, 4]
    assert V.parse_poly("s^3 - s") == [1, 0, -1, 0]
    assert V.parse_transfer_function("10/(s^2+2s+10)") == ([10], [1, 2, 10])
    assert V.parse_transfer_function("(s+1)/(s^2+3s+2)") == ([1, 1], [1, 3, 2])
    assert V.parse_transfer_function("hello world") is None


@pytest.mark.parametrize("msg, args", [
    ("show the step response with damping ratio 0.3 and natural frequency 2",
     {"kind": "step", "damping_ratio": 0.3, "natural_frequency": 2.0}),
    ("bode plot of 10/(s^2+2s+10)", {"kind": "bode", "numerator": [10], "denominator": [1, 2, 10]}),
    ("plot the step response of a first order system with time constant 0.5 s and gain 2",
     {"kind": "step", "time_constant": 0.5, "gain": 2.0}),
    ("what is the weather in Munich", None),
    ("explain step response", None),
])
def test_quick_system_request(msg, args):
    assert V.quick_system_request(msg) == args


def test_quick_function_request():
    r = V.quick_function_request("Plot sin x and cos x from 0 to 2 pi")
    assert r["expressions"] == ["sin(x)", "cos(x)"] and r["x_min"] == 0 and abs(r["x_max"] - 6.283) < 0.01
    assert V.quick_function_request("plot x squared minus 3")["expressions"] == ["x^2-3"]
    assert V.quick_function_request("plot the history of the roman empire") is None


def test_step_response_facts_and_speech():
    r = V.plot_system("step", natural_frequency=2, damping_ratio=0.3)
    assert V.image(r["id"])[:4] == b"\x89PNG"
    said = V.spoken_summary(r)
    assert "37.2 percent" in said and "5.62 seconds" in said


def test_unstable_system_is_reported():
    assert "grows without bound" in V.spoken_summary(V.plot_system("step", numerator=[1], denominator=[1, -1]))


@pytest.mark.parametrize("bad", ["__import__('os').system('echo hi')", "open('x')", "x.__class__", "1/(s+1)"])
def test_plot_function_rejects_unsafe_or_transfer_functions(bad):
    with pytest.raises(Exception):
        V.plot_function([bad])


def test_instant_diagram_is_a_closed_loop():
    d = V.quick_diagram_request("Draw a block diagram of a temperature control loop with a PID controller, "
                                "heater, oven and thermocouple")
    assert d["connections"] == ["Setpoint -> Σ", "Σ -> PID controller : error", "PID controller -> Heater",
                                "Heater -> Oven", "Oven -> Thermocouple : temperature",
                                "Thermocouple -> Σ : measured temperature"]
    r = V.diagram(d["connections"], d["title"])
    assert r["mermaid"].count("-->") == 6
    assert V.spoken_summary(r) == "Here's the block diagram of the temperature control loop, on screen."


@pytest.mark.parametrize("msg", ["draw a diagram", "plot sin x and cos x", "what is a block diagram?",
                                 "Draw a block diagram of a temperature control loop"])
def test_no_instant_diagram_when_unclear(msg):
    assert V.quick_diagram_request(msg) is None


def test_close_loop_adds_missing_feedback_only_for_loops():
    conns = ["Setpoint -> PID Controller", "PID Controller -> Heater", "Heater -> Oven", "Oven -> Thermocouple"]
    assert V.close_loop(conns, "Temperature Control Loop")[-1] == "Thermocouple -> PID Controller : feedback"
    assert V.close_loop(conns, "Signal chain") == conns


@pytest.mark.parametrize("caption, said", [
    ("Temperature Control Loop", "Here's the block diagram of the temperature control loop, on screen."),
    ("DC Motor Speed Control", "Here's the block diagram of the DC motor speed control, on screen."),
    ("Diagram", "Here's the block diagram, on screen."),
])
def test_diagram_speech_keeps_acronyms(caption, said):
    assert V.spoken_summary({"type": "diagram", "caption": caption, "mermaid": "flowchart LR"}) == said
