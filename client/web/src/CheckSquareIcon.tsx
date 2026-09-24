/** Checked square - the icon half of the selection-count chip (Timeline.tsx). */
export function CheckSquareIcon({
  size = 15,
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
      strokeWidth={2.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      style={{ display: 'block', flexShrink: 0 }}
    >
      <rect x="4" y="4" width="16" height="16" rx="3" />
      <path d="M8.5 12l2.5 2.5 5-5" />
    </svg>
  )
}
