import type {
  LayerDefinition,
  ManuscriptAsset,
  MatrixFocusDefinition,
  ReaderAudienceDefinition,
  ReaderAccessPreferenceDefinition,
  ReaderCompositionPreset,
  ReaderMaterialDefinition,
  ReaderPresentationDefinition,
  ReaderViewportDefinition,
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
  { id: 'reader', label: 'Reader', icon: 'eye-open', detail: 'read-only projection fixture' },
] as const satisfies readonly WorkspaceDefinition[]

export const readerAudienceDefinitions = [
  { id: 'scholarly', label: 'Scholarly', icon: 'learning', detail: 'Full apparatus and provenance', preferredPresentationIds: ['parallel', 'compare', 'facsimile'], apparatus: 'full' },
  { id: 'general', label: 'General', icon: 'people', detail: 'Clear reading with selective context', preferredPresentationIds: ['reading', 'parallel'], apparatus: 'selective' },
  { id: 'teaching', label: 'Teaching', icon: 'presentation', detail: 'Guided evidence and discussion', preferredPresentationIds: ['parallel', 'compare', 'reading'], apparatus: 'guided' },
  { id: 'reference', label: 'Reference', icon: 'search', detail: 'Fast lookup with visible citations', preferredPresentationIds: ['explore', 'reading', 'facsimile'], apparatus: 'selective' },
] as const satisfies readonly ReaderAudienceDefinition[]

export const readerAccessPreferenceDefinitions = [
  { id: 'standard', label: 'Standard access', icon: 'person', detail: 'Universal keyboard and screen-reader baseline', features: ['keyboard-navigation', 'semantic-landmarks', 'visible-focus'] },
  { id: 'assisted-reading', label: 'Notes open', icon: 'helper-management', detail: 'Contextual reader notes open by default', features: ['keyboard-navigation', 'semantic-landmarks', 'notes-expanded'] },
  { id: 'low-vision', label: 'Low vision', icon: 'eye-open', detail: 'Larger type and higher contrast', features: ['keyboard-navigation', 'semantic-landmarks', 'large-text', 'high-contrast'] },
  { id: 'custom', label: 'Custom access', icon: 'settings', detail: 'Manually adjusted reader display', features: ['keyboard-navigation', 'semantic-landmarks'] },
] as const satisfies readonly ReaderAccessPreferenceDefinition[]

export const readerMaterialDefinitions = [
  { id: 'manuscript', label: 'Manuscript', icon: 'document', detail: 'Folio image with region-aligned readings', capabilities: ['ordered-text', 'page-images', 'region-anchors', 'parallel-text', 'entities', 'citations'] },
  { id: 'early-print', label: 'Early print', icon: 'book', detail: 'Page image with structured print text', capabilities: ['ordered-text', 'page-images', 'region-anchors', 'parallel-text', 'structured-pages', 'citations'] },
  { id: 'modern-text', label: 'Modern text', icon: 'paragraph', detail: 'Semantic sections without required facsimile', capabilities: ['ordered-text', 'semantic-sections', 'notes', 'entities', 'citations'] },
  { id: 'illustrated', label: 'Illustrated work', icon: 'media', detail: 'Text and inspectable image regions', capabilities: ['ordered-text', 'page-images', 'illustrations', 'explorable-objects', 'region-anchors', 'citations'] },
  { id: 'serial', label: 'Serial / periodical', icon: 'list-columns', detail: 'Issue and article hierarchy', capabilities: ['ordered-text', 'page-images', 'issues', 'article-hierarchy', 'citations'] },
  { id: 'reference-work', label: 'Reference / multi-volume', icon: 'manual', detail: 'Volumes, entries, and cross-references', capabilities: ['ordered-text', 'volumes', 'entries', 'explorable-objects', 'entities', 'citations'] },
  { id: 'time-based', label: 'Time-based / born-digital', icon: 'video', detail: 'Timed media with cue-aligned text', capabilities: ['ordered-text', 'time-based-media', 'timed-cues', 'citations'] },
] as const satisfies readonly ReaderMaterialDefinition[]

export const readerPresentationDefinitions = [
  { id: 'reading', label: 'Reading', icon: 'book', detail: 'Continuous primary reading', requiresAll: ['ordered-text'], audienceAffinity: ['general', 'teaching'], materialAffinity: ['modern-text', 'serial', 'time-based'], fallbackId: null },
  { id: 'facsimile', label: 'Facsimile', icon: 'media', detail: 'Source image with synchronized drawer', requiresAll: ['page-images'], audienceAffinity: ['reference', 'scholarly'], materialAffinity: ['manuscript', 'early-print', 'illustrated', 'serial'], fallbackId: 'reading' },
  { id: 'parallel', label: 'Parallel', icon: 'layout-two-columns', detail: 'Source, transcription, and translation', requiresAll: ['page-images', 'parallel-text'], audienceAffinity: ['scholarly', 'teaching', 'general'], materialAffinity: ['manuscript', 'early-print'], fallbackId: 'facsimile' },
  { id: 'compare', label: 'Compare', icon: 'comparison', detail: 'Revision or witness comparison', requiresAll: ['parallel-text'], audienceAffinity: ['scholarly', 'teaching', 'reference'], materialAffinity: ['manuscript', 'early-print'], fallbackId: 'reading' },
  { id: 'explore', label: 'Explore', icon: 'grid-view', detail: 'Browse illustrated objects or reference entries', requiresAll: ['explorable-objects'], audienceAffinity: ['reference', 'teaching', 'general'], materialAffinity: ['illustrated', 'reference-work'], fallbackId: 'reading' },
  { id: 'media', label: 'Media', icon: 'play', detail: 'Timed playback and cue-aligned transcript', requiresAll: ['time-based-media', 'timed-cues'], audienceAffinity: ['general', 'teaching'], materialAffinity: ['time-based'], fallbackId: 'reading' },
] as const satisfies readonly ReaderPresentationDefinition[]

export const readerCompositionPresets = [
  { id: 'scholarly-manuscript', label: 'Scholar · manuscript', detail: 'Full parallel evidence from the manuscript projection', publicationId: 'reader-herbal-takamiya-46-1', audienceId: 'scholarly', presentationId: 'parallel', accessPreferenceId: 'standard' },
  { id: 'general-manuscript', label: 'General · manuscript', detail: 'Continuous reading from the manuscript projection', publicationId: 'reader-herbal-takamiya-46-1', audienceId: 'general', presentationId: 'reading', accessPreferenceId: 'standard' },
  { id: 'teaching-manuscript', label: 'Class · manuscript', detail: 'Facsimile and synchronized evidence from the manuscript projection', publicationId: 'reader-herbal-takamiya-46-1', audienceId: 'teaching', presentationId: 'facsimile', accessPreferenceId: 'standard' },
  { id: 'general-assisted-manuscript', label: 'General · assisted', detail: 'Notes-open reading over the manuscript projection', publicationId: 'reader-herbal-takamiya-46-1', audienceId: 'general', presentationId: 'reading', accessPreferenceId: 'assisted-reading' },
  { id: 'reference-fixture', label: 'Reference · explore', detail: 'Positive Explore composition from a separately pinned synthetic reference projection', publicationId: 'reader-garden-compendium-fixture', audienceId: 'reference', presentationId: 'explore', accessPreferenceId: 'standard' },
] as const satisfies readonly ReaderCompositionPreset[]

export const readerViewportDefinitions = [
  { id: 'desktop', label: 'Desktop', icon: 'desktop', frameWidth: '100%' },
  { id: 'tablet', label: 'Tablet', icon: 'applications', frameWidth: '768px' },
  { id: 'mobile', label: 'Mobile', icon: 'mobile-phone', frameWidth: '390px' },
] as const satisfies readonly ReaderViewportDefinition[]

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
