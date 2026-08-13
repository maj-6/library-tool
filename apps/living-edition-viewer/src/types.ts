import type { IconName } from '@blueprintjs/icons'

// Registry identifiers remain open strings so packages can add designs, layers,
// sources, and workspaces without changing a central union.
export type DesignId = string
export type DrawMode = 'select' | 'rectangle' | 'polygon'
export type LayerId = string
export type MatrixFocusId = string
export type NoteScope = 'book' | 'page' | 'region'
export type TextSourceId = string
export type Workspace = string

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

export interface WorkbenchLayoutDefinition {
  designId: DesignId
  layoutClass: string
  windowTitle: string
  documentCode: string
  statusText: string
  features: readonly string[]
}
