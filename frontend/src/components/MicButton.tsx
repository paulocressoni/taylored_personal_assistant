// frontend/src/components/MicButton.tsx
// Microphone toggle for the voice session. Holds no state of its own: the phase
// comes from useVoiceSession, and this component only turns it into a word and a
// colour, so the button doubles as the status readout.
import type { VoicePhase } from '../hooks/useVoiceSession'
import { formatLatency } from '../lib/voice'

type Props = {
  phase: VoicePhase
  latencyMs: number | null
  disabled: boolean
  onToggle: () => void
}

const LABEL: Record<VoicePhase, string> = {
  idle: 'Speak',
  listening: 'Listening',
  thinking: 'Thinking',
  speaking: 'Speaking',
}

const TITLE: Record<VoicePhase, string> = {
  idle: 'Ask by voice',
  listening: 'Listening — start talking',
  thinking: 'Working on your answer',
  speaking: 'Answering — speak over it to interrupt',
}

export default function MicButton({ phase, latencyMs, disabled, onToggle }: Props) {
  const active = phase !== 'idle'
  const latency = formatLatency(latencyMs)

  return (
    <button
      onClick={onToggle}
      disabled={disabled}
      title={TITLE[phase]}
      // aria-pressed makes the toggle state audible to a screen reader, which the
      // colour alone cannot do.
      aria-pressed={active}
      className={
        active
          ? 'flex items-center gap-2 rounded-xl bg-red-600 px-3 font-medium text-white hover:bg-red-700 disabled:opacity-50'
          : 'flex items-center gap-2 rounded-xl border border-gray-300 px-3 font-medium text-gray-700 hover:bg-gray-100 disabled:opacity-50'
      }
    >
      {LABEL[phase]}
      {/* Only meaningful between turns: while a turn runs, the label is the news. */}
      {!active && latency && <span className="text-xs text-gray-400">{latency}</span>}
    </button>
  )
}
