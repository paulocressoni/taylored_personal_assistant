// LanguageBadge.tsx
// A badge showing the language of the assistant's response,
// based on the `lang` field in the response metadata.
type Props = { lang: string | null }

// Map of language codes to human-readable labels. Add more as needed.
const LABELS: Record<string, string> = {
  en: 'English',
  de: 'Deutsch',
  'pt-BR': 'Português (BR)',
}

// Render a badge with the language label, or nothing if `lang` is null.
export default function LanguageBadge({ lang }: Props) {
  if (!lang) return null // nothing to show until the first reply arrives
  return (
    <span className="rounded-full bg-emerald-100 px-3 py-1 text-xs font-medium text-emerald-700">
      {LABELS[lang] ?? lang}
    </span>
  )
}
