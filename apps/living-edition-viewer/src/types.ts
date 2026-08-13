import type { IconName } from '@blueprintjs/icons'

// Registry identifiers remain open strings so packages can add designs, layers,
// sources, and workspaces without changing a central union.
export type DesignId = string
export type DrawMode = 'select' | 'rectangle' | 'polygon'
export type LayerId = string
export type MatrixFocusId = string
export type NoteScope = 'book' | 'page' | 'region'
export type ReaderAudienceId = string
export type ReaderAccessPreferenceId = string
export type ReaderCapabilityId = string
export type ReaderMaterialId = string
export type ReaderPresentationId = string
export type ReaderViewportId = string
export type TextSourceId = string
export type Workspace = string

export interface WorkspaceDefinition {
  id: Workspace
  label: string
  icon: IconName
  detail: string
}

export interface ReaderAudienceDefinition {
  id: ReaderAudienceId
  label: string
  icon: IconName
  detail: string
  preferredPresentationIds: readonly ReaderPresentationId[]
  apparatus: 'full' | 'selective' | 'guided' | 'minimal'
}

export interface ReaderAccessPreferenceDefinition {
  id: ReaderAccessPreferenceId
  label: string
  icon: IconName
  detail: string
  features: readonly string[]
}

export interface ReaderMaterialDefinition {
  id: ReaderMaterialId
  label: string
  icon: IconName
  detail: string
  capabilities: readonly ReaderCapabilityId[]
}

export interface ReaderPresentationDefinition {
  id: ReaderPresentationId
  label: string
  icon: IconName
  detail: string
  requiresAll: readonly ReaderCapabilityId[]
  audienceAffinity: readonly ReaderAudienceId[]
  materialAffinity: readonly ReaderMaterialId[]
  fallbackId: ReaderPresentationId | null
}

export interface ReaderCompositionPreset {
  id: string
  label: string
  detail: string
  publicationId: string
  audienceId: ReaderAudienceId
  presentationId: ReaderPresentationId
  accessPreferenceId: ReaderAccessPreferenceId
}

export interface ReaderViewportDefinition {
  id: ReaderViewportId
  label: string
  icon: IconName
  frameWidth: string
}

export interface ReaderLayerPin {
  kind: string
  layerId: string
  revision: string
  label: string
  role?: string
}

export interface ReaderProjectionDescriptor {
  id: string
  releaseId: string
  state: string
  rights: {
    access: 'public' | 'restricted' | 'unknown'
    reproduction: string
    allowedPresentationIds: readonly ReaderPresentationId[]
  }
  layerPins: readonly ReaderLayerPin[]
  entityReleaseId: string | null
  publicRelease: {
    state: string
    url: string | null
  }
  build: {
    state: string
    fidelity: string
    generatedAt: string
  }
  exclusions: readonly string[]
  problems: readonly {
    id: string
    severity: string
    message: string
  }[]
  allowedPresentationIds: readonly ReaderPresentationId[]
  blocked: readonly { presentationId: ReaderPresentationId; reason: string }[]
}

export interface ReaderStructureNode {
  id: string
  adapterId: string
  kind: string
  label: string
  parentId: string | null
  order: number
  targetIds: readonly string[]
}

export interface ReaderTarget {
  id: string
  adapterId: string
  kind: string
  label: string
  locator: string
}

export interface ReaderCitation {
  id: string
  label: string
  text: string
  url?: string
}

export interface ReaderEntity {
  id: string
  label: string
  writtenForms: readonly string[]
  description: string
  authorityState: string
}

export interface ReaderSourceRegion {
  id: string
  label: string
  sourceText: string
  transcription: string
  translation: string
  entityIds: readonly string[]
}

export interface ReaderSection {
  id: string
  heading: string
  body: readonly string[]
  note?: string
  citationIds: readonly string[]
  entityIds: readonly string[]
}

export interface ReaderAdapterInstance {
  id: string
  kind: string
  fixture: boolean
  payload: unknown
}

export interface ReaderPublication {
  id: string
  projectionId: string
  releaseId: string
  releaseLabel: string
  publishedAt: string | null
  status: string
  canonicalUrl: string | null
  title: string
  subtitle: string
  repository: string
  shelfmark: string
  languageLabel: string
  materialProfileId: ReaderMaterialId
  capabilities: readonly ReaderCapabilityId[]
  projectionNotice: string
  projection: ReaderProjectionDescriptor
  structures: readonly ReaderStructureNode[]
  targets: readonly ReaderTarget[]
  adapters: readonly ReaderAdapterInstance[]
  citations: readonly ReaderCitation[]
  entities: readonly ReaderEntity[]
}

export interface ManuscriptReaderPayload {
  folio: string
  sourceRegions: readonly ReaderSourceRegion[]
  sections: readonly ReaderSection[]
}

export interface ManuscriptReaderPublication extends ReaderPublication {
  materialProfileId: 'manuscript'
  adapters: readonly [{
    id: string
    kind: 'manuscript'
    fixture: boolean
    payload: ManuscriptReaderPayload
  }]
}

export interface ReferenceReaderEntry {
  id: string
  label: string
  kicker: string
  summary: string
  citationIds: readonly string[]
  entityIds: readonly string[]
}

export interface ReferenceReaderPayload {
  volumeLabel: string
  sections: readonly ReaderSection[]
  entries: readonly ReferenceReaderEntry[]
}

export interface ReferenceReaderPublication extends ReaderPublication {
  materialProfileId: 'reference-work'
  adapters: readonly [{
    id: string
    kind: 'reference'
    fixture: boolean
    payload: ReferenceReaderPayload
  }]
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
