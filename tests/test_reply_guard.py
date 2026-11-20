"""The reply guard streams: a harmless opening clause goes to the voice early, the rest of the
reply is still checked sentence by sentence."""

from typing import Any

from app.pipeline.guards import SAFE_NO_ADVICE, SAFE_NOT_BOOKED, ReplyGuard


class FakeRecorder:
    booked = False
    cancelled = False
    checked = True

    async def add_event(self, *_: Any, **__: Any) -> None:
        pass


async def stream(pieces: list[str]) -> list[str]:
    """What the voice receives, in order, when the model streams these pieces."""
    guard = ReplyGuard(FakeRecorder())
    out: list[str] = []
    for piece in pieces:
        out += await guard.feed(piece)
    return out + await guard.end()


async def test_a_harmless_opening_clause_is_spoken_before_the_sentence_ends() -> None:
    out = await stream(["Sure", ", Daha", ",", " what day", " suits you", "?"])
    assert out == ["Sure, Daha,", " what day suits you?"]


async def test_only_the_first_clause_goes_early() -> None:
    # One word is too short to say alone, so the first clause is "Okay, thank you,".
    out = await stream(["Okay", ",", " thank you", ",", " see you", ",", " goodbye", "."])
    assert out == ["Okay, thank you,", " see you, goodbye."]


async def test_a_clause_with_booking_words_waits_for_the_full_check() -> None:
    out = await stream(["Great", ", your appointment", ",", " is booked", "."])
    assert out == [SAFE_NOT_BOOKED + " "]  # never "Great, your appointment," on its own


async def test_a_clause_with_a_time_or_day_waits() -> None:
    for pieces in (["For tomorrow", ",", " I have 5 PM", "."], ["At 5", ",", " yes", "."]):
        out = await stream(pieces)
        assert len(out) == 1 and out[0].strip().startswith(pieces[0])


async def test_advice_after_an_early_clause_is_still_blocked() -> None:
    out = await stream(["I understand", ",", " please take paracetamol twice a day", "."])
    assert out == ["I understand,", SAFE_NO_ADVICE + " "]


async def test_rest_advice_never_goes_early() -> None:
    out = await stream(["Rest well", ",", " and take paracetamol", "."])
    assert out == [SAFE_NO_ADVICE + " "]


async def test_model_markers_are_never_spoken() -> None:
    from app.pipeline.guards import SAFE_SAY_AGAIN

    out = await stream(["<|channel>thought\n", "<channel|><|channel>thought\n"])
    assert out == [SAFE_SAY_AGAIN + " "]  # one safe sentence, never the markers


async def test_a_stuck_gemma_reply_is_restarted_once(monkeypatch: Any) -> None:
    """Gemma sometimes repeats "<|channel>thought" instead of answering: the agent drops that
    reply at its first token and asks again with a nudge; a normal reply is untouched."""
    from livekit.agents import Agent, llm

    from app.pipeline import voice_pipeline

    contexts: list[Any] = []

    async def fake_llm_node(self: Any, ctx: Any, tools: Any, settings: Any) -> Any:
        contexts.append(ctx)
        stuck = len(contexts) == 1 and self.stuck
        pieces = ["<|channel>", "thought\n", "<channel|>"] if stuck else ["Yes", ", I can help."]
        for piece in pieces:
            yield piece

    monkeypatch.setattr(Agent.default, "llm_node", fake_llm_node)
    agent_cls = voice_pipeline._agent_cls()
    for stuck, calls in ((True, 2), (False, 1)):
        contexts.clear()
        agent = agent_cls(recorder=FakeRecorder(), instructions="test")
        agent.stuck = stuck
        out = [c async for c in agent._model_stream(llm.ChatContext.empty(), [], None)]
        assert "".join(out) == "Yes, I can help." and len(contexts) == calls
        if stuck:  # the retry carried the nudge as a system message
            last = contexts[1].items[-1]
            assert last.role == "system" and last.text_content == voice_pipeline.LOOP_NUDGE
