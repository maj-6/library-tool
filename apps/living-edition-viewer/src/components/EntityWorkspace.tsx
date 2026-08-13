import { useMemo, useState } from 'react'
import { Button, ButtonGroup, Callout, Card, Dialog, Divider, FormGroup, HTMLSelect, Icon, InputGroup, ProgressBar, Tag, TextArea } from '@blueprintjs/core'

interface Props {
  variant: 'scriptorium' | 'spatial' | 'queue' | 'matrix'
  fromMention: boolean
  onBackToEdition: () => void
}

const entities = [
  { id: 'whl:plant:0000142', label: 'Fennel preparations', forms: ['feuel', 'fenel', 'feniculum', 'finocchio'], mentions: 87, state: 'disputed' },
  { id: 'whl:plant:0000031', label: 'Gentian bitter roots', forms: ['gencyane', 'gentiana', 'Enzian', '龍膽'], mentions: 214, state: 'reviewed' },
  { id: 'whl:plant:0000268', label: 'Fig fruit', forms: ['figes', 'ficus', 'figge'], mentions: 156, state: 'reviewed' },
  { id: 'whl:plant:0000441', label: 'Raisin / dried grape', forms: ['rasyumes', 'raisins', 'uvæ passæ'], mentions: 63, state: 'possible' },
]

export function EntityWorkspace({ variant, fromMention, onBackToEdition }: Props) {
  const [query, setQuery] = useState(fromMention ? 'feuel' : '')
  const [selectedId, setSelectedId] = useState(fromMention ? entities[0].id : entities[1].id)
  const [addAliasOpen, setAddAliasOpen] = useState(false)
  const [newAlias, setNewAlias] = useState('')
  const [aliases, setAliases] = useState<string[]>([])
  const [decision, setDecision] = useState<'undecided' | 'accept' | 'dispute'>('undecided')
  const [candidate, setCandidate] = useState('Foeniculum vulgare Mill.')
  const filtered = useMemo(() => entities.filter((entity) => `${entity.label} ${entity.forms.join(' ')}`.toLowerCase().includes(query.toLowerCase())), [query])
  const selected = entities.find((entity) => entity.id === selectedId) ?? entities[0]

  return (
    <div className={`entity-workspace entity-workspace--${variant}`}>
      <aside className="entity-results">
        <div className="entity-results__head">
          <span className="section-kicker">External proof-of-concept database</span>
          <h2>Plant entities</h2>
          <InputGroup leftIcon="search" placeholder="Names, concepts, identifiers…" value={query} onChange={(event) => setQuery(event.target.value)} />
        </div>
        <div className="entity-result-tabs"><button className="is-active">Concepts</button><button>Name forms</button><button>Needs review <Tag round intent="warning">14</Tag></button></div>
        <div className="entity-result-list">
          {filtered.map((entity) => (
            <button key={entity.id} className={entity.id === selected.id ? 'entity-result is-active' : 'entity-result'} onClick={() => setSelectedId(entity.id)}>
              <span className="entity-result__plant"><Icon icon="tree" size={16} /></span>
              <span><strong>{entity.label}</strong><small>{entity.forms.slice(0, 3).join(' · ')}</small><em>{entity.id}</em></span>
              <Tag minimal intent={entity.state === 'reviewed' ? 'success' : 'warning'}>{entity.state}</Tag>
            </button>
          ))}
        </div>
      </aside>
      <main className="entity-detail">
        {fromMention && (
          <Callout className="mention-context" icon="locate" intent="primary">
            Opened from <strong>“feuel”</strong> · <button onClick={onBackToEdition}>Herbal, fol. 4r, region m4-03</button>
          </Callout>
        )}
        <header className="entity-titlebar">
          <div><span className="section-kicker">Historical concept</span><h2>{selected.label}</h2><code>{selected.id}</code></div>
          <ButtonGroup><Button icon="history" text="History" /><Button intent="primary" icon="floppy-disk" text="Save draft" /></ButtonGroup>
        </header>
        <nav className="entity-detail-tabs"><button className="is-active">Overview</button><button>Mentions <Tag round minimal>{selected.mentions}</Tag></button><button>Assertions <Tag round minimal>3</Tag></button><button>Review log</button></nav>
        <div className="entity-detail-grid">
          <section className="entity-main-column">
            <Card className="entity-card">
              <div className="entity-card__head"><div><span className="section-kicker">Vocabulary</span><h3>Name forms</h3></div><Button small icon="plus" onClick={() => setAddAliasOpen(true)}>Add form</Button></div>
              <div className="name-form-table">
                <div className="name-form-table__head"><span>Written form</span><span>Language / period</span><span>Evidence</span></div>
                {[...selected.forms, ...aliases].map((form, index) => (
                  <div key={`${form}-${index}`}><strong>{form}</strong><span>{index === 3 ? 'Chinese · 18th c.' : index === 2 ? 'Latin · early print' : 'Middle English · c.1450'}</span><button>{index === 0 ? '1 manuscript mention' : `${8 + index * 7} confirmed`}</button></div>
                ))}
              </div>
              <p className="entity-rule"><Icon icon="info-sign" size={12} /> A written form is vocabulary, not a taxonomic conclusion. OCR errors cannot mint name forms.</p>
            </Card>
            <Card className="entity-card">
              <div className="entity-card__head"><div><span className="section-kicker">Scope is identity</span><h3>Concept definition</h3></div><Tag intent="warning" minimal>working</Tag></div>
              <div className="definition-grid">
                <FormGroup label="Tradition"><HTMLSelect fill options={['Late medieval English regimen', 'Galenic materia medica', 'Early modern botany']} /></FormGroup>
                <FormGroup label="Period"><InputGroup value="c. 1400–1500" readOnly /></FormGroup>
                <FormGroup label="Region"><InputGroup value="England" readOnly /></FormGroup>
                <FormGroup label="Entity kind"><HTMLSelect fill options={['Plant preparation', 'Plant simple', 'Modern taxon', 'Unresolved substance']} /></FormGroup>
              </div>
              <FormGroup label="Scope note"><TextArea fill rows={3} defaultValue="A leaf or seed preparation named in monthly dietary regimens. Identity should not travel automatically to early modern botanical uses." /></FormGroup>
            </Card>
          </section>
          <aside className="assertion-column">
            <Card className="entity-card assertion-card">
              <div className="entity-card__head"><div><span className="section-kicker">Interpretation</span><h3>Competing assertions</h3></div><Button minimal small icon="plus" /> </div>
              <div className={`assertion-option ${decision === 'accept' ? 'is-chosen' : ''}`}>
                <div className="assertion-option__top"><Tag intent="success" minimal>likely</Tag><span>Model proposal · Mistral OCR 4</span></div>
                <strong>{selected.forms[0]}</strong><Icon icon="arrow-right" size={12} /><strong>{candidate}</strong>
                <p>Orthographic proximity; month regimen context; co-occurs with leaf pottage.</p>
                <div className="evidence-link"><Icon icon="document-open" size={12} /> Herbal, fol. 4r · region m4-03</div>
                <ProgressBar value={0.68} intent="success" stripes={false} />
                <ButtonGroup fill><Button active={decision === 'accept'} intent="success" onClick={() => setDecision('accept')}>Accept</Button><Button active={decision === 'dispute'} intent="warning" onClick={() => setDecision('dispute')}>Dispute</Button></ButtonGroup>
              </div>
              <div className="assertion-option assertion-option--alternate">
                <div className="assertion-option__top"><Tag intent="warning" minimal>possible</Tag><span>A. Reader · editorial</span></div>
                <strong>{selected.forms[0]}</strong><Icon icon="arrow-right" size={12} /><strong>Unresolved preparation</strong>
                <p>Reading may itself be unstable; the same hand forms n/u ambiguously elsewhere.</p>
                <div className="evidence-link"><Icon icon="annotation" size={12} /> Paleographic note · 12 Aug 2026</div>
              </div>
              <Divider />
              <FormGroup label="Current preferred display">
                <HTMLSelect fill value={candidate} onChange={(event) => setCandidate(event.target.value)} options={['Foeniculum vulgare Mill.', 'Unresolved preparation', 'No preferred assertion']} />
              </FormGroup>
              <Callout intent={decision === 'accept' ? 'success' : 'warning'} icon={decision === 'accept' ? 'tick-circle' : 'warning-sign'}>
                {decision === 'accept' ? 'Accepted as a new reviewed assertion. The alternative remains visible.' : 'No assertion is promoted until a named human reviews the evidence.'}
              </Callout>
            </Card>
          </aside>
        </div>
      </main>
      <Dialog isOpen={addAliasOpen} onClose={() => setAddAliasOpen(false)} title="Add a written name form" icon="text-highlight">
        <div className="bp6-dialog-body">
          <Callout icon="info-sign">Record the string as written. Link it to this concept through a reviewable assertion.</Callout>
          <FormGroup label="Written form" labelInfo="(required)"><InputGroup autoFocus value={newAlias} onChange={(event) => setNewAlias(event.target.value)} placeholder="e.g. fenicule" /></FormGroup>
          <div className="definition-grid"><FormGroup label="Language"><HTMLSelect fill options={['Middle English', 'Latin', 'English', 'German', 'Chinese']} /></FormGroup><FormGroup label="Period"><InputGroup placeholder="e.g. c. 1450" /></FormGroup></div>
          <FormGroup label="Evidence"><TextArea fill rows={3} placeholder="Page mention or external citation…" /></FormGroup>
        </div>
        <div className="bp6-dialog-footer"><div className="bp6-dialog-footer-actions"><Button onClick={() => setAddAliasOpen(false)}>Cancel</Button><Button intent="primary" disabled={!newAlias.trim()} onClick={() => { setAliases((current) => [...current, newAlias.trim()]); setNewAlias(''); setAddAliasOpen(false) }}>Add proposed form</Button></div></div>
      </Dialog>
    </div>
  )
}
