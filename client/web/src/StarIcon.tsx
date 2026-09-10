/**
 * Star as inline SVG, not a ★/☆ text glyph.
 *
 * The text glyphs render from whatever font each platform picks for them, so
 * the same markup looked visibly different between desktop Safari and mobile
 * Chrome - different weight, different proportions, and on some Android builds
 * a colour emoji star. An SVG path is identical everywhere.
 *
 * It also makes filled-vs-outline a property rather than two different
 * characters, which is what the "starred only" toggle needs.
 */
export function StarIcon({
  filled,
  size = 18,
  color = 'currentColor',
}: {
  filled: boolean
  size?: number
  color?: string
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill={filled ? color : 'none'}
      stroke={color}
      strokeWidth={filled ? 0 : 2}
      strokeLinejoin="round"
      aria-hidden="true"
      style={{ display: 'block', flexShrink: 0 }}
    >
      <path d="M12 2.5l2.9 6.06 6.6.92-4.8 4.6 1.17 6.52L12 17.5l-5.87 3.1L7.3 14.08l-4.8-4.6 6.6-.92L12 2.5z" />
    </svg>
  )
}
