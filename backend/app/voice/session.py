"""One long-lived voice socket: always listening, one spoken turn at a time.

The turn shape is set by the latency budget (end of speech to first audio byte).
The reply is therefore split into sentences and each one is synthesised while the
graph is still producing the next; waiting for the whole reply would add its full
generation time to the first byte.

Two concurrency decisions follow from that:

  - The reader loop never blocks on the answer. Audio keeps arriving while TTS
    streams, so the endpointer can decide the user has started talking again and
    cut the turn in flight off — barge-in. A loop that awaited the turn would be
    deaf until the answer finished playing.
  - Cancellation is the only interruption path. The turn runs as a task, so
    `cancel()` unwinds it at its next await point; `CancelledError` derives from
    BaseException, which is what keeps the broad handler below from eating it.

Frames — the route owns the socket, this module owns the vocabulary:

  in    binary PCM at `voice_input_sample_rate`, or `{"type": "stop"}`
  out   binary PCM at the negotiated output rate, plus the control frames
        `ready` / `speech_start` / `transcript` / `token` / `audio_end` / `error`

Endpointing is authoritative HERE, not in the client: a browser cannot be trusted
to decide when a sentence ended, and the browser and the device must behave the
same way. `speech_start` doubles as "stop playing what you have buffered".
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Literal

from langchain_core.messages import AIMessage, AIMessageChunk

from app.api.deps import build_initial_state, build_run_config
from app.core.config import settings
from app.core.observability import (
    MAX_STATUS_MESSAGE_CHARS,
    MAX_TRACE_IO_CHARS,
    child_observation,
    enrich_trace,
    mark_turn_cancelled,
    mark_turn_failed,
    stamp_voice_outcome,
    stamp_voice_timing,
    stt_attributes,
    tts_attributes,
    turn_span,
)
from app.core.timing import StageMarks, log_stage_marks, monotonic_now
from app.language.voices import voice_for
from app.voice.audio import StreamingResampler, duration_s
from app.voice.stt import Transcriber
from app.voice.tts import PCM_SAMPLE_RATE, SentenceSplitter, Synthesizer
from app.voice.vad import EndpointDetector, Ignored, SpeechStart, Utterance, Vad
from app.voice.wake import (
    WAKE_GRACE_SECONDS,
    WakeGate,
    WakeWord,
    WakeWordUnavailable,
    is_cancel_phrase,
    strip_wake_phrase,
)

logger = logging.getLogger(__name__)

# An outbound frame is raw audio or a control dict with one of the types above.
Frame = dict[str, Any]
Outbound = bytes | Frame
Inbound = bytes | Frame
Send = Callable[[Outbound], Awaitable[None]]

# Output rates a client may ask for. The browser plays the TTS-native 24 kHz and
# pays nothing; the reSpeaker XVF3800 runs at 16 kHz and pays for one resampler.
OUTPUT_SAMPLE_RATES = frozenset({16000, PCM_SAMPLE_RATE})

# Persisted as the assistant's side of a turn a barge-in cut off. LangGraph
# commits a run's input as its FIRST checkpoint, so the turn's HumanMessage is
# durable long before the responder can answer; without this marker a cancelled
# turn leaves that question unanswered for ever. The text is deliberately short
# and plainly not spoken: it restores the question/answer alternation a model
# expects without inventing an answer the assistant never gave.
INTERRUPTED_MARKER = "[interrupted]"

# What a turn produced. `answer` is a normal turn; the other two are gated-session
# outcomes that never reach the graph, so they must not be averaged into turn
# latency (`_run_turn` skips `turn_timings` for them).
TurnOutcome = Literal["answer", "wake_only", "cancel"]


def _content_to_str(content: Any) -> str:
    """Flatten a message chunk's content to text.

    Mirrors the same helper in `app.api.routes`; both exist because the two
    transports stream the same token shape. Consolidating them into
    `app.graph.utils` is a tidy-up, not a behaviour change.

    Args:
        content: A message's content: a string, or a list of content blocks.

    Returns:
        The text of the message, empty when there is none.
    """
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def _responder_token(event: dict[str, Any]) -> str | None:
    """Return the reply text a streaming event carries, or None.

    Only the responder's tokens are the answer: the router and the knowledge node
    also call models, and speaking their output would read the classification
    aloud.

    Args:
        event: One `astream_events` v2 event.

    Returns:
        The token's text, or None for anything that is not a responder token.
    """
    if event.get("event") != "on_chat_model_stream":
        return None
    if event.get("metadata", {}).get("langgraph_node") != "responder":
        return None
    chunk = event.get("data", {}).get("chunk")
    if not isinstance(chunk, AIMessageChunk):
        return None
    return _content_to_str(chunk.content)


def _detected_lang(event: dict[str, Any]) -> str | None:
    """Return the language `detect_lang` resolved, or None.

    Read from the node's own output rather than the final state because the voice
    session needs it BEFORE the answer finishes — the first sentence is
    synthesised while the graph is still running, and the voice depends on it.

    Args:
        event: One `astream_events` v2 event.

    Returns:
        The resolved app language tag, or None for any other event.
    """
    if event.get("event") != "on_chain_end" or event.get("name") != "detect_lang":
        return None
    output = event.get("data", {}).get("output")
    if not isinstance(output, dict):
        return None
    lang = output.get("lang")
    return lang if isinstance(lang, str) and lang else None


def _final_state(event: dict[str, Any]) -> dict[str, Any] | None:
    """Return the run's final state when the event is the root chain end.

    Args:
        event: One `astream_events` v2 event.

    Returns:
        The final graph state, or None for any other event.
    """
    if event.get("event") != "on_chain_end" or event.get("name") != "LangGraph":
        return None
    if event.get("parent_ids"):
        return None
    output = event.get("data", {}).get("output")
    return output if isinstance(output, dict) else None


class VoiceSession:
    """Turn loop for one voice socket.

    One instance per connection, and it owns the sound rather than the
    conversation: the transcript is handed to the same graph — and therefore the
    same thread_id — as a typed turn, so speech and typing share one memory.
    """

    def __init__(
        self,
        *,
        graph: Any,
        transcriber: Transcriber,
        synthesizer: Synthesizer,
        vad: Vad,
        send: Send,
        session_id: str,
        device_id: str | None = None,
        output_sample_rate: int = settings.voice_output_sample_rate,
        wake: WakeWord | None = None,
        wake_phrase: str = "",
    ) -> None:
        """Wire one session.

        Args:
            graph: The compiled graph, consumed through `astream_events`.
            transcriber: Speech-to-text provider.
            synthesizer: Text-to-speech provider.
            vad: Speech-probability scorer. The endpointer state machine around
                it is built here from settings so every session boundaries audio
                identically.
            send: Awaited for every outbound frame — bytes for audio, dict for
                control. Whatever it raises is left to the caller, so the
                transport keeps its own disconnect policy.
            session_id: Conversation id, shared with the text channel.
            device_id: Which device this socket belongs to, if any.
            output_sample_rate: Rate to send audio at.
            wake: Wake-word detector for this stream, or None for an un-gated
                session. None is the whole of today's behaviour: every onset is
                forwarded and every segment becomes a turn.
            wake_phrase: Spoken phrase to strip from the transcript. Only used
                when `wake` is set.

        Raises:
            ValueError: if `output_sample_rate` is not supported.
        """
        if output_sample_rate not in OUTPUT_SAMPLE_RATES:
            raise ValueError(
                f"output_sample_rate must be one of {sorted(OUTPUT_SAMPLE_RATES)}, "
                f"got {output_sample_rate}"
            )
        self._graph = graph
        self._transcriber = transcriber
        self._synthesizer = synthesizer
        self._send = send
        self._session_id = session_id
        self._device_id = device_id
        self._output_rate = output_sample_rate
        self._detector = EndpointDetector(
            vad,
            threshold=settings.voice_vad_threshold,
            neg_threshold=settings.voice_vad_neg_threshold,
            min_speech_ms=settings.voice_vad_min_speech_ms,
            min_silence_ms=settings.voice_vad_min_silence_ms,
            speech_pad_ms=settings.voice_vad_speech_pad_ms,
            max_utterance_s=settings.voice_vad_max_utterance_s,
        )
        # Wake-word state. The gate is always constructed — it is two floats —
        # but only consulted when `_wake` is set, which keeps every branch below
        # a plain `is not None` check.
        self._wake = wake
        self._wake_phrase = wake_phrase
        self._gate = WakeGate(settings.voice_wake_word_window_seconds)
        # When the most recent detection arrived. `wake_lead` is measured from
        # this, so it must be stamped by the session, not by the detector.
        self._last_detection_at: float | None = None
        # One slot for a segment rejected just before its own detection arrived.
        self._rejected: tuple[Utterance, float] | None = None
        # The language the LAST turn resolved to. It biases this turn's
        # transcription and picks this turn's voice before the graph has
        # answered, so it is remembered here rather than read back from the
        # checkpointer.
        self._last_lang: str | None = None
        self._turn: asyncio.Task[None] | None = None
        # Diagnostics for whoever owns the session. These are the same durations
        # that go to the log line and the trace; keeping them lets a caller (the
        # latency tool, the route) read them instead of scraping the log. One
        # entry per completed turn, oldest first — a failed turn publishes the
        # stages it got to.
        self.turn_timings: list[dict[str, float]] = []

    async def run(self, frames: AsyncIterator[Inbound]) -> None:
        """Serve one socket until the client stops or the frames run out.

        Args:
            frames: Inbound frames in arrival order. The iterator ending counts
                as the client leaving.

        Raises:
            Exception: whatever `send` raises, so the caller can honour the
                transport's own disconnect policy.
        """
        logger.info(
            "voice session open (output_rate=%d, resampling=%s)",
            self._output_rate,
            self._output_rate != PCM_SAMPLE_RATE,
        )

        # Notify the client that the session is ready
        await self._send(
            {
                "type": "ready",
                "input_sample_rate": settings.voice_input_sample_rate,
                "output_sample_rate": self._output_rate,
                "format": "pcm_s16le",
                "wake_word": self._wake is not None,
            }
        )

        try:
            async for frame in frames:
                if isinstance(frame, bytes):
                    await self._handle_audio(frame)
                elif frame.get("type") == "stop":
                    break
                else:
                    logger.debug("ignoring voice frame %s", frame.get("type"))
        finally:
            # The turn is a task we created: leaving it behind while unwinding
            # would keep pushing frames onto a socket nobody owns any more.
            await self._cancel_turn()
            logger.info("voice session closed")

    async def _handle_audio(self, pcm: bytes) -> None:
        """Score wake-word audio, then feed the endpointer and act on it.

        The wake detector runs FIRST so a detection contained in this same chunk
        is already visible when the endpointer's events are processed: detection
        steps every 80 ms, endpointing every 32 ms, and the order is what keeps a
        very short phrase from being judged before it was heard.

        Args:
            pcm: Mono PCM at `voice_input_sample_rate`, of any length.
        """
        if self._wake is not None:
            await self._detect_wake(self._wake, pcm)

        for event in self._detector.push(pcm):
            if isinstance(event, SpeechStart):
                # A gated session forwards an onset only while a permit is open.
                # Otherwise room noise would flush the client's playout and cancel
                # the answer — the exact behaviour the gate exists to stop.
                if self._wake is None or self._gate.is_open():
                    await self._barge_in()
            elif isinstance(event, Utterance):
                if self._wake is not None and not self._gate.admit(event.started_at):
                    self._reject(event)
                    continue
                self._rejected = None
                # Deliberately not awaited: the reader has to stay available for
                # the audio that would interrupt this turn.
                self._turn = asyncio.create_task(self._run_turn(event))
            elif isinstance(event, Ignored):
                logger.debug("voice blip ignored (%.2fs)", event.duration_s)

    async def _detect_wake(self, wake: WakeWord, pcm: bytes) -> None:
        """Score one chunk for the wake phrase and act on what it hears.

        A detection is three things at once: it opens the permit, it is the
        barge-in signal (the phrase IS how a user interrupts an answer), and it
        may rescue a segment the endpointer closed a moment too early.

        Args:
            wake: This session's detector. Passed in rather than read from `self`
                so the `None` check above is the only place that decides whether
                the session is gated.
            pcm: Mono PCM at `voice_input_sample_rate`, of any length.

        Raises:
            WakeWordUnavailable: if the detector raises. Fail closed: running
                un-gated would send ambient audio to a paid STT, so the session
                sends one error frame and lets the route close the socket.
        """
        try:
            detections = wake.push(pcm)
        except Exception as exc:
            logger.exception("wake word detector failed")
            await self._send({"type": "error", "detail": "wake word unavailable"})
            raise WakeWordUnavailable(str(exc)) from exc

        for detection in detections:
            self._last_detection_at = monotonic_now()
            self._gate.arm()
            logger.info(
                "wake word detected (score=%.2f, in_turn=%s)",
                detection.score,
                self._turn is not None and not self._turn.done(),
            )
            # The phrase flushes the client and cancels an answer in flight. When
            # nothing is playing it is a harmless empty flush that still moves the
            # device's LED to LISTENING at the right moment.
            await self._barge_in()
            await self._recover_rejected()

    def _reject(self, utterance: Utterance) -> None:
        """Keep a segment that arrived without a permit, and log why.

        The log line is the metric the tuning runbook reads: `since_arm` is
        measured from the deadline the decision actually used, so a cluster just
        above `VOICE_WAKE_WORD_WINDOW_SECONDS` means the window is too short for
        this household, while a value well below it is the one-shot rule working.

        Args:
            utterance: The segment the gate refused.
        """
        since = (
            "never"
            if self._gate.last_arm_at is None
            else f"{utterance.started_at - self._gate.last_arm_at:.1f}s"
        )
        logger.info(
            "voice turn ignored: no wake word (speech=%.1fs, since_arm=%s)",
            utterance.detected_s,
            since,
        )
        self._rejected = (utterance, monotonic_now())

    async def _recover_rejected(self) -> None:
        """Run a segment whose own detection arrived just after its endpoint.

        One slot and half a second: the detector steps on 80 ms hops while the
        endpointer closes on 32 ms windows, so a very short "Hey Jarvis, ..." can
        be finalised first. A queue would only make false accepts likelier.
        """
        if self._rejected is None:
            return
        utterance, rejected_at = self._rejected
        self._rejected = None
        if monotonic_now() - rejected_at > WAKE_GRACE_SECONDS:
            return
        if not self._gate.admit(utterance.started_at):
            return
        logger.info("wake word recovered a segment finalised before the detection")
        self._turn = asyncio.create_task(self._run_turn(utterance))

    async def _barge_in(self) -> None:
        """Cut off the answer that is playing and tell the client to as well.

        `speech_start` goes out before anything is awaited, so the client stops
        its playout at once. The turn is then unwound to completion rather than
        merely cancelled: its teardown closes the interrupted thread, and the
        next utterance must not start while that write is still in flight.
        """
        interrupting = self._turn is not None and not self._turn.done()
        if interrupting:
            logger.info("barge-in: the user spoke over the answer")
        await self._send({"type": "speech_start"})
        if interrupting:
            await self._cancel_turn()

    async def _cancel_turn(self) -> None:
        """Cancel the turn in flight, if any, and wait for it to stop.

        Waiting is the point: a cancelled task still inside a `send` would
        otherwise keep writing frames after the session believes it is closed.
        """
        if self._turn is None:
            return
        turn, self._turn = self._turn, None
        turn.cancel()
        try:
            await turn
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            # `_run_turn` reports its own failures, so this is only a guard: a
            # turn must never take the socket down with it.
            logger.warning("voice turn ended badly (%s)", type(exc).__name__)

    async def _close_interrupted_turn(self) -> None:
        """Give a turn that never finished an assistant reply, if it needs one.

        LangGraph commits a run's input as its first checkpoint, so a turn's
        HumanMessage is durable the moment the graph starts — but the responder's
        AIMessage only lands when the responder node returns. Cancelling the turn
        mid-node therefore used to leave the persisted thread holding a question
        with no answer: `GET /sessions/{id}/history` showed it unanswered, and the
        next run fed it back to the model as an unanswered user turn.

        `StateSnapshot.next` is non-empty exactly while the graph still has a node
        to run, so the marker is written only for a run that stopped before END. A
        turn that answered, or one cancelled before the graph started, has nothing
        pending and is left untouched.

        Never raises: this is a repair, and an exception escaping here would
        replace whatever the cancelled turn is unwinding with.
        """
        config = {"configurable": {"thread_id": self._session_id}}
        try:
            snapshot = await self._graph.aget_state(config)
            if not snapshot.next:
                return
            await self._graph.aupdate_state(
                config,
                {"messages": [AIMessage(content=INTERRUPTED_MARKER)]},
            )
        except Exception as exc:  # noqa: BLE001
            # Broad on purpose: a missing marker is cosmetic, a raised one would
            # mask the cancellation.
            logger.warning(
                "could not close the interrupted turn (%s)", type(exc).__name__
            )

    async def _run_turn(self, utterance: Utterance) -> None:
        """Answer one utterance, then report how long each stage took.

        Args:
            utterance: The completed speech segment to act on.
        """
        marks = StageMarks(speech_end=monotonic_now())
        # None until `_answer` decides. None means the turn never got as far as a
        # decision (failed transcription, timeout, cancellation), which keeps
        # today's timing behaviour for those cases.
        outcome: TurnOutcome | None = None
        try:
            # turn_span() owns the app-root span: without it there is nothing for
            # enrich_trace and stamp_voice_timing to write onto.
            with turn_span():
                # INVARIANT: every marker below is written while the app-root
                # span is CURRENT. `mark_turn_failed`, `mark_turn_cancelled` and
                # `stamp_voice_timing` all go through `update_current_span`,
                # which targets whichever observation is current — and `_answer`
                # opens and closes its `stt`/`tts` child spans before it returns,
                # so the app-root span is current again by the time this code
                # runs. A child span left open here would take the failure marker
                # and the stage timings onto itself, leaving a crashed turn
                # looking healthy.
                try:
                    outcome = await self._answer(utterance, marks)
                except asyncio.CancelledError:
                    # Barge-in unwinds the turn by cancelling this task. CancelledError
                    # derives from BaseException, so the broad handler below never
                    # sees it — it must be caught explicitly to stamp the interruption.
                    # Re-raised, never swallowed: swallowing it would leave the
                    # cancelled task alive instead of unwinding it.
                    mark_turn_cancelled("barge-in", channel="voice")
                    raise
                except TimeoutError:
                    mark_turn_failed("voice turn timed out", channel="voice")
                    logger.warning(
                        "voice turn timed out after %.0fs",
                        settings.graph_timeout_seconds,
                    )
                    await self._send({"type": "error", "detail": "that took too long"})
                except Exception as exc:  # noqa: BLE001
                    # Broad on purpose. The provider adapters turn their own
                    # failures into results, so reaching here means a bug — and a
                    # bug must not end the conversation.
                    mark_turn_failed(type(exc).__name__, channel="voice")
                    logger.warning("voice turn failed (%s)", type(exc).__name__)
                    await self._send({"type": "error", "detail": "I could not answer"})
                finally:
                    # Stamped while the span is still open: the SDK writes the
                    # metadata onto the span that is current when the context is
                    # entered, so a later call would go nowhere.
                    stamp_voice_timing(marks.deltas_ms())
                    if outcome is not None:
                        stamp_voice_outcome(outcome, self._wake_lead_ms(utterance))
        finally:
            # `done` is the last audio byte leaving; on a failed turn it is when
            # the turn gave up.
            marks.done = marks.done or monotonic_now()
            if outcome not in {"wake_only", "cancel"}:
                # A bare wake word and a cancel phrase do real work (STT) but are
                # not turns; averaging them into turn latency would make the
                # numbers describe a pipeline that did not run.
                self.turn_timings.append(marks.deltas_ms())
            log_stage_marks(logger, marks)
            # A turn that never reached the graph's END left its question in the
            # checkpointer; close it so the thread stays question-answer balanced.
            await self._close_interrupted_turn()

    def _wake_lead_ms(self, utterance: Utterance) -> float | None:
        """How long after the last detection the segment started, in ms.

        Negative is the one-breath path: the phrase was recognised after the user
        had already begun speaking. None means no detection has been recorded.

        Args:
            utterance: The segment that became this turn.

        Returns:
            Milliseconds, or None when no detection has been recorded.
        """
        if self._last_detection_at is None:
            return None
        return (utterance.started_at - self._last_detection_at) * 1000.0

    async def _answer(
        self, utterance: Utterance, marks: StageMarks
    ) -> TurnOutcome | None:
        """Transcribe the utterance, stream the reply and speak it as it forms.

        Args:
            utterance: The completed speech segment to transcribe.
            marks: Stage marks to fill in as the turn progresses.

        Returns:
            The turn's outcome, or None when it stopped before a decision (failed
            or empty transcription). `wake_only` and `cancel` never reach the
            graph and never send a transcript frame.
        """
        bias = self._stt_bias()
        # `Utterance` carries the speech it detected (`detected_s`), not the audio
        # it hands over — pre-roll and tail pad included — so the submitted length
        # is derived from the PCM itself. The transcript's own `duration_s` agrees,
        # but it only exists AFTER the call and the span wants its input up front.
        audio_ms = duration_s(utterance.pcm, settings.voice_input_sample_rate) * 1000.0
        with child_observation(
            "stt",
            input=f"lang={bias or 'auto'} audio_ms={audio_ms:.1f}",
        ) as stt_span:
            transcript = await self._transcriber.transcribe(utterance.pcm, bias)
            marks.stt_done = monotonic_now()
            if stt_span is not None:
                if transcript.ok:
                    stt_span.update(
                        metadata=stt_attributes(
                            transcript, model=settings.voice_stt_model
                        ),
                        output=transcript.text[:MAX_TRACE_IO_CHARS],
                    )
                else:
                    # No output on a failure: an empty output would read as
                    # "transcribed to nothing", which is the one thing that did not
                    # happen. The error frame below is untouched, so this changes
                    # observability and never behaviour.
                    stt_span.update(
                        metadata=stt_attributes(
                            transcript, model=settings.voice_stt_model
                        ),
                        level="ERROR",
                        status_message=str(transcript.error)[:MAX_STATUS_MESSAGE_CHARS],
                    )
        if not transcript.ok:
            logger.warning("voice transcription failed (%s)", transcript.error)
            await self._send({"type": "error", "detail": "I did not catch that"})
            return None
        if transcript.is_empty:
            # Silence, noise, or an invented phrase the adapter already dropped:
            # answering nothing is better than answering a hallucination.
            return None

        spoken_text = transcript.text
        if self._wake is not None:
            spoken_text = strip_wake_phrase(spoken_text, self._wake_phrase)
            if not spoken_text:
                # The bare wake word. Re-arm so the command can follow, and send
                # no transcript: there is nothing to show and no turn to run.
                logger.info("wake word heard without a command; window re-armed")
                self._gate.arm()
                return "wake_only"
            if is_cancel_phrase(spoken_text):
                # "Hey Jarvis, stop": the detection already cancelled the answer,
                # and a graph turn here would answer "stop" as if it were a
                # question. Close without touching the thread.
                logger.info("cancel phrase heard (%r); no turn", spoken_text)
                return "cancel"
        await self._send({"type": "transcript", "text": spoken_text})

        initial: dict[str, Any] = {
            **build_initial_state(
                user_input=transcript.text,
                session_id=self._session_id,
                channel="voice",
                device_id=self._device_id,
            ),
            "stt_lang": bias,
        }
        config = build_run_config(self._session_id, channel="voice")
        splitter = SentenceSplitter(
            min_chars=settings.voice_tts_min_chunk_chars,
            max_chars=settings.voice_tts_max_chunk_chars,
            first_chunk_min_chars=settings.voice_tts_first_chunk_min_chars,
            first_chunk_max_chars=settings.voice_tts_first_chunk_max_chars,
        )
        resampler = StreamingResampler(PCM_SAMPLE_RATE, self._output_rate)
        final: dict[str, Any] | None = None
        # 1-based position of the sentence being spoken, so the `tts` spans read
        # in speaking order. A local rather than a per-turn counter on `self`:
        # only this method knows the order sentences leave the splitter, and an
        # attribute would outlive a barge-in and misnumber the next turn.
        sentence_index = 0

        # Same cooperative caveat as the text routes: this bounds the turn, it
        # cannot abort a model call already running on its own thread.
        async with asyncio.timeout(settings.graph_timeout_seconds):
            async for event in self._graph.astream_events(
                initial, config=config, version="v2"
            ):
                lang = _detected_lang(event)
                if lang is not None:
                    self._last_lang = lang
                    continue
                state = _final_state(event)
                if state is not None:
                    final = state
                    continue
                text = _responder_token(event)
                if not text:
                    continue
                if marks.llm_first_token is None:
                    marks.llm_first_token = monotonic_now()
                await self._send({"type": "token", "content": text})
                for sentence in splitter.push(text):
                    sentence_index += 1
                    await self._speak(sentence, resampler, marks, index=sentence_index)
            for sentence in splitter.flush():
                sentence_index += 1
                await self._speak(sentence, resampler, marks, index=sentence_index)
            tail = resampler.flush()
            if tail:
                await self._send(tail)
            if final is not None:
                enrich_trace(final)

        marks.done = monotonic_now()
        await self._send({"type": "audio_end"})
        return "answer"

    async def _speak(
        self,
        text: str,
        resampler: StreamingResampler,
        marks: StageMarks,
        *,
        index: int,
    ) -> None:
        """Synthesize one sentence and stream its audio to the client.

        One `tts` child span per sentence is what turns the flat stage timings into
        a waterfall: the second sentence is synthesised while the first is still
        playing, and only an interval per sentence shows that.

        Its `ttfb_ms` attribute and the turn's `tts_ttfb_ms` mark pick up the same
        first PCM byte from different origins — this span starts when `_speak` is
        entered, the mark when the first token arrived — and the SDK's own span
        duration comes from a different clock than `monotonic_now()`. For the
        first sentence they should agree within a few milliseconds; a large
        divergence is a signal about where the pipeline waited (text held back by
        the splitter, a span opened late), not a bug to hide.

        A barge-in cancels the turn at an `await` inside the loop below. The span
        still closes — the context manager ends it on the way out, and
        `CancelledError` derives from `BaseException`, so nothing here catches it —
        it simply closes without the attributes written after the loop.

        Args:
            text: The sentence to speak.
            resampler: Per-turn converter to the negotiated output rate.
            marks: Stage marks, stamped on the first audio byte.
            index: 1-based position of this sentence in the turn.
        """
        voice = voice_for(self._last_lang)
        with child_observation("tts", input=text[:MAX_TRACE_IO_CHARS]) as tts_span:
            started = monotonic_now()
            first_chunk_at: float | None = None
            sent_bytes = 0
            async for chunk in self._synthesizer.synthesize(text, voice):
                if marks.tts_first_byte is None:
                    marks.tts_first_byte = monotonic_now()
                if first_chunk_at is None:
                    first_chunk_at = monotonic_now()
                audio = resampler.process(chunk)
                if audio:
                    # Counted AFTER resampling so `bytes` and `audio_ms` describe
                    # the same buffer at the same rate. The resampler holds a few
                    # milliseconds back as lookahead (flushed at the end of the
                    # turn), so a sentence can come out a couple of ms short.
                    sent_bytes += len(audio)
                    await self._send(audio)
            if tts_span is not None:
                # A stream that yielded nothing has no first chunk to time, and a
                # fabricated duration would be worse than a zero.
                ttfb_ms = (
                    (first_chunk_at - started) * 1000.0
                    if first_chunk_at is not None
                    else 0.0
                )
                tts_span.update(
                    metadata=tts_attributes(
                        text,
                        voice=voice,
                        model=settings.voice_tts_model,
                        index=index,
                        ttfb_ms=ttfb_ms,
                        byte_count=sent_bytes,
                        # The NEGOTIATED rate, not the setting's default: a 16 kHz
                        # device plays these bytes at 16 kHz, and the 24 kHz default
                        # would report every sentence 1.5x too long.
                        output_rate=self._output_rate,
                    )
                )

    def _stt_bias(self) -> str | None:
        """Language hint for this turn's transcription.

        Returns:
            The language the previous turn resolved to, or None when hinting is
            switched off or nothing has been resolved yet.
        """
        if not settings.voice_stt_language_hint:
            return None
        return self._last_lang
