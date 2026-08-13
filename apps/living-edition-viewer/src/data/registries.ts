import type {
  LayerDefinition,
  ManuscriptAsset,
  MatrixFocusDefinition,
  RegionType,
  TextSourceDefinition,
  WorkbenchLayoutDefinition,
  WorkspaceDefinition,
} from '../types'

export const manuscriptAssets = {
  herbalFolio4r: {
    id: 'herbal-folio-4r',
    sourceId: 'canvas-0007',
    displayMode: 'abstract-placeholder',
    alt: 'Medieval herbal manuscript folio 4 recto',
    label: 'The Herbal · c.1450',
    witnessLabel: 'MS Herbal · witness WHL-M-014',
    folio: 'fol. 4r',
    width: 1160,
    height: 2000,
  },
} as const satisfies Record<string, ManuscriptAsset>

export const activeManuscript = manuscriptAssets.herbalFolio4r

export const workspaceDefinitions = [
  { id: 'library', label: 'Library', icon: 'book', detail: '4,356 works' },
  { id: 'edition', label: 'Living edition', icon: 'edit', detail: `${activeManuscript.label} · ${activeManuscript.folio}` },
  { id: 'entities', label: 'Plant entities', icon: 'diagram-tree', detail: 'proof-of-concept DB' },
] as const satisfies readonly WorkspaceDefinition[]

export const layerDefinitions: readonly LayerDefinition[] = [
  { id: 'image', label: 'Page image', shortLabel: 'Image', icon: 'media', defaultVisible: true },
  { id: 'regions', label: 'Region geometry', shortLabel: 'Regions', icon: 'selection', defaultVisible: true, overlayToggle: true, matrixFocus: 'geometry' },
  { id: 'transcription', label: 'Transcription', shortLabel: 'Text', icon: 'font', defaultVisible: true, matrixFocus: 'text' },
  { id: 'translation', label: 'Modern English', shortLabel: 'English', icon: 'translate', defaultVisible: true },
  { id: 'entities', label: 'Entity mentions', shortLabel: 'Entities', icon: 'diagram-tree', defaultVisible: true, overlayToggle: true, matrixFocus: 'entities' },
  { id: 'knowledge', label: 'Knowledge notes', shortLabel: 'Knowledge', icon: 'learning', defaultVisible: true, overlayToggle: true, matrixFocus: 'knowledge' },
] as const

export const overlayLayerDefinitions = layerDefinitions.filter((layer) => layer.overlayToggle)

export const matrixFocusDefinitions: MatrixFocusDefinition[] = layerDefinitions.flatMap((layer) => layer.matrixFocus
  ? [{ id: layer.matrixFocus, label: layer.shortLabel, icon: layer.icon }]
  : [])

export const textSourceDefinitions = [
  { id: 'mistral', label: 'Mistral 4', title: 'Mistral OCR 4', status: 'machine' },
  { id: 'local', label: 'Local', title: 'Local OCR', status: 'machine' },
  { id: 'edited', label: 'Edited', title: 'Editorial revision 3', status: 'reviewed' },
] as const satisfies readonly TextSourceDefinition[]

export const regionTypeDefinitions = [
  { id: 'body', name: 'Body text', color: '#2f81f7' },
  { id: 'hand-a', name: 'Hand A · primary scribe', color: '#4b8fc9', parentId: 'body' },
  { id: 'hand-b', name: 'Hand B · later addition', color: '#8671b8', parentId: 'body' },
  { id: 'marginalia', name: 'Marginalia', color: '#c87924' },
  { id: 'plant-name', name: 'Plant-name mention', color: '#38865d' },
  { id: 'rubric', name: 'Rubric / heading', color: '#c64949' },
  { id: 'page-furniture', name: 'Page furniture', color: '#77838f' },
] as const satisfies readonly RegionType[]

export const regionTypeById = Object.fromEntries(
  regionTypeDefinitions.map((type) => [type.id, type]),
) as Record<string, RegionType>

export const workbenchLayoutDefinitions = [
  {
    designId: 'drafting',
    layoutClass: 'drafting',
    windowTitle: 'Drafting Desk',
    documentCode: 'WHL-M-014 / fol. 4r',
    statusText: 'Ready',
    features: ['navigator', 'properties', 'text', 'problems'],
  },
  {
    designId: 'register',
    layoutClass: 'register',
    windowTitle: 'Parallel Register',
    documentCode: 'WHL-M-014 / aligned view',
    statusText: '4 differences',
    features: ['navigator', 'properties', 'text', 'problems'],
  },
  {
    designId: 'console',
    layoutClass: 'console',
    windowTitle: 'Catalog Console',
    documentCode: 'Takamiya MS 46 1',
    statusText: 'Authority DB connected',
    features: ['navigator', 'properties', 'text', 'problems', 'catalog-index'],
  },
] as const satisfies readonly WorkbenchLayoutDefinition[]
