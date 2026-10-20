import { creationFilename } from '../creationFileName'

describe('creation file names', () => {
  it('names the file like Web does, with the extension of its real type', () => {
    expect(creationFilename({ id: '1a2b3c4d-9999', kind: 'image', contentType: 'image/jpeg' })).toBe('trindatron-image-1a2b3c4d.jpg')
    expect(creationFilename({ id: '1a2b3c4d-9999', kind: 'video', contentType: 'video/mp4' })).toBe('trindatron-video-1a2b3c4d.mp4')
  })

  it('falls back to the kind when the type is unknown', () => {
    expect(creationFilename({ id: 'abcdefgh', kind: 'image', contentType: null })).toBe('trindatron-image-abcdefgh.png')
    expect(creationFilename({ id: 'abcdefgh', kind: 'video', contentType: 'application/octet-stream' })).toBe('trindatron-video-abcdefgh.mp4')
  })
})
