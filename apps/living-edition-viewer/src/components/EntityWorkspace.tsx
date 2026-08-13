import { useMemo, useState, type ComponentType, type ReactNode } from 'react'
import { Button, ButtonGroup, Callout, Dialog, FormGroup, HTMLSelect, Icon, InputGroup, Tag, TextArea } from '@blueprintjs/core'
import type { IconName } from '@blueprintjs/icons'
import type { DesignId } from '../types'
import {
  getEntityForms,
  getEvidence,
  getMentionCount,
  plantEntities,
  type EntityAssertion,
  type EntityEvidence,
  type EntityNameForm,
  type PlantEntity,
} from './entities/entityModel'
import './entities/EntityWorkspace.css'

interface Props {
  variant: DesignId
  fromMention: boolean
  onBackToEdition: () => void
}

type EntityBlueprintId = 'concept-record' | 'name-concordance' | 'assertion-ledger'
type ReviewDecision = 'accepted' | 'disputed'

interface EntityViewProps {
  entity: PlantEntity
  forms: EntityNameForm[]
  selectedFormId: string
  selectedAssertionId: string
  reviewDecisions: Readonly<Record<string, ReviewDecision>>
  preferredTarget: string
  onSelectForm: (id: string) => void
  onSelectAssertion: (id: string) => void
  onReview: (id: string, decision: ReviewDecision) => void
  onPreferredTarget: (target: string) => void
  onAddForm: () => void
  onOpenFolio: () => void
}

interface EntityBlueprintDefinition {
  id: EntityBlueprintId
  label: string
  shortLabel: string
  icon: IconName
  View: ComponentType<EntityViewProps>
}

function displayState(assertion: EntityAssertion, decisions: Readonly<Record<string, ReviewDecision>>) {
  return decisions[assertion.id] ?? assertion.state
}

function stateIntent(state: string): 'success' | 'warning' | 'primary' | 'none' {
  if (state === 'reviewed' || state === 'accepted' || state === 'attested') return 'success'
  if (state === 'proposed' || state === 'possible' || state === 'normalized') return 'primary'
  if (state === 'contested' || state === 'disputed') return 'warning'
  return 'none'
}

function PanelHead({ code, title, action }: { code: string; title: string; action?: ReactNode }) {
  return (
    <header className="ew-panel-head">
      <div><span>{code}</span><strong>{title}</strong></div>
      {action}
    </header>
  )
}

function FolioButton({ evidence, onOpenFolio }: { evidence: EntityEvidence; onOpenFolio: () => void }) {
  if (!evidence.folio) return <span className="ew-source-ref">{evidence.source}</span>
  return (
    <button className="ew-folio-link" onClick={onOpenFolio} title={`Open ${evidence.source}, fol. ${evidence.folio}`}>
      {evidence.source} · {evidence.folio} <Icon icon="document-open" size={11} />
    </button>
  )
}

function ReviewButtons({ assertion, decision, onReview }: { assertion: EntityAssertion; decision?: ReviewDecision; onReview: EntityViewProps['onReview'] }) {
  return (
    <ButtonGroup className="ew-review-buttons">
      <Button small active={decision === 'accepted'} intent={decision === 'accepted' ? 'success' : 'none'} icon="tick" onClick={() => onReview(assertion.id, 'accepted')}>Accept</Button>
      <Button small active={decision === 'disputed'} intent={decision === 'disputed' ? 'warning' : 'none'} icon="issue" onClick={() => onReview(assertion.id, 'disputed')}>Dispute</Button>
    </ButtonGroup>
  )
}

function ConceptRecordView({ entity, forms, reviewDecisions, preferredTarget, onReview, onPreferredTarget, onAddForm, onOpenFolio }: EntityViewProps) {
  return (
    <div className="ew-view ew-record-view">
      <div className="ew-record-main">
        <section className="ew-panel">
          <PanelHead code="01" title="Properties" />
          <div className="ew-property-grid" key={entity.id}>
            <label><span>Label</span><input defaultValue={entity.label} /></label>
            <label><span>Kind</span><select defaultValue={entity.kind}><option>Plant preparation</option><option>Plant simple</option><option>Plant product</option><option>Modern taxon</option><option>Unresolved substance</option></select></label>
            <label><span>Tradition</span><input defaultValue={entity.tradition} /></label>
            <label><span>Period</span><input defaultValue={entity.period} /></label>
            <label><span>Region</span><input defaultValue={entity.region} /></label>
            <label><span>Review state</span><input value={entity.state} readOnly /></label>
            <label className="ew-field-wide"><span>Scope note</span><textarea rows={3} defaultValue={entity.scopeNote} /></label>
          </div>
        </section>

        <section className="ew-panel">
          <PanelHead code="02" title="Name forms" action={<Button small icon="plus" onClick={onAddForm}>Add</Button>} />
          <div className="ew-grid-scroll">
            <table className="ew-data-grid ew-name-grid">
              <thead><tr><th>Written form</th><th>Language</th><th>Period</th><th>Script</th><th>Evidence</th><th>State</th></tr></thead>
              <tbody>
                {forms.map((form) => (
                  <tr key={form.id}>
                    <td className="ew-primary-cell">{form.written}</td><td>{form.language}</td><td>{form.period}</td><td>{form.script}</td>
                    <td>{form.evidenceIds.length}</td><td><Tag minimal intent={stateIntent(form.state)}>{form.state}</Tag></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="ew-rule-line"><Icon icon="info-sign" size={11} /> Historical vocabulary. OCR errors remain evidence, not forms.</div>
        </section>
      </div>

      <aside className="ew-record-side">
        <section className="ew-panel ew-assertion-stack">
          <PanelHead code="03" title="Current assertions" action={<span className="ew-count">{entity.assertions.length}</span>} />
          <label className="ew-preferred-select">
            <span>Preferred display</span>
            <select value={preferredTarget} onChange={(event) => onPreferredTarget(event.target.value)}>
              {entity.assertions.map((assertion) => <option key={assertion.id}>{assertion.target}</option>)}
              <option>No preferred assertion</option>
            </select>
          </label>
          {entity.assertions.map((assertion) => {
            const evidence = getEvidence(entity, assertion.evidenceIds)
            const state = displayState(assertion, reviewDecisions)
            return (
              <article className={`ew-claim ${reviewDecisions[assertion.id] ? 'is-decided' : ''}`} key={assertion.id}>
                <div className="ew-claim-head"><code>{assertion.id}</code><Tag minimal intent={stateIntent(state)}>{state}</Tag></div>
                <div className="ew-claim-line"><strong>{forms.find((form) => form.id === assertion.subjectFormId)?.written}</strong><span>{assertion.relation}</span><strong>{assertion.target}</strong></div>
                <p>{assertion.rationale}</p>
                <div className="ew-claim-meta"><span>{Math.round(assertion.confidence * 100)}%</span><span>{assertion.origin}</span></div>
                {evidence[0] && <FolioButton evidence={evidence[0]} onOpenFolio={onOpenFolio} />}
                <ReviewButtons assertion={assertion} decision={reviewDecisions[assertion.id]} onReview={onReview} />
              </article>
            )
          })}
        </section>
      </aside>
    </div>
  )
}

function NameConcordanceView({ entity, forms, selectedFormId, onSelectForm, onAddForm, onOpenFolio }: EntityViewProps) {
  const selectedForm = forms.find((form) => form.id === selectedFormId) ?? forms[0]
  const evidence = getEvidence(entity, selectedForm.evidenceIds)

  return (
    <div className="ew-view ew-concordance-view">
      <section className="ew-panel ew-concordance-index">
        <PanelHead code="NC" title="Written forms" action={<Button small icon="plus" onClick={onAddForm}>Add</Button>} />
        <table className="ew-data-grid ew-select-grid">
          <thead><tr><th>Form</th><th>Language</th><th>Period</th><th>Mentions</th><th>State</th></tr></thead>
          <tbody>
            {forms.map((form) => (
              <tr key={form.id} className={form.id === selectedForm.id ? 'is-selected' : ''} onClick={() => onSelectForm(form.id)}>
                <td><button className="ew-cell-button" onClick={() => onSelectForm(form.id)}>{form.written}</button></td>
                <td>{form.language}</td><td>{form.period}</td><td>{form.evidenceIds.length}</td><td>{form.state}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <dl className="ew-form-summary">
          <div><dt>Form ID</dt><dd>{selectedForm.id}</dd></div>
          <div><dt>Script</dt><dd>{selectedForm.script}</dd></div>
          <div><dt>Class</dt><dd>{selectedForm.state}</dd></div>
          <div><dt>Links</dt><dd>{selectedForm.evidenceIds.length}</dd></div>
        </dl>
      </section>

      <section className="ew-panel ew-evidence-panel">
        <PanelHead code="EV" title={`Mention evidence — ${selectedForm.written}`} action={<span className="ew-count">{evidence.length}</span>} />
        {evidence.length ? (
          <table className="ew-data-grid ew-evidence-grid">
            <thead><tr><th>Source / folio</th><th>Region</th><th>Hand</th><th>Reading in context</th><th>Review</th><th>Date</th></tr></thead>
            <tbody>
              {evidence.map((item) => (
                <tr key={item.id}>
                  <td><FolioButton evidence={item} onOpenFolio={onOpenFolio} /></td>
                  <td><code>{item.region ?? '—'}</code></td><td>{item.hand ?? '—'}</td><td className="ew-context-cell">{item.excerpt}</td><td>{item.reviewer}</td><td>{item.date}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="ew-empty-pane"><Icon icon="link" size={16} /><strong>No linked mention</strong><span>{selectedForm.note ?? 'Evidence not supplied.'}</span></div>
        )}
        <div className="ew-evidence-inspector">
          <div><span>Selected form</span><strong>{selectedForm.written}</strong></div>
          <div><span>Language / period</span><strong>{selectedForm.language} · {selectedForm.period}</strong></div>
          <div><span>Witness coverage</span><strong>{new Set(evidence.map((item) => item.source)).size} source(s)</strong></div>
          <div><span>Folio links</span><strong>{evidence.filter((item) => item.kind === 'folio').length}</strong></div>
        </div>
      </section>
    </div>
  )
}

function AssertionLedgerView({ entity, forms, selectedAssertionId, reviewDecisions, onSelectAssertion, onReview, onOpenFolio }: EntityViewProps) {
  const selectedAssertion = entity.assertions.find((assertion) => assertion.id === selectedAssertionId) ?? entity.assertions[0]
  const evidence = getEvidence(entity, selectedAssertion.evidenceIds)
  const selectedState = displayState(selectedAssertion, reviewDecisions)

  return (
    <div className="ew-view ew-ledger-view">
      <section className="ew-panel ew-ledger-grid-panel">
        <PanelHead code="AL" title="Assertion ledger" action={<span className="ew-count">{entity.assertions.length}</span>} />
        <table className="ew-data-grid ew-ledger-grid">
          <thead><tr><th>ID</th><th>Subject</th><th>Relation</th><th>Target</th><th>Conf.</th><th>State</th><th>Origin</th><th>Evidence</th><th>Modified</th></tr></thead>
          <tbody>
            {entity.assertions.map((assertion) => {
              const state = displayState(assertion, reviewDecisions)
              return (
                <tr key={assertion.id} className={assertion.id === selectedAssertion.id ? 'is-selected' : ''} onClick={() => onSelectAssertion(assertion.id)}>
                  <td><button className="ew-cell-button ew-code-button" onClick={() => onSelectAssertion(assertion.id)}>{assertion.id}</button></td>
                  <td>{forms.find((form) => form.id === assertion.subjectFormId)?.written}</td><td>{assertion.relation}</td><td className="ew-primary-cell">{assertion.target}</td>
                  <td>{Math.round(assertion.confidence * 100)}%</td><td><Tag minimal intent={stateIntent(state)}>{state}</Tag></td><td>{assertion.origin}</td><td>{assertion.evidenceIds.length}</td><td>{assertion.modified}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </section>

      <aside className="ew-panel ew-ledger-detail">
        <PanelHead code="DT" title="Claim detail" action={<Tag minimal intent={stateIntent(selectedState)}>{selectedState}</Tag>} />
        <dl className="ew-detail-list">
          <div><dt>Assertion</dt><dd><code>{selectedAssertion.id}</code></dd></div>
          <div><dt>Subject</dt><dd>{forms.find((form) => form.id === selectedAssertion.subjectFormId)?.written}</dd></div>
          <div><dt>Predicate</dt><dd>{selectedAssertion.relation}</dd></div>
          <div><dt>Object</dt><dd>{selectedAssertion.target}</dd></div>
          <div><dt>Authority ID</dt><dd><code>{selectedAssertion.targetId ?? '—'}</code></dd></div>
          <div><dt>Confidence</dt><dd>{Math.round(selectedAssertion.confidence * 100)}%</dd></div>
          <div><dt>Origin</dt><dd>{selectedAssertion.origin}</dd></div>
          <div><dt>Reviewer</dt><dd>{selectedAssertion.reviewer}</dd></div>
        </dl>
        <div className="ew-rationale"><span>Rationale</span><p>{selectedAssertion.rationale}</p></div>
        <div className="ew-detail-evidence">
          <span>Evidence</span>
          {evidence.map((item) => <div key={item.id}><FolioButton evidence={item} onOpenFolio={onOpenFolio} /><small>{item.excerpt}</small></div>)}
        </div>
        <ReviewButtons assertion={selectedAssertion} decision={reviewDecisions[selectedAssertion.id]} onReview={onReview} />
      </aside>
    </div>
  )
}

const entityBlueprints = [
  { id: 'concept-record', label: 'Concept Record', shortLabel: 'Record', icon: 'properties', View: ConceptRecordView },
  { id: 'name-concordance', label: 'Name Concordance', shortLabel: 'Concordance', icon: 'th-list', View: NameConcordanceView },
  { id: 'assertion-ledger', label: 'Assertion Ledger', shortLabel: 'Ledger', icon: 'changes', View: AssertionLedgerView },
] satisfies readonly EntityBlueprintDefinition[]

export function EntityWorkspace({ variant, fromMention, onBackToEdition }: Props) {
  const initialEntity = fromMention ? plantEntities[0] : plantEntities[1]
  const [blueprintId, setBlueprintId] = useState<EntityBlueprintId>(() => {
    const saved = window.localStorage.getItem('whl-design.entity-mode')
    return entityBlueprints.some((item) => item.id === saved)
      ? saved as EntityBlueprintId
      : 'concept-record'
  })
  const [query, setQuery] = useState(fromMention ? 'feuel' : '')
  const [selectedId, setSelectedId] = useState(initialEntity.id)
  const [selectedFormId, setSelectedFormId] = useState(initialEntity.forms[0].id)
  const [selectedAssertionId, setSelectedAssertionId] = useState(initialEntity.currentAssertionId)
  const [addedForms, setAddedForms] = useState<Record<string, EntityNameForm[]>>({})
  const [reviewDecisions, setReviewDecisions] = useState<Record<string, ReviewDecision>>({})
  const [preferredTargets, setPreferredTargets] = useState<Record<string, string>>({})
  const [addFormOpen, setAddFormOpen] = useState(false)
  const [newForm, setNewForm] = useState('')
  const [newLanguage, setNewLanguage] = useState('Middle English')
  const [newPeriod, setNewPeriod] = useState('c. 1450')
  const [newEvidence, setNewEvidence] = useState('')

  const selected = plantEntities.find((entity) => entity.id === selectedId) ?? plantEntities[0]
  const forms = getEntityForms(selected, addedForms[selected.id] ?? [])
  const currentAssertion = selected.assertions.find((assertion) => assertion.id === selected.currentAssertionId) ?? selected.assertions[0]
  const preferredTarget = preferredTargets[selected.id] ?? currentAssertion.target
  const activeBlueprint = entityBlueprints.find((item) => item.id === blueprintId) ?? entityBlueprints[0]
  const ActiveView = activeBlueprint.View

  const filtered = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase()
    if (!normalized) return plantEntities
    return plantEntities.filter((entity) => {
      const allForms = getEntityForms(entity, addedForms[entity.id] ?? [])
      return `${entity.id} ${entity.label} ${entity.kind} ${allForms.map((form) => form.written).join(' ')}`.toLocaleLowerCase().includes(normalized)
    })
  }, [addedForms, query])

  const chooseEntity = (id: string) => {
    const entity = plantEntities.find((item) => item.id === id)
    if (!entity) return
    setSelectedId(id)
    setSelectedFormId(entity.forms[0].id)
    setSelectedAssertionId(entity.currentAssertionId)
  }

  const reviewAssertion = (id: string, decision: ReviewDecision) => {
    setReviewDecisions((current) => ({ ...current, [id]: decision }))
    if (decision === 'accepted') {
      const assertion = selected.assertions.find((item) => item.id === id)
      if (assertion) setPreferredTargets((current) => ({ ...current, [selected.id]: assertion.target }))
    }
  }

  const addNameForm = () => {
    const written = newForm.trim()
    if (!written) return
    const id = `user-form-${Date.now()}`
    const added: EntityNameForm = {
      id,
      written,
      language: newLanguage,
      period: newPeriod.trim() || 'Unspecified',
      script: 'Latin',
      state: 'proposed',
      evidenceIds: [],
      note: newEvidence.trim() || 'Evidence not supplied.',
    }
    setAddedForms((current) => ({ ...current, [selected.id]: [...(current[selected.id] ?? []), added] }))
    setSelectedFormId(id)
    setNewForm('')
    setNewEvidence('')
    setAddFormOpen(false)
  }

  const nearestFolio = selected.evidence.find((item) => item.kind === 'folio')

  return (
    <div className={`entity-workspace entity-workspace--${variant} ew-shell`}>
      <header className="ew-command-bar">
        <div className="ew-app-label"><Icon icon="tree" size={15} /><strong>Plant Entities</strong><span>Authority editor</span></div>
        <div className="ew-blueprint-switch" role="tablist" aria-label="Entity blueprint">
          <span>Blueprint</span>
          {entityBlueprints.map((item) => (
            <button key={item.id} role="tab" aria-selected={item.id === blueprintId} className={item.id === blueprintId ? 'is-active' : ''} onClick={() => {
              setBlueprintId(item.id)
              window.localStorage.setItem('whl-design.entity-mode', item.id)
            }} title={item.label}>
              <Icon icon={item.icon} size={12} />{item.shortLabel}
            </button>
          ))}
        </div>
        <div className="ew-command-actions">
          <Button small icon="plus" onClick={() => setAddFormOpen(true)}>Add form</Button>
          <Button small intent="primary" icon="floppy-disk">Save</Button>
        </div>
      </header>

      <div className="ew-body">
        <aside className="ew-index">
          <div className="ew-index-head">
            <label>Find concept or form</label>
            <InputGroup small leftIcon="search" placeholder="Name or ID" value={query} onChange={(event) => setQuery(event.target.value)} rightElement={query ? <Button minimal small icon="cross" aria-label="Clear search" onClick={() => setQuery('')} /> : undefined} />
          </div>
          <div className="ew-index-summary"><span>Concepts</span><strong>{filtered.length}</strong><span>Review queue</span><strong>14</strong></div>
          <div className="ew-result-list" aria-label="Plant concepts">
            {filtered.map((entity) => (
              <button key={entity.id} className={entity.id === selected.id ? 'ew-result is-selected' : 'ew-result'} onClick={() => chooseEntity(entity.id)}>
                <span className="ew-result-state" data-state={entity.state} />
                <span className="ew-result-text"><strong>{entity.label}</strong><small>{getEntityForms(entity, addedForms[entity.id] ?? []).slice(0, 3).map((form) => form.written).join(' · ')}</small><code>{entity.id}</code></span>
                <span className="ew-result-count">{getMentionCount(entity)}</span>
              </button>
            ))}
            {!filtered.length && <div className="ew-no-results"><Icon icon="search" /><span>No matches.</span><button onClick={() => setQuery('')}>Clear</button></div>}
          </div>
        </aside>

        <main className="ew-work-area">
          {fromMention && (
            <div className="ew-source-strip">
              <Icon icon="locate" size={12} /><span>Source</span><strong>“feuel”</strong><code>Herbal / 4r / m4-03</code><button onClick={onBackToEdition}><Icon icon="arrow-left" size={11} /> Folio</button>
            </div>
          )}
          <header className="ew-record-title">
            <div><span>{selected.kind}</span><h2>{selected.label}</h2><code>{selected.id}</code></div>
            <div className="ew-record-meta"><Tag minimal intent={stateIntent(selected.state)}>{selected.state}</Tag><span>{forms.length} forms</span><span>{selected.assertions.length} claims</span>{nearestFolio && <button onClick={onBackToEdition}>Folio {nearestFolio.folio}</button>}</div>
          </header>
          <ActiveView
            entity={selected}
            forms={forms}
            selectedFormId={selectedFormId}
            selectedAssertionId={selectedAssertionId}
            reviewDecisions={reviewDecisions}
            preferredTarget={preferredTarget}
            onSelectForm={setSelectedFormId}
            onSelectAssertion={setSelectedAssertionId}
            onReview={reviewAssertion}
            onPreferredTarget={(target) => setPreferredTargets((current) => ({ ...current, [selected.id]: target }))}
            onAddForm={() => setAddFormOpen(true)}
            onOpenFolio={onBackToEdition}
          />
        </main>
      </div>

      <footer className="ew-status-bar"><span>Authority: external POC</span><span>Mode: {activeBlueprint.label}</span><span>{filtered.length} visible / {plantEntities.length} total</span><span className="ew-status-ready">Ready</span></footer>

      <Dialog className="ew-form-dialog" isOpen={addFormOpen} onClose={() => setAddFormOpen(false)} title="Add name form" icon="form">
        <div className="bp6-dialog-body">
          <Callout compact icon="info-sign">Record the written form. Link identity through an assertion.</Callout>
          <FormGroup label="Written form" labelInfo="Required"><InputGroup autoFocus value={newForm} onChange={(event) => setNewForm(event.target.value)} /></FormGroup>
          <div className="ew-dialog-grid">
            <FormGroup label="Language"><HTMLSelect fill value={newLanguage} onChange={(event) => setNewLanguage(event.target.value)} options={['Middle English', 'Latin', 'English', 'French', 'German', 'Italian', 'Chinese']} /></FormGroup>
            <FormGroup label="Period"><InputGroup value={newPeriod} onChange={(event) => setNewPeriod(event.target.value)} /></FormGroup>
          </div>
          <FormGroup label="Evidence note"><TextArea fill rows={3} value={newEvidence} onChange={(event) => setNewEvidence(event.target.value)} placeholder="Folio, citation, or editorial note" /></FormGroup>
        </div>
        <div className="bp6-dialog-footer"><div className="bp6-dialog-footer-actions"><Button onClick={() => setAddFormOpen(false)}>Cancel</Button><Button intent="primary" disabled={!newForm.trim()} onClick={addNameForm}>Add</Button></div></div>
      </Dialog>
    </div>
  )
}
