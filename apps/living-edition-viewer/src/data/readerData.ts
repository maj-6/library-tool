import type {
  ManuscriptReaderPayload,
  ManuscriptReaderPublication,
  ReaderPublication,
  ReferenceReaderPayload,
  ReferenceReaderPublication,
} from '../types'

// These are sealed design fixtures, not catalog records or publication releases.
// The Reader renders only the revisions pinned by each projection.
export const herbalReaderPublication = {
  id: 'reader-herbal-takamiya-46-1',
  projectionId: 'projection-herbal-reader-poc-r1',
  releaseId: 'release-herbal-poc-0.1',
  releaseLabel: 'Projection fixture 0.1',
  publishedAt: null,
  status: 'Citation preview · not published',
  canonicalUrl: null,
  title: 'Herbal in prose and verse',
  subtitle: 'A living-edition reader',
  repository: 'Yale University Library',
  shelfmark: 'Takamiya MS 46 1',
  languageLabel: 'Middle English and Latin · modern English',
  materialProfileId: 'manuscript',
  capabilities: ['ordered-text', 'page-images', 'region-anchors', 'parallel-text', 'entities', 'citations'],
  projectionNotice: 'Projection fixture. Text, notes, citations, and entity anchors are read-only and pinned to this snapshot.',
  projection: {
    id: 'projection-herbal-reader-poc-r1',
    releaseId: 'release-herbal-poc-0.1',
    state: 'unreleased-design-fixture',
    rights: {
      access: 'restricted',
      reproduction: 'Source-record rights must be imported verbatim; no open license is inferred.',
      allowedPresentationIds: ['reading', 'facsimile', 'parallel', 'compare'],
    },
    entityReleaseId: 'poc-2026-08-12',
    publicRelease: {
      state: 'none',
      url: null,
    },
    build: {
      state: 'prototype-generated',
      fidelity: 'Interface sample: one abstract folio and three aligned regions',
      generatedAt: '2026-08-12',
    },
    exclusions: [
      'Source raster is excluded from this design package.',
      'Only three sample regions are represented.',
      'No editorial approval or catalog publication event is asserted.',
    ],
    problems: [
      { id: 'R1', severity: 'warning', message: 'Rights metadata is provisional and blocks public source-image access.' },
      { id: 'R2', severity: 'warning', message: 'Transcription, translation, and entity links are interface fixtures, not approved readings.' },
      { id: 'R3', severity: 'error', message: 'No public release exists; citation output is a preview only.' },
    ],
    layerPins: [
      { kind: 'canvas', layerId: 'canvas-0007', revision: 'r1', label: 'Facsimile canvas', role: 'source-image' },
      { kind: 'transcription', layerId: 'transcription-mistral-ocr4', revision: 'ocr-4-blocks', label: 'Mistral OCR 4', role: 'source' },
      { kind: 'transcription', layerId: 'transcription-editorial', revision: 'r3', label: 'Editorial transcription', role: 'reading' },
      { kind: 'translation', layerId: 'translation-modern-english', revision: 'r2', label: 'Modern English', role: 'reading' },
      { kind: 'entity', layerId: 'plant-mention-candidates', revision: 'r1', label: 'Plant mention candidates' },
      { kind: 'commentary', layerId: 'reader-notes', revision: 'r1', label: 'Reader notes' },
    ],
    allowedPresentationIds: ['reading', 'facsimile', 'parallel', 'compare'],
    blocked: [
      { presentationId: 'explore', reason: 'This projection declares no explorable-object layer.' },
      { presentationId: 'media', reason: 'This projection declares no timed media or cue track.' },
    ],
  },
  structures: [
    { id: 'structure-work', adapterId: 'herbal-manuscript', kind: 'work', label: 'Herbal in prose and verse', parentId: null, order: 1, targetIds: ['target-canvas-0007'] },
    { id: 'structure-folio-4r', adapterId: 'herbal-manuscript', kind: 'folio', label: 'fol. 4r', parentId: 'structure-work', order: 7, targetIds: ['target-canvas-0007', 'target-regions-0007'] },
  ],
  targets: [
    { id: 'target-canvas-0007', adapterId: 'herbal-manuscript', kind: 'canvas', label: 'Canvas 0007', locator: 'canvas-0007@r1' },
    { id: 'target-regions-0007', adapterId: 'herbal-manuscript', kind: 'region-set', label: 'Regions m4-01–m4-06', locator: 'region-layer-editorial@r3' },
  ],
  adapters: [
    {
      id: 'herbal-manuscript',
      kind: 'manuscript',
      fixture: true,
      payload: {
        folio: 'fol. 4r',
        sourceRegions: [
          {
            id: 'm4-01',
            label: 'Opening rubric',
            sourceText: 'Bere tuis pponas ye gode leibe et reilles of metes …',
            transcription: 'Here begins guidance on wholesome diet and the ordering of meals.',
            translation: 'This section gives seasonal guidance for diet and bloodletting.',
            entityIds: [],
          },
          {
            id: 'm4-04',
            label: 'March regimen',
            sourceText: 'H ye monithe of marthe figes y rasyumes …',
            transcription: 'In the month of March, use figs and raisins, and warm meats and drinks.',
            translation: 'In March, take figs and raisins with warming foods and drinks.',
            entityIds: ['concept-fig-medieval-western', 'concept-grape-raisin-medieval-western'],
          },
          {
            id: 'm4-06',
            label: 'May regimen',
            sourceText: 'P ye monithe of may … drinkes … of betayne …',
            transcription: 'In the month of May … drink a preparation of betony.',
            translation: 'In May, drink a preparation of betony; the passage attributes several benefits to it.',
            entityIds: ['concept-betony-medieval-western'],
          },
        ],
        sections: [
          {
            id: 'section-calendar',
            heading: 'A calendar for health',
            body: [
              'The manuscript opens this passage with month-by-month advice. Diet, rest, bathing, and bloodletting are treated as parts of one seasonal regimen.',
              'January and February emphasize restraint and the proper days for treatment. The language is compressed, and several readings remain dependent on editorial judgment.',
            ],
            note: 'The continuous reading joins aligned passages for accessibility; the scholarly view keeps their region boundaries visible.',
            citationIds: ['citation-folio', 'citation-editorial'],
            entityIds: [],
          },
          {
            id: 'section-spring',
            heading: 'Spring foods',
            body: [
              'For March, the reader is advised to take figs and raisins with warming foods and drinks, to avoid bathing, and to observe a prescribed day for bloodletting.',
              'The May entry turns to betony. It describes the herb as part of a preparation and associates it with hearing, digestion, sleep, and bodily balance.',
            ],
            note: 'Plant links expose candidate historical concepts and written forms; they do not silently assert modern botanical identity.',
            citationIds: ['citation-folio'],
            entityIds: ['concept-fig-medieval-western', 'concept-grape-raisin-medieval-western', 'concept-betony-medieval-western'],
          },
        ],
      },
    },
  ],
  citations: [
    {
      id: 'citation-yale-catalog',
      label: 'Catalog-record preview',
      text: 'Yale University Library, Takamiya MS 46 1, “Herbal in prose and verse,” ca. 1400–1425. Verify against the source catalog before publication.',
      url: 'https://collections.library.yale.edu/catalog/16156709',
    },
    {
      id: 'citation-folio',
      label: 'Passage preview',
      text: 'Herbal in prose and verse, fol. 4r, projection fixture 0.1, sample regions m4-01–m4-06; not published.',
    },
    {
      id: 'citation-editorial',
      label: 'Editorial-method preview',
      text: 'Display readings are fixtures for interface review; publication requires an approved editorial release.',
    },
  ],
  entities: [
    {
      id: 'concept-betony-medieval-western',
      label: 'Betony',
      writtenForms: ['betayne', 'betony', 'betonica'],
      description: 'A historical plant concept linked provisionally to medicinal betony traditions.',
      authorityState: 'Proposed authority assertion',
    },
    {
      id: 'concept-fig-medieval-western',
      label: 'Fig',
      writtenForms: ['figes', 'fig', 'figs', 'ficus'],
      description: 'The fruit named in the March dietary regimen.',
      authorityState: 'Candidate written-name match',
    },
    {
      id: 'concept-grape-raisin-medieval-western',
      label: 'Raisin / grape',
      writtenForms: ['rasyumes', 'raisin', 'raisins', 'grape', 'uva'],
      description: 'A dried-grape food named beside figs in the regimen.',
      authorityState: 'Candidate written-name match',
    },
  ],
} as const satisfies ManuscriptReaderPublication

export const referenceReaderPublication = {
  id: 'reader-garden-compendium-fixture',
  projectionId: 'projection-garden-compendium-poc-r1',
  releaseId: 'release-garden-compendium-poc-0.1',
  releaseLabel: 'Projection fixture 0.1',
  publishedAt: null,
  status: 'Citation preview · not published',
  canonicalUrl: null,
  title: 'A small garden compendium',
  subtitle: 'Reference-reader design fixture',
  repository: 'World Herb Library design lab',
  shelfmark: 'REF-FIXTURE-01',
  languageLabel: 'Modern English · synthetic fixture content',
  materialProfileId: 'reference-work',
  capabilities: ['ordered-text', 'volumes', 'entries', 'explorable-objects', 'entities', 'citations'],
  projectionNotice: 'Projection fixture. This synthetic reference work validates a second material adapter; it is not a cataloged publication.',
  projection: {
    id: 'projection-garden-compendium-poc-r1',
    releaseId: 'release-garden-compendium-poc-0.1',
    state: 'unreleased-synthetic-fixture',
    rights: {
      access: 'restricted',
      reproduction: 'Synthetic interface text only; no third-party images or catalog rights are represented.',
      allowedPresentationIds: ['reading', 'explore'],
    },
    entityReleaseId: 'poc-2026-08-12',
    publicRelease: {
      state: 'none',
      url: null,
    },
    build: {
      state: 'prototype-generated',
      fidelity: 'Synthetic volume, entry index, cross-references, and continuous reading',
      generatedAt: '2026-08-12',
    },
    exclusions: [
      'No source publication, facsimile, or external authority assertions are represented.',
      'Entry illustrations are abstract placeholders.',
      'No public catalog or citation record exists.',
    ],
    problems: [
      { id: 'R1', severity: 'warning', message: 'Fixture content is synthetic and must never be exported as collection metadata.' },
      { id: 'R2', severity: 'error', message: 'No public release exists; citation output is a preview only.' },
    ],
    layerPins: [
      { kind: 'text', layerId: 'reference-reading-text', revision: 'r1', label: 'Reference reading text', role: 'reading' },
      { kind: 'index', layerId: 'reference-entry-index', revision: 'r1', label: 'Entry index', role: 'explore' },
      { kind: 'entity', layerId: 'plant-authority-poc', revision: 'poc-2026-08-12', label: 'Plant authority candidates' },
      { kind: 'commentary', layerId: 'reference-reader-notes', revision: 'r1', label: 'Reader notes' },
    ],
    allowedPresentationIds: ['reading', 'explore'],
    blocked: [
      { presentationId: 'facsimile', reason: 'This projection declares no page-image layer.' },
      { presentationId: 'parallel', reason: 'This projection declares no image-aligned parallel text.' },
      { presentationId: 'compare', reason: 'This projection declares no comparison layer.' },
      { presentationId: 'media', reason: 'This projection declares no timed media or cue track.' },
    ],
  },
  structures: [
    { id: 'reference-work', adapterId: 'garden-reference', kind: 'work', label: 'A small garden compendium', parentId: null, order: 1, targetIds: ['reference-volume-1'] },
    { id: 'reference-volume-1', adapterId: 'garden-reference', kind: 'volume', label: 'Volume I · Garden simples', parentId: 'reference-work', order: 1, targetIds: ['entry-betony', 'entry-fig', 'entry-grape'] },
  ],
  targets: [
    { id: 'entry-betony', adapterId: 'garden-reference', kind: 'entry', label: 'Betony', locator: 'entry-index@r1/betony' },
    { id: 'entry-fig', adapterId: 'garden-reference', kind: 'entry', label: 'Fig', locator: 'entry-index@r1/fig' },
    { id: 'entry-grape', adapterId: 'garden-reference', kind: 'entry', label: 'Grape and raisin', locator: 'entry-index@r1/grape-raisin' },
  ],
  adapters: [
    {
      id: 'garden-reference',
      kind: 'reference',
      fixture: true,
      payload: {
        volumeLabel: 'Volume I · Garden simples',
        sections: [
          {
            id: 'reference-introduction',
            heading: 'Reading a plant entry',
            body: [
              'This synthetic reference fixture demonstrates a continuous reading assembled from entries rather than manuscript regions.',
              'Written forms, historical concepts, notes, and citation previews remain visibly provisional.',
            ],
            note: 'The fixture tests the shared reading primitives while its adapter supplies entry-specific navigation.',
            citationIds: ['reference-citation-method'],
            entityIds: ['concept-betony-medieval-western', 'concept-fig-medieval-western'],
          },
          {
            id: 'reference-cross-links',
            heading: 'Cross-reference paths',
            body: [
              'Explore mode treats each entry as an addressable target and preserves links to the same candidate entity release.',
            ],
            note: 'A production reference adapter could contribute volumes, alphabetical ranges, plates, or indexes without changing the Reader kernel.',
            citationIds: ['reference-citation-fixture'],
            entityIds: ['concept-grape-raisin-medieval-western'],
          },
        ],
        entries: [
          { id: 'entry-betony', label: 'Betony', kicker: 'B · medicinal simple', summary: 'A synthetic entry that demonstrates written-name and concept links.', citationIds: ['reference-citation-fixture'], entityIds: ['concept-betony-medieval-western'] },
          { id: 'entry-fig', label: 'Fig', kicker: 'F · fruit', summary: 'A synthetic entry connected to the dietary-regimen concept fixture.', citationIds: ['reference-citation-fixture'], entityIds: ['concept-fig-medieval-western'] },
          { id: 'entry-grape', label: 'Grape and raisin', kicker: 'G · fruit and preparation', summary: 'A synthetic cross-reference between grape and dried-grape written forms.', citationIds: ['reference-citation-fixture'], entityIds: ['concept-grape-raisin-medieval-western'] },
        ],
      },
    },
  ],
  citations: [
    { id: 'reference-citation-method', label: 'Method preview', text: 'Synthetic reference-reader fixture, projection 0.1; not published.' },
    { id: 'reference-citation-fixture', label: 'Entry preview', text: 'Design-fixture entry with no external catalog identity or public release.' },
  ],
  entities: [
    { id: 'concept-betony-medieval-western', label: 'Betony', writtenForms: ['betayne', 'betony', 'betonica'], description: 'Provisional historical-concept fixture.', authorityState: 'Proposed authority assertion' },
    { id: 'concept-fig-medieval-western', label: 'Fig', writtenForms: ['figes', 'fig', 'figs', 'ficus'], description: 'Provisional written-name fixture.', authorityState: 'Candidate written-name match' },
    { id: 'concept-grape-raisin-medieval-western', label: 'Raisin / grape', writtenForms: ['rasyumes', 'raisin', 'raisins', 'grape', 'uva'], description: 'Provisional written-name fixture.', authorityState: 'Candidate written-name match' },
  ],
} as const satisfies ReferenceReaderPublication

export const readerPublications: readonly ReaderPublication[] = [
  herbalReaderPublication,
  referenceReaderPublication,
]

export const defaultReaderPublicationId = herbalReaderPublication.id

export function isManuscriptReaderPayload(value: unknown): value is ManuscriptReaderPayload {
  if (!value || typeof value !== 'object') return false
  const candidate = value as Partial<ManuscriptReaderPayload>
  return typeof candidate.folio === 'string'
    && Array.isArray(candidate.sourceRegions)
    && Array.isArray(candidate.sections)
}

export function isReferenceReaderPayload(value: unknown): value is ReferenceReaderPayload {
  if (!value || typeof value !== 'object') return false
  const candidate = value as Partial<ReferenceReaderPayload>
  return typeof candidate.volumeLabel === 'string'
    && Array.isArray(candidate.entries)
    && Array.isArray(candidate.sections)
}
