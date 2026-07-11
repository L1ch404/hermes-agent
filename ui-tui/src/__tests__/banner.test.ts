import { describe, expect, it } from 'vitest'

import { logo, parseRichMarkup } from '../banner.js'
import { DEFAULT_THEME } from '../theme.js'

describe('joLink banner', () => {
  it('renders the default ASCII logo with its trailing backslash intact', () => {
    expect(logo(DEFAULT_THEME.color).map(([, text]) => text)).toEqual([
      '       _       _     _       _',
      '      (_) ___ | |   (_)_ __ | | __',
      "      | |/ _ \\| |   | | '_ \\| |/ /",
      '      | | (_) | |___| | | | |   <',
      '     _/ |\\___/|_____|_|_| |_|_|\\_\\',
      '    |__/'
    ])
  })

  it('unescapes Rich double backslashes from the Python skin payload', () => {
    expect(parseRichMarkup(String.raw`[#CD7F32]x\\[/]`)).toEqual([['#CD7F32', 'x\\']])
  })
})
