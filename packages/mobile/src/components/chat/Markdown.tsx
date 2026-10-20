import * as Clipboard from 'expo-clipboard'
import { marked, type Token, type Tokens } from 'marked'
import { memo, useMemo, type ReactNode } from 'react'
import { Linking, ScrollView, StyleSheet, Text, View, type TextStyle } from 'react-native'

import { AppText } from '@/components/ui/AppText'
import { IconButton } from '@/components/ui/Button'
import { tableColumnWidths } from '@/features/chat/table'
import { useTransientFlag } from '@/hooks/useTransientFlag'
import { useT } from '@/i18n'
import { font, radius, space, useTheme, type Palette } from '@/theme'

/**
 * Markdown → native views. `marked` only tokenises; nothing is rendered as
 * HTML, remote images are never fetched, and only http(s)/mailto links open.
 */

const SAFE_LINK = /^(https?:|mailto:)/i

const ENTITIES: Record<string, string> = { '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#39;': "'" }
export function decodeEntities(text: string): string {
  return text.replace(/&(amp|lt|gt|quot|#39);/g, (match) => ENTITIES[match] ?? match)
}

export function openSafeLink(href: string): boolean {
  if (!SAFE_LINK.test(href)) return false
  Linking.openURL(href).catch(() => undefined)
  return true
}

interface Ctx {
  colors: Palette
  base: TextStyle
  t: (key: string, params?: Record<string, string>) => string
}

function renderInline(tokens: Token[] | undefined, ctx: Ctx, key: string): ReactNode[] {
  if (!tokens) return []
  return tokens.map((token, index) => {
    const k = `${key}.${index}`
    switch (token.type) {
      case 'text': {
        const t = token as Tokens.Text
        return t.tokens ? <Text key={k}>{renderInline(t.tokens, ctx, k)}</Text> : decodeEntities(t.text)
      }
      case 'escape':
        return decodeEntities((token as Tokens.Escape).text)
      case 'strong':
        return (
          <Text key={k} style={styles.strong}>
            {renderInline((token as Tokens.Strong).tokens, ctx, k)}
          </Text>
        )
      case 'em':
        return (
          <Text key={k} style={styles.em}>
            {renderInline((token as Tokens.Em).tokens, ctx, k)}
          </Text>
        )
      case 'del':
        return (
          <Text key={k} style={styles.del}>
            {renderInline((token as Tokens.Del).tokens, ctx, k)}
          </Text>
        )
      case 'codespan':
        return (
          <Text key={k} style={[styles.codespan, { backgroundColor: ctx.colors.surface3, color: ctx.colors.textPrimary }]}>
            {decodeEntities((token as Tokens.Codespan).text)}
          </Text>
        )
      case 'br':
        return '\n'
      case 'link': {
        const link = token as Tokens.Link
        const children = renderInline(link.tokens, ctx, k)
        if (!SAFE_LINK.test(link.href)) return <Text key={k}>{children}</Text>
        return (
          <Text
            key={k}
            accessibilityRole="link"
            onPress={() => openSafeLink(link.href)}
            style={[styles.link, { color: ctx.colors.accentText }]}
          >
            {children}
          </Text>
        )
      }
      case 'image': {
        const image = token as Tokens.Image
        return (
          <Text key={k} style={{ color: ctx.colors.textMuted }}>
            {image.text ? ctx.t('markdown.imageWithAlt', { alt: image.text }) : ctx.t('markdown.image')}
          </Text>
        )
      }
      default:
        return 'raw' in token ? decodeEntities(String(token.raw)) : null
    }
  })
}

function renderBlocks(tokens: Token[], ctx: Ctx, key: string): ReactNode[] {
  return tokens.map((token, index) => {
    const k = `${key}-${index}`
    switch (token.type) {
      case 'space':
        return null
      case 'heading': {
        const heading = token as Tokens.Heading
        const size = heading.depth <= 1 ? 22 : heading.depth === 2 ? 19 : 17
        return (
          <Text key={k} selectable accessibilityRole="header" style={[ctx.base, styles.heading, { fontSize: size, lineHeight: size + 8 }]}>
            {renderInline(heading.tokens, ctx, k)}
          </Text>
        )
      }
      case 'paragraph':
        return (
          <Text key={k} selectable style={ctx.base}>
            {renderInline((token as Tokens.Paragraph).tokens, ctx, k)}
          </Text>
        )
      case 'text': {
        const text = token as Tokens.Text
        return (
          <Text key={k} selectable style={ctx.base}>
            {text.tokens ? renderInline(text.tokens, ctx, k) : decodeEntities(text.text)}
          </Text>
        )
      }
      case 'code': {
        const code = token as Tokens.Code
        return <CodeBlock key={k} code={code.text} language={code.lang || null} />
      }
      case 'blockquote':
        return (
          <View key={k} style={[styles.quote, { borderLeftColor: ctx.colors.border }]}>
            {renderBlocks((token as Tokens.Blockquote).tokens, ctx, k)}
          </View>
        )
      case 'list': {
        const list = token as Tokens.List
        const start = typeof list.start === 'number' ? list.start : 1
        return (
          <View key={k} style={styles.list}>
            {list.items.map((item, itemIndex) => (
              <View key={`${k}-i${itemIndex}`} style={styles.listItem}>
                <Text style={[ctx.base, styles.bullet, { color: ctx.colors.textMuted }]}>
                  {item.task ? (item.checked ? '☑' : '☐') : list.ordered ? `${start + itemIndex}.` : '•'}
                </Text>
                <View style={styles.listBody}>{renderBlocks(item.tokens, ctx, `${k}-i${itemIndex}`)}</View>
              </View>
            ))}
          </View>
        )
      }
      case 'hr':
        return <View key={k} style={[styles.hr, { backgroundColor: ctx.colors.border }]} />
      case 'table': {
        const table = token as Tokens.Table
        // Every row is measured together so a column is one width from the header down; see tableColumnWidths.
        const widths = tableColumnWidths([table.header, ...table.rows].map((row) => row.map((cell) => cell.text)))
        const cellStyle = (cellIndex: number) => [styles.cell, { borderColor: ctx.colors.border, width: widths[cellIndex] }]
        return (
          <ScrollView key={k} horizontal showsHorizontalScrollIndicator>
            <View>
              <View style={[styles.tableRow, { backgroundColor: ctx.colors.surface2 }]}>
                {table.header.map((cell, cellIndex) => (
                  <Text key={`${k}-h${cellIndex}`} style={[ctx.base, styles.strong, cellStyle(cellIndex)]}>
                    {renderInline(cell.tokens, ctx, `${k}-h${cellIndex}`)}
                  </Text>
                ))}
              </View>
              {table.rows.map((row, rowIndex) => (
                <View key={`${k}-r${rowIndex}`} style={styles.tableRow}>
                  {row.map((cell, cellIndex) => (
                    <Text key={`${k}-r${rowIndex}c${cellIndex}`} selectable style={[ctx.base, cellStyle(cellIndex)]}>
                      {renderInline(cell.tokens, ctx, `${k}-r${rowIndex}c${cellIndex}`)}
                    </Text>
                  ))}
                </View>
              ))}
            </View>
          </ScrollView>
        )
      }
      default:
        return 'raw' in token && String(token.raw).trim() ? (
          <Text key={k} selectable style={ctx.base}>
            {String(token.raw)}
          </Text>
        ) : null
    }
  })
}

export const Markdown = memo(function Markdown({ content }: { content: string }) {
  const { colors } = useTheme()
  const t = useT()
  const tokens = useMemo(() => marked.lexer(content, { gfm: true }), [content])
  const ctx: Ctx = { colors, t, base: { color: colors.textPrimary, fontSize: font.size.md, lineHeight: 24 } }
  return <View style={styles.root}>{renderBlocks(tokens, ctx, 'md')}</View>
})

function CodeBlock({ code, language }: { code: string; language: string | null }) {
  const { colors } = useTheme()
  const t = useT()
  const [copied, flashCopied] = useTransientFlag()

  async function copy() {
    await Clipboard.setStringAsync(code)
    flashCopied()
  }

  return (
    <View style={[styles.code, { backgroundColor: colors.surface2, borderColor: colors.border }]}>
      <View style={[styles.codeHeader, { borderBottomColor: colors.border }]}>
        <AppText variant="caption" tone="muted">
          {language ?? t('markdown.code')}
        </AppText>
        <IconButton
          icon={copied ? 'checkmark' : 'copy-outline'}
          label={copied ? t('markdown.codeCopied') : t('markdown.copyCode')}
          size="md"
          onPress={copy}
        />
      </View>
      <ScrollView horizontal showsHorizontalScrollIndicator contentContainerStyle={styles.codeScroll}>
        <Text selectable style={[styles.codeText, { color: colors.textPrimary }]}>
          {code}
        </Text>
      </ScrollView>
    </View>
  )
}

const styles = StyleSheet.create({
  root: { gap: space.md },
  heading: { fontWeight: '600' },
  strong: { fontWeight: '700' },
  em: { fontStyle: 'italic' },
  del: { textDecorationLine: 'line-through' },
  link: { textDecorationLine: 'underline' },
  codespan: { fontFamily: font.mono, fontSize: font.size.sm },
  quote: { borderLeftWidth: 3, paddingLeft: space.md, gap: space.sm },
  list: { gap: space.xs },
  listItem: { flexDirection: 'row', gap: space.sm },
  bullet: { minWidth: 18 },
  listBody: { flex: 1, gap: space.xs },
  hr: { height: StyleSheet.hairlineWidth, marginVertical: space.sm },
  tableRow: { flexDirection: 'row' },
  cell: { padding: space.sm, borderWidth: StyleSheet.hairlineWidth },
  code: { borderRadius: radius.md, borderWidth: StyleSheet.hairlineWidth, overflow: 'hidden' },
  codeHeader: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingLeft: space.md,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  codeScroll: { padding: space.md },
  codeText: { fontFamily: font.mono, fontSize: 13, lineHeight: 20 },
})
