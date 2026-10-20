import { fireEvent, render, screen, waitFor } from '@testing-library/react-native'
import * as Clipboard from 'expo-clipboard'
import { Linking } from 'react-native'

import { Markdown } from '../Markdown'

jest.mock('expo-clipboard', () => ({ setStringAsync: jest.fn().mockResolvedValue(true) }))

describe('Markdown', () => {
  it('renders headings, emphasis, lists and inline code as text', () => {
    render(<Markdown content={'# Título\n\nUm **forte** e `a < b && c`.\n\n- item um\n- item dois'} />)
    expect(screen.getByText('Título')).toBeTruthy()
    expect(screen.getByText('forte')).toBeTruthy()
    expect(screen.getByText('a < b && c')).toBeTruthy()
    expect(screen.getByText('item um')).toBeTruthy()
  })

  it('renders fenced code with its language and copies it', async () => {
    const code = 'const x = 1 < 2\nconsole.log(x)'
    render(<Markdown content={'```ts\n' + code + '\n```'} />)
    expect(screen.getByText('ts')).toBeTruthy()
    expect(screen.getByText(code)).toBeTruthy()

    fireEvent.press(screen.getByLabelText('Copiar código'))
    await waitFor(() => expect(Clipboard.setStringAsync).toHaveBeenCalledWith(code))
    expect(await screen.findByLabelText('Código copiado')).toBeTruthy()
  })

  it('opens only http(s) and mailto links', () => {
    const open = jest.spyOn(Linking, 'openURL').mockResolvedValue(true)
    render(<Markdown content={'[site](https://trindatron.com) e [perigo](javascript:alert(1))'} />)

    fireEvent.press(screen.getByText('site'))
    expect(open).toHaveBeenCalledWith('https://trindatron.com')

    expect(screen.getAllByRole('link')).toHaveLength(1)
    fireEvent.press(screen.getByText('perigo'))
    expect(open).toHaveBeenCalledTimes(1)
  })

  it('never loads remote images', () => {
    render(<Markdown content={'![grafico](https://tracker.example/pixel.png)'} />)
    expect(screen.getByText('[imagem: grafico]')).toBeTruthy()
  })

  it('tolerates the half-written markdown seen while streaming', () => {
    expect(() => render(<Markdown content={'Lista:\n\n- um\n- do```py\nprint('} />)).not.toThrow()
  })
})
