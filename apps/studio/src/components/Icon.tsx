/* Inline SVG icons.
 *
 * Hand-drawn on a 24-unit grid rather than pulled from a library: the whole set is under 2 KB,
 * there is no dependency to keep current, and every icon inherits `currentColor` so it picks up
 * the surrounding state automatically. An icon font or an icon package would be more code, more
 * bytes, and one more thing to update.
 */

export type IconName =
  | 'grid' | 'phone' | 'list' | 'user' | 'book' | 'flow' | 'hash' | 'gauge' | 'shield'
  | 'search' | 'plus' | 'trash' | 'mic' | 'mic-off' | 'send' | 'check' | 'x' | 'alert'
  | 'sparkle' | 'clock' | 'volume' | 'chevron'

const PATHS: Record<IconName, string> = {
  grid: 'M3 3h7v7H3zM14 3h7v7h-7zM14 14h7v7h-7zM3 14h7v7H3z',
  phone: 'M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2 4.2 2 2 0 0 1 4 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8a2 2 0 0 1-.5 2.1L8.1 9.9a16 16 0 0 0 6 6l1.3-1.1a2 2 0 0 1 2.1-.5c.9.3 1.8.6 2.8.7a2 2 0 0 1 1.7 2z',
  list: 'M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01',
  user: 'M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z',
  book: 'M4 19.5A2.5 2.5 0 0 1 6.5 17H20M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z',
  flow: 'M5 3h5v5H5zM14 16h5v5h-5zM5 16h5v5H5zM7.5 8v8M12.5 5h4a2 2 0 0 1 2 2v9',
  hash: 'M4 9h16M4 15h16M10 3L8 21M16 3l-2 18',
  gauge: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 12l4-4',
  shield: 'M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z',
  search: 'M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM21 21l-4.3-4.3',
  plus: 'M12 5v14M5 12h14',
  trash: 'M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6',
  mic: 'M12 15a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3zM19 11a7 7 0 0 1-14 0M12 18v4',
  'mic-off': 'M2 2l20 20M9 9v3a3 3 0 0 0 5 2M15 9.3V6a3 3 0 0 0-5.7-1.3M19 11a7 7 0 0 1-1 3.5M5 11a7 7 0 0 0 10.5 6M12 18v4',
  send: 'M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z',
  check: 'M20 6L9 17l-5-5',
  x: 'M18 6L6 18M6 6l12 12',
  alert: 'M12 9v4M12 17h.01M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z',
  sparkle: 'M12 3l1.9 5.6L19.5 10l-5.6 1.9L12 17.5l-1.9-5.6L4.5 10l5.6-1.4L12 3zM19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8L19 15z',
  clock: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v5l3 2',
  volume: 'M11 5L6 9H2v6h4l5 4V5zM15.5 8.5a5 5 0 0 1 0 7M19 5a10 10 0 0 1 0 14',
  chevron: 'M9 18l6-6-6-6',
}

interface Props {
  name: IconName
  className?: string
  size?: number
}

export function Icon({ name, className, size = 16 }: Props) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={PATHS[name]} />
    </svg>
  )
}
