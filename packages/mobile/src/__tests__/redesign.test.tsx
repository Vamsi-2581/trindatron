import { fireEvent, render, screen } from '@testing-library/react-native'
import { marked, type Tokens } from 'marked'
import { Platform, StyleSheet, Text } from 'react-native'
import { KeyboardAwareScrollView } from 'react-native-keyboard-controller'
import { SafeAreaProvider } from 'react-native-safe-area-context'

import { ActionMenu, AppHeader, AppText, BottomSheet, Button, Chip, Screen, SegmentedControl, Select, SkeletonRows } from '@/components/ui'
import { baselineCapabilities, type Capabilities, type CapabilityState } from '@/features/capabilities/capabilities'
import { combinedStatus } from '@/features/capabilities/combinedStatus'
import { startSuggestions } from '@/features/chat/suggestions'
import { MAX_COLUMN, MIN_COLUMN, tableColumnWidths } from '@/features/chat/table'
import { uprightStyle } from '@/features/chat/upright'
import { conversationGroup, groupConversations } from '@/features/navigation/conversationGroups'
import { ACCOUNT_PLACES, visibleTools } from '@/features/navigation/tools'
import { describeDevice } from '@/features/settings/deviceName'
import { categoryLabel, productPageFor, shouldOfferAssistant } from '@/features/support/helpCenter'
import { translate, useLocaleStore } from '@/i18n'
import { initialTicketCategory, shownSupportCategory } from '@/services/api/support'
import type { Session } from '@/services/api/types'

const state = (status: CapabilityState['status']): CapabilityState => ({ available: status === 'available', status, dailyQuota: null, limits: null })

function withCapabilities(changes: Partial<Record<keyof Capabilities, CapabilityState['status']>>): Capabilities {
  const capabilities = baselineCapabilities()
  for (const [id, status] of Object.entries(changes)) capabilities[id as keyof Capabilities] = state(status)
  return capabilities
}

beforeEach(() => {
  useLocaleStore.setState({ preference: 'auto', systemLocale: 'pt', locale: 'pt' })
})

describe('conversation history groups', () => {
  // Wednesday 16 September 2026, 10:00 on this device.
  const now = new Date(2026, 8, 16, 10, 0).getTime()
  const at = (day: number, hour = 9) => {
    const date = new Date(2026, 8, day, hour, 0)
    return date.toISOString()
  }
  const session = (id: string, updatedAt: string): Session => ({
    id,
    title: id,
    project_id: null,
    agent_definition_id: null,
    is_temporary: false,
    created_at: updatedAt,
    updated_at: updatedAt,
  })

  it('goes by calendar day, not by 24 hours', () => {
    expect(conversationGroup(at(16, 0), now)).toBe('today')
    // 23:00 yesterday is less than a day ago, and still yesterday.
    expect(conversationGroup(at(15, 23), now)).toBe('yesterday')
    expect(conversationGroup(at(10), now)).toBe('week')
    expect(conversationGroup(at(9), now)).toBe('older')
  })

  it('keeps the groups in reading order, newest first inside each, and leaves empty ones out', () => {
    const groups = groupConversations([session('old', at(1)), session('morning', at(16, 8)), session('late', at(16, 9)), session('mon', at(14))], now)
    expect(groups.map((group) => group.key)).toEqual(['today', 'week', 'older'])
    expect(groups[0]!.sessions.map((item) => item.id)).toEqual(['late', 'morning'])
  })
})

describe('side menu tools', () => {
  it('shows a place while its capability is part of the product, even outside the plan', () => {
    const keys = visibleTools(withCapabilities({ browser_control: 'plan', computer_control: 'plan' })).map((tool) => tool.key)
    expect(keys).toContain('computerUse')
    expect(keys).toContain('scheduled')
  })

  it('never offers a place the Core says does not exist', () => {
    const keys = visibleTools(withCapabilities({ browser_control: 'not_integrated', computer_control: 'not_integrated' })).map((tool) => tool.key)
    expect(keys).not.toContain('computerUse')
    // Agendados needs no capability of its own.
    expect(keys).toContain('scheduled')
  })
})

describe('new conversation suggestions', () => {
  it('only offers what this account can do right now, and at most four', () => {
    expect(startSuggestions(baselineCapabilities()).map((item) => item.key)).toEqual(['write', 'explain'])

    const all = startSuggestions(withCapabilities({ image_generation: 'available', video_generation: 'available', agent_mode: 'available', chat_attachments: 'available' }))
    expect(all.map((item) => item.key)).toEqual(['image', 'video', 'agent', 'document'])

    // In the plan above this one is not "available": no suggestion to buy your way into.
    expect(startSuggestions(withCapabilities({ image_generation: 'plan' })).map((item) => item.key)).not.toContain('image')
  })
})

describe('combined status (Computer Use)', () => {
  it('works when either works; an outage beats the plan; the plan beats not integrated', () => {
    expect(combinedStatus(state('plan'), state('available'))).toBe('available')
    expect(combinedStatus(state('plan'), state('unavailable'))).toBe('unavailable')
    expect(combinedStatus(state('plan'), undefined)).toBe('plan')
    expect(combinedStatus(undefined, undefined)).toBe('not_integrated')
  })
})

describe('device names', () => {
  it('reads the browser and the system, without versions', () => {
    const edge = describeDevice('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36 Edg/128.0')
    expect(edge).toMatchObject({ kind: 'browser', browser: 'Edge', platform: 'windows' })
    const safari = describeDevice('Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1')
    expect(safari).toMatchObject({ kind: 'browser', browser: 'Safari', platform: 'ios' })
    expect(describeDevice('Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Mobile Safari/537.36')).toMatchObject({
      browser: 'Chrome',
      platform: 'android',
    })
  })

  it('recognises the app by its HTTP client, and admits when nothing is recognisable', () => {
    expect(describeDevice('okhttp/4.12.0')).toMatchObject({ kind: 'app', platform: 'android' })
    expect(describeDevice('TRINDATRON/1 CFNetwork/1498 Darwin/24.0.0')).toMatchObject({ kind: 'app', platform: 'ios' })
    expect(describeDevice('curl/8.0')).toMatchObject({ kind: 'unknown', platform: 'unknown' })
    expect(describeDevice(null)).toMatchObject({ kind: 'unknown' })
  })
})

describe('account places', () => {
  it('Usage, Plans and Billing each have their own screen', () => {
    expect(ACCOUNT_PLACES.map((place) => [place.key, place.href])).toEqual([
      ['usage', '/usage'],
      ['plans', '/plans'],
      ['billing', '/billing'],
    ])
  })

  it('every "see plans" opens the plans, never Usage, Billing or the Help Center', () => {
    const fs = jest.requireActual<typeof import('fs')>('fs')
    const path = jest.requireActual<typeof import('path')>('path')
    const root = path.resolve(__dirname, '../..')
    const files = [
      'app/(app)/chat/[sessionId].tsx',
      'app/(app)/usage.tsx',
      'app/(app)/computer-use.tsx',
      'app/(app)/create.tsx',
      'src/features/tips/tips.ts',
    ]
    for (const file of files) {
      const source = fs.readFileSync(path.join(root, file), 'utf8')
      expect(source).toMatch(/PLANS_HREF/)
      expect(source).not.toMatch(/['"]\/(billing|usage|support)['"]/)
    }
  })
})

describe('Screen layout', () => {
  it('reserves the navigation bar inside the scroll and keeps the keyboard off the content', () => {
    const insets = { top: 24, bottom: 44, left: 0, right: 0 }
    render(
      <SafeAreaProvider initialMetrics={{ frame: { x: 0, y: 0, width: 400, height: 800 }, insets }}>
        <Screen>
          <Text>fim da lista</Text>
        </Screen>
      </SafeAreaProvider>,
    )
    // The last row must land above the bar: the room it takes is content padding, not a cut-short scroll.
    const scroll = screen.UNSAFE_getByType(KeyboardAwareScrollView)
    expect(StyleSheet.flatten(scroll.props.contentContainerStyle).paddingBottom).toBeGreaterThanOrEqual(insets.bottom)
    expect(screen.getByText('fim da lista')).toBeTruthy()
  })
})

describe('help center', () => {
  it('the Plans and Billing categories point to the real pages, other categories do not', () => {
    expect(productPageFor('plans')?.href).toBe('/plans')
    expect(productPageFor('billing')?.href).toBe('/billing')
    expect(productPageFor('voice')).toBeNull()
    expect(productPageFor(null)).toBeNull()
  })

  it('offers the assistant only after a finished search found no article', () => {
    expect(shouldOfferAssistant('', 0, false)).toBe(false)
    expect(shouldOfferAssistant('   ', 0, false)).toBe(false)
    expect(shouldOfferAssistant('reembolso', 0, true)).toBe(false)
    expect(shouldOfferAssistant('reembolso', 2, false)).toBe(false)
    expect(shouldOfferAssistant('reembolso', 0, false)).toBe(true)
  })

  it('names a category the app has no words for by the Core id', () => {
    expect(categoryLabel('computer_use_beta', (key) => key)).toBe('computer use beta')
    expect(categoryLabel('billing', () => 'Cobrança')).toBe('Cobrança')
  })
})

describe('shared UI', () => {
  it('Chip says whether it is chosen', () => {
    const onPress = jest.fn()
    render(<Chip label="Imagens" role="radio" selected onPress={onPress} />)
    const chip = screen.getByRole('radio', { name: 'Imagens' })
    expect(chip.props.accessibilityState).toMatchObject({ selected: true, checked: true })
    fireEvent.press(chip)
    expect(onPress).toHaveBeenCalled()
  })

  it('AppHeader has one back action, a header title and the trailing actions', () => {
    const onBack = jest.fn()
    render(<AppHeader title="Configurações" onBack={onBack} trailing={<Text>extra</Text>} />)
    expect(screen.getByRole('header', { name: 'Configurações' })).toBeTruthy()
    fireEvent.press(screen.getByLabelText('Voltar'))
    expect(onBack).toHaveBeenCalled()
    expect(screen.getByText('extra')).toBeTruthy()
  })

  it('ActionMenu fires the chosen action through the native menu', () => {
    const onSelect = jest.fn()
    render(<ActionMenu label="Ações da conversa" items={[{ id: 'delete', title: 'Excluir conversa', destructive: true }]} onSelect={onSelect} />)
    fireEvent.press(screen.getByLabelText('Ações da conversa'))
    fireEvent.press(screen.getByLabelText('Excluir conversa'))
    expect(onSelect).toHaveBeenCalledWith('delete')
  })

  it('ActionMenu on the Web opens the same actions in a sheet', () => {
    const os = Platform.OS
    Object.defineProperty(Platform, 'OS', { value: 'web', configurable: true })
    try {
      const onSelect = jest.fn()
      render(<ActionMenu label="Ações da conversa" items={[{ id: 'delete', title: 'Excluir conversa', destructive: true }]} onSelect={onSelect} />)
      expect(screen.queryByText('Excluir conversa')).toBeNull()
      fireEvent.press(screen.getByLabelText('Ações da conversa'))
      fireEvent.press(screen.getByText('Excluir conversa'))
      expect(onSelect).toHaveBeenCalledWith('delete')
    } finally {
      Object.defineProperty(Platform, 'OS', { value: os, configurable: true })
    }
  })

  describe('on Android, no Jetpack Compose host (expo/expo#47625 crashed a physical device)', () => {
    const os = Platform.OS
    beforeEach(() => Object.defineProperty(Platform, 'OS', { value: 'android', configurable: true }))
    afterEach(() => Object.defineProperty(Platform, 'OS', { value: os, configurable: true }))

    it('BottomSheet is a React Native modal that closes from its scrim', () => {
      const onClose = jest.fn()
      render(
        <BottomSheet visible onClose={onClose} title="Idioma">
          <Text>conteúdo</Text>
        </BottomSheet>,
      )
      expect(screen.queryByTestId('bottom-sheet')).toBeNull()
      expect(screen.getByText('conteúdo')).toBeTruthy()
      // The scrim sits beside the modal panel, so assistive technology does not reach it (the back action closes); a tap does.
      fireEvent.press(screen.getByLabelText('Fechar', { includeHiddenElements: true }))
      expect(onClose).toHaveBeenCalled()
    })

    it('ActionMenu opens its actions in the sheet and fires the chosen one', () => {
      const onSelect = jest.fn()
      render(<ActionMenu label="Ações da conversa" items={[{ id: 'delete', title: 'Apagar conversa', destructive: true }]} onSelect={onSelect} />)
      expect(screen.queryByText('Apagar conversa')).toBeNull()
      fireEvent.press(screen.getByLabelText('Ações da conversa'))
      fireEvent.press(screen.getByText('Apagar conversa'))
      expect(onSelect).toHaveBeenCalledWith('delete')
    })

    it('SegmentedControl selects by value and says which one is chosen', () => {
      const onChange = jest.fn()
      render(
        <SegmentedControl
          accessibilityLabel="Estilo"
          options={[
            { value: 'concise', label: 'Direto' },
            { value: 'balanced', label: 'Equilibrado' },
          ]}
          value="balanced"
          onChange={onChange}
        />,
      )
      expect(screen.getByRole('radio', { name: 'Equilibrado' }).props.accessibilityState).toMatchObject({ checked: true })
      fireEvent.press(screen.getByRole('radio', { name: 'Equilibrado' }))
      expect(onChange).not.toHaveBeenCalled()
      fireEvent.press(screen.getByRole('radio', { name: 'Direto' }))
      expect(onChange).toHaveBeenCalledWith('concise')
    })

    it('Select shows the current value and picks another from the sheet', () => {
      const onChange = jest.fn()
      render(
        <Select
          accessibilityLabel="Idioma das respostas"
          options={[
            { value: 'pt', label: 'Português' },
            { value: 'en', label: 'English' },
          ]}
          value="pt"
          onChange={onChange}
        />,
      )
      fireEvent.press(screen.getByRole('button', { name: 'Idioma das respostas, Português' }))
      fireEvent.press(screen.getByLabelText('English'))
      expect(onChange).toHaveBeenCalledWith('en')
    })
  })

  it('SegmentedControl and Select speak in values', () => {
    const onStyle = jest.fn()
    const onLanguage = jest.fn()
    render(
      <>
        <SegmentedControl
          options={[
            { value: 'concise', label: 'Conciso' },
            { value: 'detailed', label: 'Detalhado' },
          ]}
          value="concise"
          onChange={onStyle}
        />
        <Select
          options={[
            { value: 'pt', label: 'Português' },
            { value: 'en', label: 'English' },
          ]}
          value="pt"
          onChange={onLanguage}
        />
      </>,
    )
    fireEvent.press(screen.getByLabelText('Detalhado'))
    fireEvent.press(screen.getByLabelText('English'))
    expect(onStyle).toHaveBeenCalledWith('detailed')
    expect(onLanguage).toHaveBeenCalledWith('en')
  })

  it('SkeletonRows announces loading', () => {
    render(<SkeletonRows count={3} />)
    expect(screen.getByRole('progressbar', { name: 'Carregando…' })).toBeTruthy()
  })
})

describe('chat states in the inverted list', () => {
  it('is turned upright with the same transform the list uses on each platform', () => {
    // Android inverts on both axes (VirtualizedList verticallyInverted), so undoing only Y mirrored every empty,
    // loading and error state left-to-right on a physical device.
    expect(uprightStyle('android')).toEqual({ transform: [{ scale: -1 }] })
    expect(uprightStyle('ios')).toEqual({ transform: [{ scaleY: -1 }] })
    expect(uprightStyle('web')).toEqual({ transform: [{ scaleY: -1 }] })
  })
})

describe('Markdown table columns', () => {
  const widthsOf = (markdown: string) => {
    const table = marked.lexer(markdown, { gfm: true })[0] as Tokens.Table
    return tableColumnWidths([table.header, ...table.rows].map((row) => row.map((cell) => cell.text)))
  }

  it('gives a column one width, taken from its longest cell in any row', () => {
    const widths = widthsOf(
      ['| Plano | Limite mensal de mensagens |', '| --- | --- |', '| Free | 20 |', '| Pro | 1000 |'].join('\n'),
    )
    // Column 1 is sized by its header, column 0 by nothing longer than "Plano" — and neither changes per row.
    const [plan, limit] = widths
    expect(widths).toHaveLength(2)
    expect(limit).toBeGreaterThan(plan ?? 0)
    expect(plan).toBe(MIN_COLUMN)
  })

  it('stops growing at the maximum so one long cell does not push the table off screen', () => {
    const long = 'x'.repeat(400)
    expect(widthsOf(`| A | B |\n| --- | --- |\n| ${long} | b |`)[0]).toBe(MAX_COLUMN)
  })

  it('covers a ragged row rather than dropping its extra cell', () => {
    // marked trims a row to the header, but the widths must not depend on that: a missing cell is a width, not a crash.
    expect(tableColumnWidths([['A', 'B'], ['1', '2', '3'], ['4']])).toHaveLength(3)
  })
})

describe('visual rules from the Android audit', () => {
  it('writes labels the way they are said, never in forced capitals', () => {
    render(<AppText variant="label">Plano atual</AppText>)
    const style = StyleSheet.flatten(screen.getByText('Plano atual').props.style)
    expect(style.textTransform).toBeUndefined()
  })

  it('keeps a disabled button legible instead of fading it to half', () => {
    render(<Button label="Criar team e pagar" disabled onPress={jest.fn()} />)
    const button = screen.getByRole('button', { name: 'Criar team e pagar' })
    expect(button.props.accessibilityState).toMatchObject({ disabled: true })
    expect(StyleSheet.flatten(button.props.style).opacity).toBeUndefined()
  })

  it('does not lead Support with voice, creations or privacy, and files a hidden suggestion under "other"', () => {
    expect(['account', 'voice', 'creations', 'privacy', 'billing'].filter(shownSupportCategory)).toEqual(['account', 'billing'])
    expect(initialTicketCategory('voice')).toBe('other')
    expect(initialTicketCategory('billing')).toBe('billing')
    expect(initialTicketCategory(null)).toBe('other')
  })

  it('names the team feature "Team" in Portuguese', () => {
    expect(translate('pt', 'nav.team')).toBe('Team')
    expect(translate('pt', 'nav.support')).toBe('Suporte')
  })
})
