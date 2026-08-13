import { useMemo, useState } from 'react'
import { Button, ButtonGroup, Card, Checkbox, Icon, InputGroup, Tag } from '@blueprintjs/core'
import { activeManuscript } from '../data/registries'

interface Props {
  variant: 'scriptorium' | 'spatial' | 'queue' | 'matrix'
  onOpenEdition: () => void
}

const books = [
  {
    id: 'herbal-ms', title: 'A Medieval Herbal and Calendar', author: 'Anonymous', date: 'c. 1450', language: 'Middle English',
    status: 'Living edition', pages: 55, progress: 64, subject: 'Manuscript · regimen · materia medica', hasImage: true,
  },
  {
    id: 'gerard', title: 'The Herball or Generall Historie of Plantes', author: 'John Gerard', date: '1633', language: 'English',
    status: 'OCR available', pages: 1_740, progress: 18, subject: 'Early print · botany · medicine', hasImage: false,
  },
  {
    id: 'woodville', title: 'Medical Botany, Volume II', author: 'William Woodville', date: '1792', language: 'English',
    status: 'Catalogued', pages: 292, progress: 0, subject: 'Illustrated · materia medica', hasImage: false,
  },
  {
    id: 'gentiana', title: 'De Gentiana: tractatus brevis', author: 'Attributed to Macer', date: '[1518?]', language: 'Latin',
    status: 'Entity-linked', pages: 48, progress: 82, subject: 'Incunable · simples · Latin', hasImage: false,
  },
]

export function LibraryBrowser({ variant, onOpenEdition }: Props) {
  const [query, setQuery] = useState('')
  const [view, setView] = useState<'grid' | 'list'>(variant === 'matrix' ? 'list' : 'grid')
  const [onlyEditions, setOnlyEditions] = useState(false)
  const filtered = useMemo(() => books.filter((book) => {
    const haystack = `${book.title} ${book.author} ${book.date} ${book.language} ${book.subject}`.toLowerCase()
    return haystack.includes(query.toLowerCase()) && (!onlyEditions || book.progress > 0)
  }), [query, onlyEditions])

  return (
    <div className={`library-browser library-browser--${variant}`}>
      <aside className="library-filters">
        <div className="library-filters__title"><Icon icon="filter-list" /> Refine</div>
        <div className="filter-group"><strong>Material</strong><Checkbox defaultChecked label="Manuscript (312)" /><Checkbox label="Early print (1,184)" /><Checkbox label="Modern (2,860)" /></div>
        <div className="filter-group"><strong>Language</strong><Checkbox defaultChecked label="English (1,932)" /><Checkbox label="Latin (488)" /><Checkbox label="Chinese (221)" /></div>
        <div className="filter-group"><strong>Layers</strong><Checkbox checked={onlyEditions} onChange={(event) => setOnlyEditions(event.currentTarget.checked)} label="Living-edition work" /><Checkbox label="Plant entities" /><Checkbox label="Translation" /></div>
        <Button minimal icon="cross" onClick={() => { setQuery(''); setOnlyEditions(false) }}>Clear filters</Button>
      </aside>
      <main className="catalog-content">
        <header className="catalog-header">
          <div>
            <span className="section-kicker">World Herb Library</span>
            <h2>Browse the collection</h2>
            <p>Works, editions, and physical witnesses are distinct—but discoverable together.</p>
          </div>
          <Tag large intent="success" minimal>4,356 open books</Tag>
        </header>
        <div className="catalog-toolbar">
          <InputGroup large leftIcon="search" placeholder="Search title, person, plant, or passage…" value={query} onChange={(event) => setQuery(event.target.value)} rightElement={query ? <Button minimal icon="cross" onClick={() => setQuery('')} /> : undefined} />
          <Button icon="sort-alphabetical" text="Title" />
          <ButtonGroup><Button icon="grid-view" active={view === 'grid'} onClick={() => setView('grid')} aria-label="Grid view" /><Button icon="list" active={view === 'list'} onClick={() => setView('list')} aria-label="List view" /></ButtonGroup>
        </div>
        <div className="catalog-result-line"><strong>{filtered.length}</strong> design-sample records <span>·</span> sorted by relevance</div>
        <div className={`book-results is-${view}`}>
          {filtered.map((book) => (
            <Card className="book-card" key={book.id} interactive onClick={book.id === 'herbal-ms' ? onOpenEdition : undefined}>
              <div className={`book-cover ${book.hasImage ? 'has-image' : ''}`}>
                {book.hasImage ? <div className="book-cover__manuscript" aria-hidden="true"><b>¶</b><i /><i /><i /><i /><i /></div> : <><Icon icon="book" size={30} /><span>WHL</span></>}
                {book.id === 'herbal-ms' && <Tag className="book-cover__folio" minimal>{activeManuscript.folio}</Tag>}
              </div>
              <div className="book-card__body">
                <div className="book-card__tags"><Tag minimal>{book.date}</Tag><Tag minimal>{book.language}</Tag></div>
                <h3>{book.title}</h3>
                <p className="book-author">{book.author}</p>
                <p className="book-subject">{book.subject}</p>
                <div className="book-card__status">
                  <span className="mini-progress"><i style={{ width: `${book.progress}%` }} /></span>
                  <span>{book.status}</span><small>{book.pages.toLocaleString()} pp.</small>
                </div>
                {book.id === 'herbal-ms' && <Button intent="primary" icon="book" onClick={(event) => { event.stopPropagation(); onOpenEdition() }}>Open living edition</Button>}
              </div>
            </Card>
          ))}
          {filtered.length === 0 && <div className="empty-results"><Icon icon="search" size={28} /><h3>No sample records match</h3><p>Try “herbal”, “Latin”, or clear the layer filter.</p></div>}
        </div>
      </main>
    </div>
  )
}
