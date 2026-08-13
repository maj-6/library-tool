import { useMemo, useState } from 'react'
import { Button, ButtonGroup, Checkbox, Icon, InputGroup, Tag } from '@blueprintjs/core'
import { activeManuscript } from '../data/registries'
import type { DesignId } from '../types'
import './library/LibraryBrowser.css'

interface Props {
  variant: DesignId
  onOpenEdition: () => void
}

type LibraryMode = 'catalog' | 'collections' | 'workflow'
type SortKey = 'title' | 'date' | 'language' | 'pages' | 'coverage' | 'status'
type SortDirection = 'ascending' | 'descending'
type MaterialId = 'manuscript' | 'early-print' | 'modern'
type LayerId = 'transcription' | 'translation' | 'entities'
type LayerState = 'none' | 'queued' | 'active' | 'review'
type FacetFlag = 'edition' | 'entities' | 'translation'

interface LayerMeasure {
  coverage: number
  state: LayerState
}

interface WorkflowIssues {
  blocking: number
  review: number
  notes: number
}

interface BookRecord {
  id: string
  title: string
  author: string
  date: string
  dateSort: number
  language: string
  material: MaterialId
  status: string
  pages: number
  coverage: number
  subject: string
  shelfmark: string
  repository: string
  updated: string
  collections: string[]
  layers: Record<LayerId, LayerMeasure>
  issues: WorkflowIssues
  nextAction: string
}

interface ModeDefinition {
  id: LibraryMode
  label: string
  icon: 'th' | 'diagram-tree' | 'timeline-line-chart'
}

interface FacetDefinition<T extends string> {
  id: T
  label: string
  count: number
}

interface CollectionNode {
  id: string
  label: string
  icon: 'database' | 'book' | 'document' | 'layers' | 'endorsed'
  accepts: (record: BookRecord) => boolean
}

interface CollectionGroup {
  id: string
  label: string
  nodes: CollectionNode[]
}

const modeRegistry: readonly ModeDefinition[] = [
  { id: 'catalog', label: 'Catalog', icon: 'th' },
  { id: 'collections', label: 'Collections', icon: 'diagram-tree' },
  { id: 'workflow', label: 'Workflow', icon: 'timeline-line-chart' },
]

const layerRegistry: readonly FacetDefinition<LayerId>[] = [
  { id: 'transcription', label: 'Text', count: 638 },
  { id: 'translation', label: 'Translation', count: 291 },
  { id: 'entities', label: 'Entities', count: 417 },
]

const materialRegistry: readonly FacetDefinition<MaterialId>[] = [
  { id: 'manuscript', label: 'Manuscript', count: 312 },
  { id: 'early-print', label: 'Early print', count: 1_184 },
  { id: 'modern', label: 'Modern', count: 2_860 },
]

const languageRegistry = [
  { id: 'English', label: 'English', count: 1_932 },
  { id: 'Latin', label: 'Latin', count: 488 },
  { id: 'Middle English', label: 'Middle English', count: 96 },
] as const

const facetFlagRegistry: readonly FacetDefinition<FacetFlag>[] = [
  { id: 'edition', label: 'Edition work', count: 638 },
  { id: 'entities', label: 'Entity links', count: 417 },
  { id: 'translation', label: 'Translation', count: 291 },
]

const books: readonly BookRecord[] = [
  {
    id: 'herbal-ms',
    title: 'A Medieval Herbal and Calendar',
    author: 'Anonymous',
    date: 'c. 1450',
    dateSort: 1450,
    language: 'Middle English',
    material: 'manuscript',
    status: 'In review',
    pages: 55,
    coverage: 64,
    subject: 'Regimen · materia medica',
    shelfmark: 'Takamiya MS 46 1',
    repository: 'Beinecke Library',
    updated: '2026-08-12',
    collections: ['active', 'medicine'],
    layers: {
      transcription: { coverage: 92, state: 'review' },
      translation: { coverage: 61, state: 'active' },
      entities: { coverage: 38, state: 'active' },
    },
    issues: { blocking: 2, review: 34, notes: 17 },
    nextAction: 'Resolve OCR gaps',
  },
  {
    id: 'gerard',
    title: 'The Herball or Generall Historie of Plantes',
    author: 'John Gerard',
    date: '1633',
    dateSort: 1633,
    language: 'English',
    material: 'early-print',
    status: 'Processing',
    pages: 1_740,
    coverage: 18,
    subject: 'Botany · medicine',
    shelfmark: 'QL41 .G36 1633',
    repository: 'World Herb Library',
    updated: '2026-08-09',
    collections: ['active', 'medicine'],
    layers: {
      transcription: { coverage: 24, state: 'active' },
      translation: { coverage: 0, state: 'none' },
      entities: { coverage: 8, state: 'queued' },
    },
    issues: { blocking: 0, review: 186, notes: 8 },
    nextAction: 'Review text batches',
  },
  {
    id: 'woodville',
    title: 'Medical Botany, Volume II',
    author: 'William Woodville',
    date: '1792',
    dateSort: 1792,
    language: 'English',
    material: 'modern',
    status: 'Catalogued',
    pages: 292,
    coverage: 0,
    subject: 'Illustrated · materia medica',
    shelfmark: 'QK99 .W66 v.2',
    repository: 'World Herb Library',
    updated: '2026-07-28',
    collections: ['medicine'],
    layers: {
      transcription: { coverage: 0, state: 'none' },
      translation: { coverage: 0, state: 'none' },
      entities: { coverage: 0, state: 'none' },
    },
    issues: { blocking: 0, review: 0, notes: 2 },
    nextAction: 'Schedule capture',
  },
  {
    id: 'gentiana',
    title: 'De Gentiana: tractatus brevis',
    author: 'Attributed to Macer',
    date: '[1518?]',
    dateSort: 1518,
    language: 'Latin',
    material: 'early-print',
    status: 'Entity review',
    pages: 48,
    coverage: 82,
    subject: 'Incunable · simples',
    shelfmark: 'Inc. Herb. 1518.4',
    repository: 'World Herb Library',
    updated: '2026-08-11',
    collections: ['active', 'latin', 'medicine'],
    layers: {
      transcription: { coverage: 100, state: 'review' },
      translation: { coverage: 78, state: 'review' },
      entities: { coverage: 69, state: 'review' },
    },
    issues: { blocking: 1, review: 12, notes: 23 },
    nextAction: 'Reconcile plant names',
  },
]

const collectionRegistry: readonly CollectionGroup[] = [
  {
    id: 'scope',
    label: 'Scope',
    nodes: [
      { id: 'all', label: 'All records', icon: 'database', accepts: () => true },
      { id: 'active', label: 'Active work', icon: 'endorsed', accepts: (record) => record.collections.includes('active') },
    ],
  },
  {
    id: 'material',
    label: 'Material',
    nodes: [
      { id: 'manuscripts', label: 'Manuscripts', icon: 'book', accepts: (record) => record.material === 'manuscript' },
      { id: 'print', label: 'Printed works', icon: 'document', accepts: (record) => record.material !== 'manuscript' },
    ],
  },
  {
    id: 'curated',
    label: 'Curated',
    nodes: [
      { id: 'medicine', label: 'Materia medica', icon: 'layers', accepts: (record) => record.collections.includes('medicine') },
      { id: 'latin', label: 'Latin corpus', icon: 'layers', accepts: (record) => record.collections.includes('latin') },
    ],
  },
]

const sortValue: Record<SortKey, (record: BookRecord) => string | number> = {
  title: (record) => record.title,
  date: (record) => record.dateSort,
  language: (record) => record.language,
  pages: (record) => record.pages,
  coverage: (record) => record.coverage,
  status: (record) => record.status,
}

const materialLabel = Object.fromEntries(materialRegistry.map((item) => [item.id, item.label])) as Record<MaterialId, string>

function compareRecords(left: BookRecord, right: BookRecord, key: SortKey, direction: SortDirection) {
  const leftValue = sortValue[key](left)
  const rightValue = sortValue[key](right)
  const comparison = typeof leftValue === 'number' && typeof rightValue === 'number'
    ? leftValue - rightValue
    : String(leftValue).localeCompare(String(rightValue))
  return direction === 'ascending' ? comparison : -comparison
}

function layerFlagMatches(record: BookRecord, flag: FacetFlag) {
  if (flag === 'edition') return record.coverage > 0
  return record.layers[flag].coverage > 0
}

function ProgressCell({ value }: { value: number }) {
  return (
    <span className="lib-progress" aria-label={`${value}%`}>
      <i style={{ width: `${value}%` }} />
      <b>{value}</b>
    </span>
  )
}

function StateMark({ state }: { state: LayerState }) {
  const labels: Record<LayerState, string> = { none: '—', queued: 'Queued', active: 'Active', review: 'Review' }
  return <span className={`lib-state lib-state--${state}`}>{labels[state]}</span>
}

function RecordInspector({ record, onOpenEdition }: { record?: BookRecord; onOpenEdition: () => void }) {
  if (!record) return <div className="lib-empty lib-empty--inspector">No record selected.</div>

  return (
    <aside className="lib-inspector" aria-label="Record properties">
      <header className="lib-panel-title">
        <span>Properties</span>
        <Tag minimal>{record.status}</Tag>
      </header>
      <div className="lib-inspector__identity">
        <Icon icon={record.material === 'manuscript' ? 'book' : 'document'} size={22} />
        <div><strong>{record.title}</strong><span>{record.author}</span></div>
      </div>
      <dl className="lib-properties">
        <div><dt>Date</dt><dd>{record.date}</dd></div>
        <div><dt>Language</dt><dd>{record.language}</dd></div>
        <div><dt>Material</dt><dd>{materialLabel[record.material]}</dd></div>
        <div><dt>Extent</dt><dd>{record.pages.toLocaleString()} pages</dd></div>
        <div><dt>Shelfmark</dt><dd>{record.shelfmark}</dd></div>
        <div><dt>Repository</dt><dd>{record.repository}</dd></div>
        <div><dt>Updated</dt><dd>{record.updated}</dd></div>
      </dl>
      <section className="lib-inspector__layers">
        <h4>Layer coverage</h4>
        {layerRegistry.map((layer) => (
          <div key={layer.id}>
            <span>{layer.label}</span>
            <ProgressCell value={record.layers[layer.id].coverage} />
          </div>
        ))}
      </section>
      {record.id === 'herbal-ms' && (
        <div className="lib-inspector__edition">
          <span>Current canvas</span><strong>{activeManuscript.folio}</strong>
          <Button small intent="primary" icon="book" onClick={onOpenEdition}>Open edition</Button>
        </div>
      )}
    </aside>
  )
}

interface CatalogTableProps {
  records: readonly BookRecord[]
  selectedId: string
  onSelect: (id: string) => void
  onOpenEdition: () => void
  sortKey: SortKey
  sortDirection: SortDirection
  onSort: (key: SortKey) => void
}

function CatalogTable({ records, selectedId, onSelect, onOpenEdition, sortKey, sortDirection, onSort }: CatalogTableProps) {
  const columns: readonly { key: SortKey; label: string; className?: string }[] = [
    { key: 'title', label: 'Title' },
    { key: 'date', label: 'Date' },
    { key: 'language', label: 'Language' },
    { key: 'pages', label: 'Pages', className: 'is-number' },
    { key: 'coverage', label: 'Coverage' },
    { key: 'status', label: 'State' },
  ]
  const selected = records.find((record) => record.id === selectedId) ?? records[0]

  return (
    <div className="lib-workarea lib-workarea--catalog">
      <div className="lib-table-wrap">
        <table className="lib-table lib-catalog-table">
          <thead><tr>{columns.map((column) => (
            <th key={column.key} className={column.className} aria-sort={sortKey === column.key ? sortDirection : 'none'}>
              <button type="button" onClick={() => onSort(column.key)}>
                {column.label}<span>{sortKey === column.key ? (sortDirection === 'ascending' ? '▲' : '▼') : ''}</span>
              </button>
            </th>
          ))}</tr></thead>
          <tbody>{records.map((record) => (
            <tr
              key={record.id}
              className={record.id === selected?.id ? 'is-selected' : undefined}
              onClick={() => onSelect(record.id)}
              onDoubleClick={record.id === 'herbal-ms' ? onOpenEdition : undefined}
            >
              <td><strong>{record.title}</strong><small>{record.author} · {materialLabel[record.material]}</small></td>
              <td>{record.date}</td>
              <td>{record.language}</td>
              <td className="is-number">{record.pages.toLocaleString()}</td>
              <td><ProgressCell value={record.coverage} /></td>
              <td><span className="lib-status-text">{record.status}</span></td>
            </tr>
          ))}</tbody>
        </table>
        {records.length === 0 && <div className="lib-empty">No records.</div>}
      </div>
      <RecordInspector record={selected} onOpenEdition={onOpenEdition} />
    </div>
  )
}

interface CollectionViewProps {
  records: readonly BookRecord[]
  selectedId: string
  onSelect: (id: string) => void
  onOpenEdition: () => void
  collectionId: string
  onCollectionChange: (id: string) => void
}

function CollectionView({ records, selectedId, onSelect, onOpenEdition, collectionId, onCollectionChange }: CollectionViewProps) {
  const node = collectionRegistry.flatMap((group) => group.nodes).find((item) => item.id === collectionId)
  const visibleRecords = node ? records.filter(node.accepts) : records
  const selected = visibleRecords.find((record) => record.id === selectedId) ?? visibleRecords[0]

  return (
    <div className="lib-workarea lib-workarea--collections">
      <nav className="lib-tree" aria-label="Collections">
        <header className="lib-panel-title">Collection tree</header>
        {collectionRegistry.map((group) => (
          <section key={group.id}>
            <h4>{group.label}</h4>
            {group.nodes.map((item) => {
              const count = records.filter(item.accepts).length
              return (
                <button
                  key={item.id}
                  type="button"
                  className={item.id === collectionId ? 'is-selected' : undefined}
                  onClick={() => onCollectionChange(item.id)}
                >
                  <Icon icon={item.icon} size={13} /><span>{item.label}</span><b>{count}</b>
                </button>
              )
            })}
          </section>
        ))}
      </nav>
      <section className="lib-record-list" aria-label="Collection records">
        <header className="lib-panel-title"><span>Records</span><b>{visibleRecords.length}</b></header>
        {visibleRecords.map((record) => (
          <button
            type="button"
            key={record.id}
            className={record.id === selected?.id ? 'is-selected' : undefined}
            onClick={() => onSelect(record.id)}
            onDoubleClick={record.id === 'herbal-ms' ? onOpenEdition : undefined}
          >
            <Icon icon={record.material === 'manuscript' ? 'book' : 'document'} size={15} />
            <span><strong>{record.title}</strong><small>{record.date} · {record.language}</small></span>
            <b>{record.coverage}%</b>
          </button>
        ))}
        {visibleRecords.length === 0 && <div className="lib-empty">No records.</div>}
      </section>
      <RecordInspector record={selected} onOpenEdition={onOpenEdition} />
    </div>
  )
}

function WorkflowView({ records, selectedId, onSelect, onOpenEdition }: Omit<CatalogTableProps, 'sortKey' | 'sortDirection' | 'onSort'>) {
  const totals = useMemo(() => records.reduce((sum, record) => ({
    blocking: sum.blocking + record.issues.blocking,
    review: sum.review + record.issues.review,
    notes: sum.notes + record.issues.notes,
  }), { blocking: 0, review: 0, notes: 0 }), [records])
  const ready = records.filter((record) => record.coverage >= 75 && record.issues.blocking === 0).length

  return (
    <div className="lib-workarea lib-workarea--workflow">
      <section className="lib-ledger">
        <div className="lib-ledger-summary">
          <dl><dt>Records</dt><dd>{records.length}</dd></dl>
          <dl><dt>Ready</dt><dd>{ready}</dd></dl>
          <dl className={totals.blocking ? 'has-alert' : undefined}><dt>Blocking</dt><dd>{totals.blocking}</dd></dl>
          <dl><dt>Review</dt><dd>{totals.review}</dd></dl>
          <dl><dt>Notes</dt><dd>{totals.notes}</dd></dl>
        </div>
        <div className="lib-table-wrap">
          <table className="lib-table lib-workflow-table">
            <thead><tr>
              <th>Record</th>
              {layerRegistry.map((layer) => <th key={layer.id}>{layer.label}</th>)}
              <th className="is-number">Issues</th>
              <th>Next action</th>
            </tr></thead>
            <tbody>{records.map((record) => (
              <tr
                key={record.id}
                className={record.id === selectedId ? 'is-selected' : undefined}
                onClick={() => onSelect(record.id)}
                onDoubleClick={record.id === 'herbal-ms' ? onOpenEdition : undefined}
              >
                <td><strong>{record.title}</strong><small>{record.status} · {record.coverage}% overall</small></td>
                {layerRegistry.map((layer) => (
                  <td key={layer.id}>
                    <ProgressCell value={record.layers[layer.id].coverage} />
                    <StateMark state={record.layers[layer.id].state} />
                  </td>
                ))}
                <td className="is-number">
                  <span className={record.issues.blocking ? 'lib-issue-count has-alert' : 'lib-issue-count'}>
                    {record.issues.blocking + record.issues.review}
                  </span>
                </td>
                <td><span className="lib-next-action">{record.nextAction}</span></td>
              </tr>
            ))}</tbody>
          </table>
          {records.length === 0 && <div className="lib-empty">No records.</div>}
        </div>
      </section>
    </div>
  )
}

export function LibraryBrowser({ variant, onOpenEdition }: Props) {
  const [mode, setMode] = useState<LibraryMode>(() => {
    const saved = window.localStorage.getItem('whl-design.library-mode')
    return modeRegistry.some((item) => item.id === saved)
      ? saved as LibraryMode
      : 'catalog'
  })
  const [query, setQuery] = useState('')
  const [materials, setMaterials] = useState<Set<MaterialId>>(new Set())
  const [languages, setLanguages] = useState<Set<string>>(new Set())
  const [facetFlags, setFacetFlags] = useState<Set<FacetFlag>>(new Set())
  const [selectedId, setSelectedId] = useState('herbal-ms')
  const [collectionId, setCollectionId] = useState('all')
  const [sortKey, setSortKey] = useState<SortKey>('title')
  const [sortDirection, setSortDirection] = useState<SortDirection>('ascending')

  const filtered = useMemo(() => books.filter((book) => {
    const haystack = `${book.title} ${book.author} ${book.date} ${book.language} ${book.subject} ${book.shelfmark}`.toLowerCase()
    const matchesQuery = haystack.includes(query.trim().toLowerCase())
    const matchesMaterial = materials.size === 0 || materials.has(book.material)
    const matchesLanguage = languages.size === 0 || languages.has(book.language)
    const matchesLayers = [...facetFlags].every((flag) => layerFlagMatches(book, flag))
    return matchesQuery && matchesMaterial && matchesLanguage && matchesLayers
  }).sort((left, right) => compareRecords(left, right, sortKey, sortDirection)), [facetFlags, languages, materials, query, sortDirection, sortKey])

  const toggleMaterial = (id: MaterialId) => setMaterials((current) => {
    const next = new Set(current)
    if (next.has(id)) next.delete(id); else next.add(id)
    return next
  })
  const toggleLanguage = (id: string) => setLanguages((current) => {
    const next = new Set(current)
    if (next.has(id)) next.delete(id); else next.add(id)
    return next
  })
  const toggleFlag = (id: FacetFlag) => setFacetFlags((current) => {
    const next = new Set(current)
    if (next.has(id)) next.delete(id); else next.add(id)
    return next
  })
  const clearFilters = () => {
    setQuery('')
    setMaterials(new Set())
    setLanguages(new Set())
    setFacetFlags(new Set())
  }
  const handleSort = (key: SortKey) => {
    if (key === sortKey) setSortDirection((direction) => direction === 'ascending' ? 'descending' : 'ascending')
    else {
      setSortKey(key)
      setSortDirection('ascending')
    }
  }

  return (
    <div className={`library-workspace library-workspace--${variant}`}>
      <aside className="lib-facets">
        <header className="lib-panel-title"><Icon icon="filter-list" size={14} /> Facets</header>
        <section>
          <h4>Material</h4>
          {materialRegistry.map((item) => (
            <Checkbox key={item.id} checked={materials.has(item.id)} onChange={() => toggleMaterial(item.id)}>
              <span>{item.label}</span><b>{item.count.toLocaleString()}</b>
            </Checkbox>
          ))}
        </section>
        <section>
          <h4>Language</h4>
          {languageRegistry.map((item) => (
            <Checkbox key={item.id} checked={languages.has(item.id)} onChange={() => toggleLanguage(item.id)}>
              <span>{item.label}</span><b>{item.count.toLocaleString()}</b>
            </Checkbox>
          ))}
        </section>
        <section>
          <h4>Layers</h4>
          {facetFlagRegistry.map((item) => (
            <Checkbox key={item.id} checked={facetFlags.has(item.id)} onChange={() => toggleFlag(item.id)}>
              <span>{item.label}</span><b>{item.count.toLocaleString()}</b>
            </Checkbox>
          ))}
        </section>
        <Button small minimal icon="cross" onClick={clearFilters}>Clear</Button>
      </aside>

      <main className="lib-main">
        <header className="lib-commandbar">
          <div className="lib-commandbar__identity">
            <strong>Library</strong><span>{filtered.length} records</span>
          </div>
          <InputGroup
            small
            leftIcon="search"
            placeholder="Search records"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            rightElement={query ? <Button small minimal icon="cross" aria-label="Clear search" onClick={() => setQuery('')} /> : undefined}
          />
          <ButtonGroup className="lib-mode-switch">
            {modeRegistry.map((item) => (
              <Button
                key={item.id}
                small
                icon={item.icon}
                active={mode === item.id}
                onClick={() => {
                  setMode(item.id)
                  window.localStorage.setItem('whl-design.library-mode', item.id)
                }}
              >{item.label}</Button>
            ))}
          </ButtonGroup>
          <div className="lib-commandbar__sort">
            <span>Sort</span>
            <select value={sortKey} onChange={(event) => setSortKey(event.target.value as SortKey)}>
              <option value="title">Title</option><option value="date">Date</option><option value="coverage">Coverage</option><option value="status">State</option>
            </select>
          </div>
        </header>

        {mode === 'catalog' && (
          <CatalogTable
            records={filtered}
            selectedId={selectedId}
            onSelect={setSelectedId}
            onOpenEdition={onOpenEdition}
            sortKey={sortKey}
            sortDirection={sortDirection}
            onSort={handleSort}
          />
        )}
        {mode === 'collections' && (
          <CollectionView
            records={filtered}
            selectedId={selectedId}
            onSelect={setSelectedId}
            onOpenEdition={onOpenEdition}
            collectionId={collectionId}
            onCollectionChange={setCollectionId}
          />
        )}
        {mode === 'workflow' && (
          <WorkflowView records={filtered} selectedId={selectedId} onSelect={setSelectedId} onOpenEdition={onOpenEdition} />
        )}
      </main>
    </div>
  )
}
