import { redirect } from 'next/navigation'
import { DEFAULT_SETTINGS_SECTION } from '@/components/settings/nav'

export default function SettingsIndex() {
  redirect(`/settings/${DEFAULT_SETTINGS_SECTION}`)
}
