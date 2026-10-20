import fs from 'fs'
import path from 'path'

const ROOT = path.resolve(__dirname, '../..')
const PLATFORM_FILE = /^(.+)\.(web|native|ios|android)\.tsx?$/

function walk(dir: string): string[] {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name)
    return entry.isDirectory() ? walk(full) : [full]
  })
}

describe('platform files', () => {
  /*
   * Metro resolves `./name` to `name.web.ts` on Web (and `.ios`/`.android`/`.native` on devices), so a platform file
   * importing its own base name imports itself: everything it reads from there is undefined. Jest resolves for the
   * device, so no other test sees it — this is how the Web boot stopped at the opening screen.
   */
  it('never import their own base name, which resolves back to themselves', () => {
    const offenders = [...walk(path.join(ROOT, 'src')), ...walk(path.join(ROOT, 'app'))].flatMap((file) => {
      const match = PLATFORM_FILE.exec(path.basename(file))
      if (!match) return []
      const self = new RegExp(`from\\s+['"]\\./${match[1]!.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}['"]`)
      return self.test(fs.readFileSync(file, 'utf8')) ? [path.relative(ROOT, file)] : []
    })
    expect(offenders).toEqual([])
  })
})
