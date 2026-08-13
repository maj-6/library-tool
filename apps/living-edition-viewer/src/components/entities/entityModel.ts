export type EntityState = 'reviewed' | 'disputed' | 'possible'
export type FormState = 'attested' | 'normalized' | 'proposed'
export type AssertionState = 'reviewed' | 'contested' | 'proposed'
export type EvidenceKind = 'folio' | 'authority' | 'editorial'

export interface EntityEvidence {
  id: string
  kind: EvidenceKind
  source: string
  folio?: string
  region?: string
  hand?: string
  excerpt: string
  reviewer: string
  date: string
}

export interface EntityNameForm {
  id: string
  written: string
  language: string
  period: string
  script: string
  state: FormState
  evidenceIds: string[]
  note?: string
}

export interface EntityAssertion {
  id: string
  subjectFormId: string
  relation: 'identifies as' | 'broader than' | 'remains'
  target: string
  targetId?: string
  confidence: number
  state: AssertionState
  origin: string
  reviewer: string
  modified: string
  evidenceIds: string[]
  rationale: string
}

export interface PlantEntity {
  id: string
  label: string
  kind: string
  state: EntityState
  tradition: string
  period: string
  region: string
  scopeNote: string
  currentAssertionId: string
  forms: EntityNameForm[]
  evidence: EntityEvidence[]
  assertions: EntityAssertion[]
}

export const plantEntities: readonly PlantEntity[] = [
  {
    id: 'whl:plant:0000142',
    label: 'Fennel preparations',
    kind: 'Plant preparation',
    state: 'disputed',
    tradition: 'Late medieval English regimen',
    period: 'c. 1400–1500',
    region: 'England',
    scopeNote: 'Leaf or seed preparations named in monthly dietary regimens. Do not extend the identity automatically to early modern botanical uses.',
    currentAssertionId: 'a-fen-01',
    forms: [
      { id: 'f-fen-01', written: 'feuel', language: 'Middle English', period: 'c. 1450', script: 'Anglicana', state: 'attested', evidenceIds: ['e-fen-01'] },
      { id: 'f-fen-02', written: 'fenel', language: 'Middle English', period: '14th–15th c.', script: 'Latin', state: 'attested', evidenceIds: ['e-fen-02', 'e-fen-03'] },
      { id: 'f-fen-03', written: 'feniculum', language: 'Latin', period: '12th–16th c.', script: 'Latin', state: 'normalized', evidenceIds: ['e-fen-04'] },
      { id: 'f-fen-04', written: 'finocchio', language: 'Italian', period: '15th c.', script: 'Latin', state: 'attested', evidenceIds: ['e-fen-05'] },
    ],
    evidence: [
      { id: 'e-fen-01', kind: 'folio', source: 'Herbal', folio: '4r', region: 'm4-03', hand: 'Hand A', excerpt: '…and take feuel with the leaf pottage…', reviewer: 'Unreviewed OCR', date: '2026-08-12' },
      { id: 'e-fen-02', kind: 'folio', source: 'Herbal', folio: '11v', region: 'm11-08', hand: 'Hand A', excerpt: 'fenel seed after mete', reviewer: 'A. Reader', date: '2026-08-10' },
      { id: 'e-fen-03', kind: 'folio', source: 'Regimen witness B', folio: '22r', region: 'rb22-02', hand: 'Main hand', excerpt: 'broth of fenel and sauge', reviewer: 'A. Reader', date: '2026-08-09' },
      { id: 'e-fen-04', kind: 'authority', source: 'Historical lexicon', excerpt: 'feniculum, n. — fennel and preparations', reviewer: 'Import 07', date: '2026-08-08' },
      { id: 'e-fen-05', kind: 'editorial', source: 'Concordance note 18', excerpt: 'Italian comparison retained; no direct witness link.', reviewer: 'M. Chen', date: '2026-08-07' },
    ],
    assertions: [
      { id: 'a-fen-01', subjectFormId: 'f-fen-01', relation: 'identifies as', target: 'Foeniculum vulgare Mill.', targetId: 'powo:771318-1', confidence: 0.68, state: 'proposed', origin: 'Mistral OCR 4', reviewer: 'Pending', modified: '2026-08-12', evidenceIds: ['e-fen-01', 'e-fen-02'], rationale: 'Orthographic proximity and regimen context support fennel, but the reading remains unstable.' },
      { id: 'a-fen-02', subjectFormId: 'f-fen-01', relation: 'remains', target: 'Unresolved preparation', confidence: 0.46, state: 'contested', origin: 'A. Reader', reviewer: 'A. Reader', modified: '2026-08-12', evidenceIds: ['e-fen-01'], rationale: 'The same hand forms n and u ambiguously. The passage may refer to a prepared simple rather than a taxon.' },
      { id: 'a-fen-03', subjectFormId: 'f-fen-02', relation: 'broader than', target: 'Fennel seed preparations', confidence: 0.82, state: 'reviewed', origin: 'Editorial board', reviewer: 'M. Chen', modified: '2026-08-06', evidenceIds: ['e-fen-02', 'e-fen-03', 'e-fen-04'], rationale: 'The historical concept includes medicinal and dietary preparations, not only the modern species identity.' },
    ],
  },
  {
    id: 'whl:plant:0000031',
    label: 'Gentian bitter roots',
    kind: 'Plant simple',
    state: 'reviewed',
    tradition: 'Galenic materia medica',
    period: 'c. 1250–1550',
    region: 'Northern Europe',
    scopeNote: 'Bitter roots transmitted under gentian names. Modern taxon links remain assertions rather than concept labels.',
    currentAssertionId: 'a-gen-01',
    forms: [
      { id: 'f-gen-01', written: 'gencyane', language: 'Middle English', period: 'c. 1450', script: 'Anglicana', state: 'attested', evidenceIds: ['e-gen-01'] },
      { id: 'f-gen-02', written: 'gentiana', language: 'Latin', period: '13th–16th c.', script: 'Latin', state: 'attested', evidenceIds: ['e-gen-02'] },
      { id: 'f-gen-03', written: 'Enzian', language: 'German', period: '15th c.', script: 'Gothic', state: 'attested', evidenceIds: ['e-gen-03'] },
      { id: 'f-gen-04', written: '龍膽', language: 'Chinese', period: '18th c.', script: 'Han', state: 'normalized', evidenceIds: ['e-gen-04'] },
    ],
    evidence: [
      { id: 'e-gen-01', kind: 'folio', source: 'Herbal', folio: '19v', region: 'm19-05', hand: 'Hand B', excerpt: 'the rote of gencyane is bitter', reviewer: 'M. Chen', date: '2026-08-02' },
      { id: 'e-gen-02', kind: 'folio', source: 'Materia medica A', folio: '31r', region: 'mm31-01', hand: 'Main hand', excerpt: 'gentiana valet contra venenum', reviewer: 'M. Chen', date: '2026-07-28' },
      { id: 'e-gen-03', kind: 'authority', source: 'German plant-name index', excerpt: 'Enzian — Gentiana spp.', reviewer: 'Import 03', date: '2026-07-20' },
      { id: 'e-gen-04', kind: 'editorial', source: 'Cross-tradition note 4', excerpt: 'Comparison only; not asserted as direct transmission.', reviewer: 'L. Zhao', date: '2026-07-19' },
    ],
    assertions: [
      { id: 'a-gen-01', subjectFormId: 'f-gen-01', relation: 'identifies as', target: 'Gentiana lutea L.', targetId: 'powo:370123-1', confidence: 0.91, state: 'reviewed', origin: 'Editorial board', reviewer: 'M. Chen', modified: '2026-08-02', evidenceIds: ['e-gen-01', 'e-gen-02'], rationale: 'Root use, bitterness, and the Latin parallel support the preferred modern alignment.' },
      { id: 'a-gen-02', subjectFormId: 'f-gen-03', relation: 'broader than', target: 'Gentiana spp.', confidence: 0.74, state: 'proposed', origin: 'Authority import', reviewer: 'Pending', modified: '2026-07-20', evidenceIds: ['e-gen-03'], rationale: 'The index heading does not distinguish species.' },
    ],
  },
  {
    id: 'whl:plant:0000268',
    label: 'Fig fruit',
    kind: 'Plant product',
    state: 'reviewed',
    tradition: 'Dietary regimen',
    period: 'c. 1300–1550',
    region: 'Western Europe',
    scopeNote: 'Fresh and dried fig fruit where witnesses do not distinguish preparation.',
    currentAssertionId: 'a-fig-01',
    forms: [
      { id: 'f-fig-01', written: 'figes', language: 'Middle English', period: 'c. 1450', script: 'Anglicana', state: 'attested', evidenceIds: ['e-fig-01', 'e-fig-02'] },
      { id: 'f-fig-02', written: 'ficus', language: 'Latin', period: '13th–15th c.', script: 'Latin', state: 'attested', evidenceIds: ['e-fig-03'] },
      { id: 'f-fig-03', written: 'figge', language: 'Early English', period: '16th c.', script: 'Secretary', state: 'attested', evidenceIds: ['e-fig-04'] },
    ],
    evidence: [
      { id: 'e-fig-01', kind: 'folio', source: 'Herbal', folio: '7v', region: 'm7-06', hand: 'Hand A', excerpt: 'figes and raysons in wynter', reviewer: 'A. Reader', date: '2026-08-05' },
      { id: 'e-fig-02', kind: 'folio', source: 'Herbal', folio: '13r', region: 'm13-04', hand: 'Hand A', excerpt: 'dry figes comforte the body', reviewer: 'A. Reader', date: '2026-08-05' },
      { id: 'e-fig-03', kind: 'authority', source: 'Latin index', excerpt: 'ficus — fruit of the fig tree', reviewer: 'Import 05', date: '2026-07-31' },
      { id: 'e-fig-04', kind: 'folio', source: 'Printed herbal C', folio: '44', region: 'ph44-09', hand: 'Type', excerpt: 'the dry figge', reviewer: 'M. Chen', date: '2026-07-29' },
    ],
    assertions: [
      { id: 'a-fig-01', subjectFormId: 'f-fig-01', relation: 'identifies as', target: 'Ficus carica L. fruit', targetId: 'powo:853331-1', confidence: 0.94, state: 'reviewed', origin: 'Editorial board', reviewer: 'A. Reader', modified: '2026-08-05', evidenceIds: ['e-fig-01', 'e-fig-02', 'e-fig-03'], rationale: 'The witnesses consistently identify the edible fruit.' },
      { id: 'a-fig-02', subjectFormId: 'f-fig-01', relation: 'broader than', target: 'Fresh and dried fig fruit', confidence: 0.79, state: 'proposed', origin: 'M. Chen', reviewer: 'Pending', modified: '2026-08-04', evidenceIds: ['e-fig-01', 'e-fig-02', 'e-fig-04'], rationale: 'Preparation is inconsistently stated across the witness group.' },
    ],
  },
  {
    id: 'whl:plant:0000441',
    label: 'Raisin / dried grape',
    kind: 'Plant product',
    state: 'possible',
    tradition: 'Dietary regimen',
    period: 'c. 1350–1550',
    region: 'Western Europe',
    scopeNote: 'Dried grape products. Preserve uncertainty where a reading may indicate fresh grapes.',
    currentAssertionId: 'a-rai-01',
    forms: [
      { id: 'f-rai-01', written: 'rasyumes', language: 'Middle English', period: 'c. 1450', script: 'Anglicana', state: 'attested', evidenceIds: ['e-rai-01'] },
      { id: 'f-rai-02', written: 'raisins', language: 'French', period: '14th–15th c.', script: 'Latin', state: 'attested', evidenceIds: ['e-rai-02'] },
      { id: 'f-rai-03', written: 'uvæ passæ', language: 'Latin', period: '13th–16th c.', script: 'Latin', state: 'normalized', evidenceIds: ['e-rai-03'] },
    ],
    evidence: [
      { id: 'e-rai-01', kind: 'folio', source: 'Herbal', folio: '7v', region: 'm7-07', hand: 'Hand A', excerpt: 'figes and rasyumes in wynter', reviewer: 'Unreviewed OCR', date: '2026-08-12' },
      { id: 'e-rai-02', kind: 'folio', source: 'Regimen witness B', folio: '18r', region: 'rb18-03', hand: 'Main hand', excerpt: 'raisins bien lavés', reviewer: 'A. Reader', date: '2026-08-03' },
      { id: 'e-rai-03', kind: 'authority', source: 'Latin index', excerpt: 'uvæ passæ — dried grapes', reviewer: 'Import 05', date: '2026-07-31' },
    ],
    assertions: [
      { id: 'a-rai-01', subjectFormId: 'f-rai-01', relation: 'identifies as', target: 'Vitis vinifera L. dried fruit', targetId: 'powo:262745-2', confidence: 0.63, state: 'proposed', origin: 'Mistral OCR 4', reviewer: 'Pending', modified: '2026-08-12', evidenceIds: ['e-rai-01', 'e-rai-02'], rationale: 'The neighboring fig reference and French parallel support dried grapes.' },
      { id: 'a-rai-02', subjectFormId: 'f-rai-01', relation: 'remains', target: 'Unresolved grape product', confidence: 0.51, state: 'contested', origin: 'A. Reader', reviewer: 'A. Reader', modified: '2026-08-11', evidenceIds: ['e-rai-01'], rationale: 'The OCR string is not yet confirmed against the raster.' },
    ],
  },
] as const

export function getEntityForms(entity: PlantEntity, addedForms: readonly EntityNameForm[]): EntityNameForm[] {
  return [...entity.forms, ...addedForms]
}

export function getEvidence(entity: PlantEntity, evidenceIds: readonly string[]): EntityEvidence[] {
  const ids = new Set(evidenceIds)
  return entity.evidence.filter((item) => ids.has(item.id))
}

export function getMentionCount(entity: PlantEntity): number {
  return entity.evidence.filter((item) => item.kind === 'folio').length
}
