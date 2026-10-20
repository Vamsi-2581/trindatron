/**
 * The Settings map. Each id is a real section rendered by
 * components/settings/sections.tsx from data the Core returns — none is a
 * placeholder. Where something is not integrated yet, its own section says so.
 */
export interface SettingsSection {
  id: string
  label: string
}

export interface SettingsGroup {
  label: string
  sections: SettingsSection[]
}

export const SETTINGS_GROUPS: SettingsGroup[] = [
  {
    label: 'Personalizar',
    sections: [
      { id: 'skills', label: 'Habilidades' },
      { id: 'connectors', label: 'Conectores' },
      { id: 'plugins', label: 'Plugins' },
    ],
  },
  {
    label: 'Configurações',
    sections: [
      { id: 'general', label: 'Geral' },
      { id: 'account', label: 'Conta' },
      { id: 'privacy', label: 'Privacidade' },
      { id: 'billing', label: 'Cobrança' },
      { id: 'usage', label: 'Uso' },
      { id: 'capabilities', label: 'Capacidades' },
      { id: 'memory', label: 'Memória' },
      { id: 'computer-use', label: 'Computer Use' },
      { id: 'scheduled', label: 'Agendados' },
      { id: 'work', label: 'Trabalho' },
      { id: 'voice', label: 'Voz' },
      { id: 'media', label: 'Mídia' },
    ],
  },
  {
    label: 'Aplicativo',
    sections: [
      { id: 'desktop', label: 'Desktop' },
      { id: 'extensions', label: 'Extensões' },
    ],
  },
]

export const SETTINGS_SECTION_IDS: string[] = SETTINGS_GROUPS.flatMap((group) => group.sections.map((s) => s.id))
export const DEFAULT_SETTINGS_SECTION = 'general'
