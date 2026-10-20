'use client'

import { useParams } from 'next/navigation'
import { SECTION_COMPONENTS } from '@/components/settings/sections'

export default function SettingsSectionPage() {
  const { section } = useParams<{ section: string }>()
  const Section = SECTION_COMPONENTS[section]
  if (!Section) {
    return <p className="text-sm text-text-muted">Esta seção não existe.</p>
  }
  return <Section />
}
