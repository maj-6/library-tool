import type { IconName } from '@blueprintjs/icons'

export type DesignId = 'scriptorium' | 'spatial' | 'queue' | 'matrix'
export type DrawMode = 'select' | 'rectangle' | 'polygon'
export type LayerId = 'image' | 'regions' | 'transcription' | 'translation' | 'entities' | 'knowledge'
export type MatrixFocusId = 'text' | 'geometry' | 'entities' | 'knowledge'
export type NoteScope = 'book' | 'page' | 'region'
export type TextSourceId = 'mistral' | 'local' | 'edited'
export type Workspace = 'library' | 'edition' | 'entities'

export interface WorkspaceDefinition {
  id: Workspace
  label: string
  icon: IconName
  detail: string
}

export interface LayerDefinition {
  id: LayerId
  label: string
  shortLabel: string
  icon: IconName
  defaultVisible: boolean
  overlayToggle?: boolean
  matrixFocus?: MatrixFocusId
}

export interface MatrixFocusDefinition {
  id: MatrixFocusId
  label: string
  icon: IconName
}

export interface TextSourceDefinition {
  id: TextSourceId
  label: string
  title: string
  status: 'machine' | 'reviewed'
}

export interface ManuscriptAsset {
  id: string
  sourceId: string
  displayMode: 'abstract-placeholder' | 'local-raster'
  alt: string
  label: string
  witnessLabel: string
  folio: string
  width: number
  height: number
}

export interface Point {
  x: number
  y: number
}

export interface Region {
  id: string
  label: string
  typeId: string
  color: string
  confidence?: number
  x: number
  y: number
  width: number
  height: number
  polygon?: Point[]
  source: 'mistral-ocr-4' | 'local-tesseract' | 'manual'
}

export interface RegionType {
  id: string
  name: string
  color: string
  parentId?: string
}

export interface DesignDirection {
  id: DesignId
  marker: string
  title: string
  subtitle: string
  description: string
  bestFor: string
  tradeoff: string
}
