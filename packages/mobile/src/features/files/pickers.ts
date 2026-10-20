import * as DocumentPicker from 'expo-document-picker'
import * as ImagePicker from 'expo-image-picker'

import { t } from '@/i18n'
import type { UploadPart } from '@/services/api/upload'

export interface PickedFile extends UploadPart {
  kind: 'image' | 'document'
  /** Local URI for an on-device preview before upload. */
  previewUri: string | null
  sizeBytes: number | null
}

export class PermissionDeniedError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'PermissionDeniedError'
  }
}

function fromImage(asset: ImagePicker.ImagePickerAsset): PickedFile {
  const mimeType = asset.mimeType ?? 'image/jpeg'
  const extension = mimeType.split('/')[1] ?? 'jpg'
  return {
    kind: 'image',
    uri: asset.uri,
    name: asset.fileName ?? `${t('upload.imageName')}-${Date.now()}.${extension}`,
    mimeType,
    file: asset.file ?? null,
    previewUri: asset.uri,
    sizeBytes: asset.fileSize ?? null,
  }
}

const IMAGE_OPTIONS: ImagePicker.ImagePickerOptions = { mediaTypes: ['images'], quality: 0.85, exif: false }

/** System photo picker: no library permission is needed on current iOS/Android. */
export async function pickImageFromLibrary(): Promise<PickedFile | null> {
  const result = await ImagePicker.launchImageLibraryAsync(IMAGE_OPTIONS)
  const asset = result.canceled ? undefined : result.assets[0]
  return asset ? fromImage(asset) : null
}

export async function takePhoto(): Promise<PickedFile | null> {
  const permission = await ImagePicker.requestCameraPermissionsAsync()
  if (!permission.granted) {
    throw new PermissionDeniedError(t('upload.cameraDenied'))
  }
  const result = await ImagePicker.launchCameraAsync(IMAGE_OPTIONS)
  const asset = result.canceled ? undefined : result.assets[0]
  return asset ? fromImage(asset) : null
}

export async function pickDocument(): Promise<PickedFile | null> {
  const result = await DocumentPicker.getDocumentAsync({ copyToCacheDirectory: true, multiple: false })
  const asset = result.canceled ? undefined : result.assets[0]
  if (!asset) return null
  const mimeType = asset.mimeType ?? 'application/octet-stream'
  return {
    kind: mimeType.startsWith('image/') ? 'image' : 'document',
    uri: asset.uri,
    name: asset.name,
    mimeType,
    file: asset.file ?? null,
    previewUri: mimeType.startsWith('image/') ? asset.uri : null,
    sizeBytes: asset.size ?? null,
  }
}
