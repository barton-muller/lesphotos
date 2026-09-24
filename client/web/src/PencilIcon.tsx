/** Rename affordance - a plain pencil, smaller and less noisy than the word "Rename". */
export function PencilIcon({
  size = 14,
  color = 'currentColor',
}: {
  size?: number
  color?: string
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke={color}
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      style={{ display: 'block', flexShrink: 0 }}
    >
      <path d="M14.5 4.5l5 5L8 21H3v-5z" />
      <path d="M13 6l5 5" />
    </svg>
  )
}
