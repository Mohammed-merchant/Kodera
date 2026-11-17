"""Safety net between the LLM and the voice.

A small model sometimes says things it must not: "I have booked your appointment" when it never
called the booking tool (nothing was saved!), or medical advice. This processor checks every
sentence BEFORE it is spoken and swaps a bad one for a safe sentence.

It runs inside the agent's llm_node (see voice_pipeline.py), between the LLM and TTS, so a
replaced sentence is also what goes into the chat history. TTS needs whole sentences anyway, so
buffering sentences here adds no delay.
"""

import re
from typing import Any

import structlog

log = structlog.get_logger(__name__)

SAFE_NOT_BOOKED = "I have not booked that yet. Which day would you like to come in?"
SAFE_NOT_CANCELLED = "I have not cancelled anything yet. Could you tell me the appointment date?"
SAFE_CHECK_FIRST = "Let me check the schedule for you."
SAFE_SAY_AGAIN = "Sorry, could you say that once more?"
# A small model sometimes writes a tool call as plain text instead of making it. Never speak that.
_TOOL_LEAK = re.compile(
    r"(^/?(check_available_slots|book_appointment|cancel_appointment|my_appointments)"
    r"|<tool_call>|\{\s*\"[a-z_]+\"\s*:)"
)
SAFE_NO_ADVICE = "I am sorry, I cannot give medical advice. The doctor will guide you at the visit."

_APPOINTMENT = r"(?:appointment|booking|slot|visit|consultation|reservation)"
_BOOKED_WORDS = r"(?:booked|confirmed|scheduled|reserved|fixed|set up)"
_CANCEL_WORDS = r"(?:cancelled|canceled|removed)"
_NEGATION = re.compile(
    r"\b(?:not|never|no|yet|unable|can't|cannot|couldn't|haven't|hasn't|wasn't|won't|"
    r"will be|shall|should|would|could|want to|like to|if)\b|n't\b",
    re.IGNORECASE,
)
_CLAIM_BOOKED = re.compile(
    rf"\b{_BOOKED_WORDS}\b.*\b{_APPOINTMENT}\b|\b{_APPOINTMENT}\b.*\b{_BOOKED_WORDS}\b|"
    r"\bi(?:'ve| have) (?:booked|scheduled|reserved)\b|\byou(?:'re| are) (?:all set|booked)\b",
    re.IGNORECASE,
)
_CLAIM_CANCELLED = re.compile(
    rf"\b{_CANCEL_WORDS}\b.*\b{_APPOINTMENT}\b|\b{_APPOINTMENT}\b.*\b{_CANCEL_WORDS}\b",
    re.IGNORECASE,
)
_MEDICAL = (
    r"(?:medicine|medication|tablet|pill|paracetamol|ibuprofen|antibiotic|syrup|"
    r"ointment|cream|dose|fluids|steam|remedy|remedies)"
)
_ADVICE = re.compile(
    rf"\b(?:take|drink|eat|use|apply|try|have|avoid)\b.*\b{_MEDICAL}\b|"
    r"\b(?:get|take|plenty of) (?:plenty of )?rest\b|"
    r"\byou (?:may|might|could|probably|likely) (?:have|be suffering from)\b|"
    r"\b(?:sounds|looks) like (?:a |an )?(?:cold|flu|infection|virus|viral|allergy|fever)\b",
    re.IGNORECASE,
)
_TIME_OR_DAY = re.compile(
    r"\b\d{1,2}(?::\d{2})?\s?(?:a\.?m\.?|p\.?m\.?)\b|\b\d{1,2}:\d{2}\b|\bo'clock\b|"
    r"\b(?:morning|afternoon|evening|tomorrow|today|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday)\b",
    re.IGNORECASE,
)
_CLAIM_AVAILABLE = re.compile(
    r"\b(?:is|are)\s+(?:currently\s+|still\s+)?(?:available|free)\b|"
    r"\b(?:available|free)\s+(?:at|on|from|for)\b|"
    r"\bwe have (?:a |an |some )?(?:slot|slots|opening|openings|time|times|availability)\b",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"[.!?]\s*$|\n")
# The first part of a reply may be spoken before its sentence ends ("Sure, Daha,"), so the voice
# starts sooner. Only a part with nothing to check: any word below keeps it waiting for the full
# sentence and the full check, exactly as before.
_CLAUSE_END = re.compile(r"[,;:]\s*$")
_RISKY = re.compile(
    rf"\b{_APPOINTMENT}\b|\b{_BOOKED_WORDS}\b|\b{_CANCEL_WORDS}\b|\b{_MEDICAL}\b|"
    r"\b(?:book|cancel|reschedul\w*|rest|sleep|water|hydrat\w*|available|free|slots?|"
    r"times?|am|pm|a\.m|p\.m|doctor|fee|fees|rupees?)\b|\d",
    re.IGNORECASE,
)


def _speakable_early(text: str) -> bool:
    """A short opening clause with nothing that a safety check could object to."""
    plain = text.strip()
    return (
        bool(_CLAUSE_END.search(plain))
        and 2 <= len(plain.split()) <= 8
        and not _RISKY.search(plain)
        and not _TIME_OR_DAY.search(plain)
        and not _TOOL_LEAK.search(plain)
    )


def check_sentence(
    sentence: str, *, booked: bool, cancelled: bool, checked: bool = True
) -> tuple[str, str | None]:
    """Return (text to speak, reason it was replaced or None)."""
    plain = sentence.strip()
    if _ADVICE.search(plain):
        return SAFE_NO_ADVICE, "medical advice"
    if _TOOL_LEAK.search(plain):
        return SAFE_SAY_AGAIN, "a tool call was written as text"
    negated = bool(_NEGATION.search(plain))
    if not booked and not negated and _CLAIM_BOOKED.search(plain):
        return SAFE_NOT_BOOKED, "claimed a booking that was not made"
    if not cancelled and not negated and _CLAIM_CANCELLED.search(plain):
        return SAFE_NOT_CANCELLED, "claimed a cancellation that was not made"
    if (
        not checked
        and not negated
        and not plain.endswith("?")
        and _TIME_OR_DAY.search(plain)
        and _CLAIM_AVAILABLE.search(plain)
    ):
        return SAFE_CHECK_FIRST, "promised a time before checking the schedule"
    return sentence, None


class ReplyGuard:
    """Checks each LLM sentence of ONE reply before it reaches the voice.

    Make a new one per reply: feed() the streamed text, then end() when the reply is over.
    An interrupted reply simply drops its guard, and with it any half sentence.
    """

    def __init__(self, recorder: Any) -> None:
        self._recorder = recorder
        self._buffer = ""
        self._replaced_this_reply = False
        self._released_any = False  # only the reply's very first clause may go early

    async def _release(self, text: str) -> str | None:
        spoken, why = check_sentence(
            text,
            booked=self._recorder.booked,
            cancelled=self._recorder.cancelled,
            checked=getattr(self._recorder, "checked", True),
        )
        if why:
            # Say the safe sentence once per reply; drop any further bad ones silently.
            log.warning("reply_blocked", reason=why, said=text.strip()[:120])
            await self._recorder.add_event("guard", reason=why, blocked=text.strip()[:200])
            if self._replaced_this_reply and spoken in (
                SAFE_NO_ADVICE,
                SAFE_NOT_BOOKED,
                SAFE_CHECK_FIRST,
                SAFE_SAY_AGAIN,
            ):
                return None
            self._replaced_this_reply = True
            spoken += " "  # the next sentence must not run into the safe one
        return spoken

    async def feed(self, text: str) -> list[str]:
        """Text that may be spoken now (whole, checked sentences)."""
        self._buffer += text
        if not _SENTENCE_END.search(self._buffer):
            if self._released_any or not _speakable_early(self._buffer):
                return []
            clause, self._buffer = self._buffer, ""
            self._released_any = True
            return [clause]  # nothing in it to check (see _speakable_early)
        sentence, self._buffer = self._buffer, ""
        spoken = await self._release(sentence)
        self._released_any = True
        return [spoken] if spoken else []

    async def end(self) -> list[str]:
        """The reply is over (or a tool call follows): release the last half sentence."""
        if not self._buffer.strip():
            self._buffer = ""
            return []
        sentence, self._buffer = self._buffer, ""
        spoken = await self._release(sentence)
        return [spoken] if spoken else []
