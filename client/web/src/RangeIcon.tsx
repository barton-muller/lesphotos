/**
 * "Extend the selection to here": an anchor dot, a dotted trail, an arrowhead.
 *
 * Drawn rather than lettered so the affordance survives a tile small enough
 * that a text pill would cover the photo, and so it reads the same on every
 * platform (the reason the ★ glyph became StarIcon).
 *
 * The dotted middle is what carries the meaning - a solid arrow would say
 * "go there", where this says "everything from the anchor through to here".
 */
export function RangeIcon({
  size = 16,
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
      {/* anchor */}
      <circle cx="3.5" cy="12" r="2.2" fill={color} stroke="none" />
      {/* trail */}
      <path d="M8 12h1M12 12h1M16 12h1" />
      {/* arrowhead */}
      <path d="M18.5 8.5L22 12l-3.5 3.5" />
    </svg>
  )
}
