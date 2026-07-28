/**
 * Dark theme for Work Overview.
 *
 * The timeline is the whole app, so the surface palette is deliberately narrow
 * and low-contrast: three near-black steps for chrome/panel/raised, with all
 * remaining contrast budget spent on the marks themselves. Accent hues are
 * assigned by meaning (a session, a collection, an image, a voice note), never
 * decoratively — colour is the only way to tell lanes apart at low zoom.
 */
import { createTheme, type MantineColorsTuple } from '@mantine/core'

// Cool slate rather than pure grey: against it the accent hues stay legible at
// the low saturations this much screen area demands.
const slate: MantineColorsTuple = [
  '#c9ced6', '#aab2bd', '#8d96a4', '#71798a', '#586070',
  '#434a58', '#333944', '#252a33', '#181c22', '#0b0c0e',
]

export const SURFACE = {
  chrome: '#0b0c0e',
  panel: '#12151a',
  raised: '#181c22',
  line: '#252a33',
  lineStrong: '#333944',
} as const

/** One hue per lane meaning. Kept here so nothing invents its own. */
export const LANE = {
  session: '#4c8dff',
  label: '#a879ff',
  collection: '#2fb8a2',
  capture: '#e0b341',
  image: '#7ea6d8',
  voice: '#e07a5f',
  /** approximate positions read as an outline, never a filled mark */
  approximate: '#5b6472',
} as const

export const theme = createTheme({
  primaryColor: 'slate',
  primaryShade: { dark: 4 },
  colors: { slate },
  fontFamily:
    'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
  fontFamilyMonospace:
    'ui-monospace, "Cascadia Mono", "JetBrains Mono", Consolas, monospace',
  defaultRadius: 'sm',
  // A dense tool viewed maximized: the default scale wastes vertical space that
  // the timeline can use for more lanes.
  fontSizes: {
    xs: '10.5px', sm: '12px', md: '13px', lg: '15px', xl: '18px',
  },
  spacing: {
    xs: '6px', sm: '9px', md: '13px', lg: '18px', xl: '26px',
  },
  headings: {
    sizes: {
      h1: { fontSize: '19px', fontWeight: '600' },
      h2: { fontSize: '16px', fontWeight: '600' },
      h3: { fontSize: '13px', fontWeight: '600' },
      h4: { fontSize: '12px', fontWeight: '600' },
      h5: { fontSize: '11px', fontWeight: '600' },
      h6: { fontSize: '10.5px', fontWeight: '600' },
    },
  },
  components: {
    Tooltip: {
      defaultProps: { openDelay: 260, withArrow: true, transitionProps: { duration: 120 } },
    },
    Button: { defaultProps: { size: 'xs' } },
    ActionIcon: { defaultProps: { variant: 'subtle', size: 'md' } },
  },
})
