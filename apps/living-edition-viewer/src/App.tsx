import { useMemo, useState } from 'react'
import {
  Alignment,
  Button,
  ButtonGroup,
  Card,
  Checkbox,
  Divider,
  Icon,
  Menu,
  MenuItem,
  Navbar,
  NavbarDivider,
  NavbarGroup,
  NavbarHeading,
  Popover,
  Switch,
  Tab,
  Tabs,
  Tag,
} from '@blueprintjs/core'
import { DesignPicker } from './components/DesignPicker'
import { DesktopWorkbench } from './components/DesktopWorkbench'
import { EntityWorkspace } from './components/EntityWorkspace'
import { FeedbackPanel } from './components/FeedbackPanel'
import { LayerText } from './components/LayerText'
import { LibraryBrowser } from './components/LibraryBrowser'
import { ManuscriptCanvas } from './components/ManuscriptCanvas'
import { NotesEditor } from './components/NotesEditor'
import { RegionInspector, RegionTree } from './components/RegionTools'
import { ReprocessPanel } from './components/ReprocessPanel'
import { WorkspaceNav } from './components/WorkspaceNav'
import { designs, initialRegions, queueItems } from './data/mockData'
import {
  activeManuscript,
  layerDefinitions,
  matrixFocusDefinitions,
  overlayLayerDefinitions,
  regionTypeDefinitions,
  workbenchLayoutDefinitions,
} from './data/registries'
import type { DesignId, DrawMode, LayerId, MatrixFocusId, NoteScope, Region, RegionType, Workspace } from './types'

type FeedbackMap = Record<DesignId, string>

function readStored<T>(key: string, fallback: T): T {
  try {
    const stored = window.localStorage.getItem(key)
    return stored ? JSON.parse(stored) as T : fallback
  } catch {
    return fallback
  }
}

function App() {
  const [designId, setDesignId] = useState<DesignId>(() => readStored('whl.mockup.design', 'scriptorium'))
  const [workspace, setWorkspaceState] = useState<Workspace>('edition')
  const [fromMention, setFromMention] = useState(false)
  const [shortlist, setShortlist] = useState<DesignId[]>(() => readStored('whl.mockup.shortlist', []))
  const [feedback, setFeedback] = useState<FeedbackMap>(() => readStored(
    'whl.mockup.feedback',
    Object.fromEntries(designs.map((item) => [item.id, ''])) as FeedbackMap,
  ))
  const [regions, setRegions] = useState<Region[]>(initialRegions)
  const [selectedRegionId, setSelectedRegionId] = useState<string | null>('m4-03')
  const [drawMode, setDrawMode] = useState<DrawMode>('select')
  const [visibleLayerIds, setVisibleLayerIds] = useState<LayerId[]>(() => layerDefinitions.filter((layer) => layer.defaultVisible).map((layer) => layer.id))
  const [activeTypeId, setActiveTypeId] = useState('hand-a')
  const [types, setTypes] = useState<RegionType[]>(() => [...regionTypeDefinitions])
  const [noteScope, setNoteScope] = useState<NoteScope>('region')
  const [notes, setNotes] = useState<Record<NoteScope, string>>({
    book: 'The calendar section may have been copied from a separate exemplar. Compare opening rubric with fols. 5r–8v.',
    page: 'Foliation reads “iii” in a later pencil hand. Keep it separate from the manuscript folio label.',
    region: 'Possible change of hand here: rounder d, taller ascenders, and darker ink. Reprocess as Hand B without merging the marginal note.',
  })
  const [sideTab, setSideTab] = useState<'region' | 'notes' | 'reprocess'>('region')
  const [queueSelection, setQueueSelection] = useState(queueItems[0].id)
  const [matrixFocus, setMatrixFocus] = useState<MatrixFocusId>('text')

  const design = designs.find((candidate) => candidate.id === designId) ?? designs[0]
  const workbenchLayout = workbenchLayoutDefinitions.find((candidate) => candidate.designId === designId)
  const selectedRegion = regions.find((region) => region.id === selectedRegionId) ?? null
  const regionCounts = useMemo(() => regions.reduce<Record<string, number>>((counts, region) => {
    counts[region.typeId] = (counts[region.typeId] ?? 0) + 1
    return counts
  }, {}), [regions])

  const persistDesign = (id: DesignId) => {
    setDesignId(id)
    window.localStorage.setItem('whl.mockup.design', JSON.stringify(id))
  }

  const toggleShortlist = (id: DesignId) => {
    const next = shortlist.includes(id) ? shortlist.filter((item) => item !== id) : [...shortlist, id]
    setShortlist(next)
    window.localStorage.setItem('whl.mockup.shortlist', JSON.stringify(next))
  }

  const changeFeedback = (id: DesignId, value: string) => {
    const next = { ...feedback, [id]: value }
    setFeedback(next)
    window.localStorage.setItem('whl.mockup.feedback', JSON.stringify(next))
  }

  const setWorkspace = (next: Workspace) => {
    setWorkspaceState(next)
    if (next === 'entities') setFromMention(false)
  }

  const openEntityFromMention = () => {
    setFromMention(true)
    setWorkspaceState('entities')
  }

  const updateRegion = (updated: Region) => setRegions((current) => current.map((region) => region.id === updated.id ? updated : region))
  const addRegion = (region: Region) => { setRegions((current) => [...current, region]); setSelectedRegionId(region.id); setSideTab('region') }
  const deleteRegion = (id: string) => { setRegions((current) => current.filter((region) => region.id !== id)); setSelectedRegionId(null) }
  const addSubclass = (parentId: string, name: string) => {
    const parent = types.find((type) => type.id === parentId)
    const id = `custom-${Date.now()}`
    setTypes((current) => [...current, { id, parentId, name, color: parent?.color ?? '#738091' }])
    setActiveTypeId(id)
  }

  const setLayerVisibility = (id: LayerId, visible: boolean) => {
    setVisibleLayerIds((current) => visible
      ? current.includes(id) ? current : [...current, id]
      : current.filter((layerId) => layerId !== id))
  }

  const showRegions = visibleLayerIds.includes('regions')
  const setShowRegions = (visible: boolean) => setLayerVisibility('regions', visible)

  const canvasProps = {
    regions,
    selectedId: selectedRegionId,
    drawMode,
    activeTypeId,
    regionTypes: types,
    showRegions,
    onSelect: setSelectedRegionId,
    onChangeDrawMode: setDrawMode,
    onAddRegion: addRegion,
  }

  const commonWorkspace = workspace === 'library'
    ? <LibraryBrowser key={designId} variant={designId} onOpenEdition={() => setWorkspaceState('edition')} />
    : workspace === 'entities'
      ? <EntityWorkspace key={designId} variant={designId} fromMention={fromMention} onBackToEdition={() => setWorkspaceState('edition')} />
      : null

  return (
    <div className={`gallery-app gallery-app--${designId}`}>
      <Navbar className="review-navbar">
        <NavbarGroup align={Alignment.LEFT}>
          <span className="review-mark"><Icon icon="tree" size={18} /></span>
          <NavbarHeading>World Herb Library</NavbarHeading>
          <NavbarDivider />
          <span className="review-title">Living Edition</span>
        </NavbarGroup>
        <NavbarGroup align={Alignment.RIGHT}>
          <Tag intent="warning" icon="changes">Design review 1/3</Tag>
          <span className="review-progress"><i /><i /><i /></span>
          <Popover content={<Menu><MenuItem icon="export" text="Export review notes" /><MenuItem icon="reset" text="Clear local feedback" onClick={() => { window.localStorage.removeItem('whl.mockup.feedback'); window.location.reload() }} /></Menu>} placement="bottom-end">
            <Button minimal icon="more" aria-label="Review menu" />
          </Popover>
        </NavbarGroup>
      </Navbar>

      <main className="gallery-main">
        <header className="gallery-intro">
          <div>
            <span className="eyebrow">Interface study</span>
            <h1>Living Edition layouts</h1>
            <p>Seven light desktop variants. Test navigation, geometry, text, notes, processing, and entities.</p>
          </div>
          <Card className="review-instructions">
            <span>Review stage</span>
            <ol><li className="is-current">Explore</li><li>Refine</li><li>Settle</li></ol>
            <small>Preferences are stored locally.</small>
          </Card>
        </header>

        <DesignPicker designs={designs} selected={designId} shortlist={shortlist} onSelect={persistDesign} onShortlist={toggleShortlist} />

        <section className={`concept-stage concept-stage--${designId}`} aria-label={`${design.title} interactive mockup`}>
          <div className="concept-caption">
            <div><span className="concept-caption__marker">{design.marker}</span><span><strong>{design.title}</strong><small>{design.subtitle}</small></span></div>
            <div className="concept-caption__tips"><Icon icon="hand" /> Switch workspace. Draw region. Edit text. Open entity.</div>
            <Tag minimal icon="desktop">Desktop preview</Tag>
          </div>

          {designId === 'scriptorium' && (
            <Scriptorium
              workspace={workspace}
              setWorkspace={setWorkspace}
              commonWorkspace={commonWorkspace}
              canvasProps={canvasProps}
              selectedRegion={selectedRegion}
              types={types}
              onUpdateRegion={updateRegion}
              onDeleteRegion={deleteRegion}
              onOpenEntity={openEntityFromMention}
              noteScope={noteScope}
              setNoteScope={setNoteScope}
              notes={notes}
              setNotes={setNotes}
              showRegions={showRegions}
              setShowRegions={setShowRegions}
              visibleLayerIds={visibleLayerIds}
              setLayerVisibility={setLayerVisibility}
              sideTab={sideTab}
              setSideTab={setSideTab}
            />
          )}
          {designId === 'spatial' && (
            <SpatialLab
              workspace={workspace}
              setWorkspace={setWorkspace}
              commonWorkspace={commonWorkspace}
              canvasProps={canvasProps}
              selectedRegion={selectedRegion}
              types={types}
              activeTypeId={activeTypeId}
              setActiveTypeId={setActiveTypeId}
              regionCounts={regionCounts}
              addSubclass={addSubclass}
              onUpdateRegion={updateRegion}
              onDeleteRegion={deleteRegion}
              onOpenEntity={openEntityFromMention}
              noteScope={noteScope}
              setNoteScope={setNoteScope}
              notes={notes}
              setNotes={setNotes}
              showRegions={showRegions}
              setShowRegions={setShowRegions}
              visibleLayerIds={visibleLayerIds}
            />
          )}
          {designId === 'queue' && (
            <ReviewQueue
              workspace={workspace}
              setWorkspace={setWorkspace}
              commonWorkspace={commonWorkspace}
              canvasProps={canvasProps}
              selectedRegion={selectedRegion}
              types={types}
              onUpdateRegion={updateRegion}
              onDeleteRegion={deleteRegion}
              onOpenEntity={openEntityFromMention}
              queueSelection={queueSelection}
              setQueueSelection={setQueueSelection}
            />
          )}
          {designId === 'matrix' && (
            <LayerMatrix
              workspace={workspace}
              setWorkspace={setWorkspace}
              commonWorkspace={commonWorkspace}
              canvasProps={canvasProps}
              selectedRegion={selectedRegion}
              types={types}
              onUpdateRegion={updateRegion}
              onDeleteRegion={deleteRegion}
              onOpenEntity={openEntityFromMention}
              focus={matrixFocus}
              setFocus={setMatrixFocus}
            />
          )}
          {workbenchLayout && (
            <DesktopWorkbench
              layout={workbenchLayout}
              workspace={workspace}
              setWorkspace={setWorkspace}
              commonWorkspace={commonWorkspace}
              canvasProps={canvasProps}
              selectedRegion={selectedRegion}
              types={types}
              activeTypeId={activeTypeId}
              setActiveTypeId={setActiveTypeId}
              regionCounts={regionCounts}
              addSubclass={addSubclass}
              onUpdateRegion={updateRegion}
              onDeleteRegion={deleteRegion}
              onOpenEntity={openEntityFromMention}
              noteScope={noteScope}
              setNoteScope={setNoteScope}
              notes={notes}
              setNotes={setNotes}
              visibleLayerIds={visibleLayerIds}
              setLayerVisibility={setLayerVisibility}
            />
          )}
        </section>

        <FeedbackPanel
          design={design}
          feedback={feedback[designId]}
          shortlisted={shortlist.includes(designId)}
          onChange={(value) => changeFeedback(designId, value)}
          onShortlist={() => toggleShortlist(designId)}
        />
      </main>
    </div>
  )
}

interface SharedEditionProps {
  workspace: Workspace
  setWorkspace: (workspace: Workspace) => void
  commonWorkspace: React.ReactNode
  canvasProps: React.ComponentProps<typeof ManuscriptCanvas>
  selectedRegion: Region | null
  types: RegionType[]
  onUpdateRegion: (region: Region) => void
  onDeleteRegion: (id: string) => void
  onOpenEntity: () => void
}

interface NoteProps {
  noteScope: NoteScope
  setNoteScope: (scope: NoteScope) => void
  notes: Record<NoteScope, string>
  setNotes: React.Dispatch<React.SetStateAction<Record<NoteScope, string>>>
}

function EditionChrome({ children, label = activeManuscript.label }: { children: React.ReactNode; label?: string }) {
  return (
    <div className="edition-chrome">
      <div className="edition-breadcrumb"><Icon icon="book" size={13} /><span>{label}</span><Icon icon="chevron-right" size={11} /><strong>{activeManuscript.folio}</strong><Tag minimal intent="warning">draft · rev. 3</Tag></div>
      <div className="edition-page-tools">
        <ButtonGroup minimal><Button icon="chevron-left" small aria-label="Previous page" /><Button small>4r / 55</Button><Button icon="chevron-right" small aria-label="Next page" /></ButtonGroup>
        <Divider />
        <Tag minimal icon="history">Saved 1 min ago</Tag>
        <Popover content={<Menu><MenuItem icon="git-branch" text="Compare revision 2" /><MenuItem icon="endorsed" text="Freeze release candidate" /></Menu>} placement="bottom-end"><Button minimal small icon="more" aria-label="Page menu" /></Popover>
      </div>
      {children}
    </div>
  )
}

function Scriptorium(props: SharedEditionProps & NoteProps & { showRegions: boolean; setShowRegions: (value: boolean) => void; visibleLayerIds: LayerId[]; setLayerVisibility: (id: LayerId, visible: boolean) => void; sideTab: 'region' | 'notes' | 'reprocess'; setSideTab: (tab: 'region' | 'notes' | 'reprocess') => void }) {
  return (
    <div className="mockup-window scriptorium-window">
      <header className="scriptorium-header"><div className="scriptorium-brand"><span className="brand-seal"><Icon icon="tree" /></span><span><strong>Living Editions</strong><small>World Herb Library</small></span></div><WorkspaceNav value={props.workspace} onChange={props.setWorkspace} /><div className="scriptorium-user"><Button minimal icon="notifications" /><span>AM</span></div></header>
      {props.commonWorkspace ?? (
        <EditionChrome>
          <div className="scriptorium-layerbar">
            <span>View</span>
            {overlayLayerDefinitions.map((layer) => (
              <Checkbox
                key={layer.id}
                inline
                checked={props.visibleLayerIds.includes(layer.id)}
                onChange={(event) => props.setLayerVisibility(layer.id, event.currentTarget.checked)}
                label={layer.shortLabel}
              />
            ))}
            <span className="scriptorium-layerbar__spacer" /><Button small icon="comment" text="3 unresolved" /><Button small intent="primary" icon="tick" text="Submit passage" />
          </div>
          <div className="scriptorium-body">
            <ManuscriptCanvas {...props.canvasProps} />
            <LayerText selectedRegionId={props.selectedRegion?.id ?? null} onOpenEntity={props.onOpenEntity} />
            <aside className="scriptorium-inspector">
              <Tabs selectedTabId={props.sideTab} onChange={(id) => props.setSideTab(id as 'region' | 'notes' | 'reprocess')} animate={false}>
                <Tab id="region" title={<Icon icon="selection" />} panel={<RegionInspector region={props.selectedRegion} types={props.types} onChange={props.onUpdateRegion} onDelete={props.onDeleteRegion} onOpenEntity={props.onOpenEntity} />} />
                <Tab id="notes" title={<Icon icon="annotation" />} panel={<NotesEditor scope={props.noteScope} value={props.notes[props.noteScope]} selectedRegionLabel={props.selectedRegion?.label} onChangeScope={props.setNoteScope} onChange={(value) => props.setNotes((current) => ({ ...current, [props.noteScope]: value }))} />} />
                <Tab id="reprocess" title={<Icon icon="refresh" />} panel={<ReprocessPanel />} />
              </Tabs>
            </aside>
          </div>
        </EditionChrome>
      )}
    </div>
  )
}

function SpatialLab(props: SharedEditionProps & NoteProps & { activeTypeId: string; setActiveTypeId: (id: string) => void; regionCounts: Record<string, number>; addSubclass: (parentId: string, name: string) => void; showRegions: boolean; setShowRegions: (value: boolean) => void; visibleLayerIds: LayerId[] }) {
  return (
    <div className="mockup-window spatial-window">
      <WorkspaceNav value={props.workspace} onChange={props.setWorkspace} mode="rail" />
      <div className="spatial-main">
        <header className="spatial-topbar"><div><span className="section-kicker">Spatial Lab</span><strong>{props.workspace === 'edition' ? `${activeManuscript.label} · ${activeManuscript.folio}` : props.workspace === 'library' ? 'Library' : 'Plant entities'}</strong></div><div><Tag minimal icon="layers">{props.visibleLayerIds.length} layers</Tag><Switch inline large checked={props.showRegions} label="Regions" onChange={(event) => props.setShowRegions(event.currentTarget.checked)} /><Button intent="primary" icon="floppy-disk">Save</Button></div></header>
        {props.commonWorkspace ?? (
          <div className="spatial-workarea">
            <aside className="spatial-left"><RegionTree types={props.types} activeTypeId={props.activeTypeId} regionCounts={props.regionCounts} onSelect={props.setActiveTypeId} onAddSubclass={props.addSubclass} /><Divider /><div className="spatial-object-list"><div className="section-heading"><div><span className="section-kicker">Page objects</span><h3>Regions</h3></div><Tag round>{props.canvasProps.regions.length}</Tag></div>{props.canvasProps.regions.map((region) => <button key={region.id} className={props.selectedRegion?.id === region.id ? 'is-active' : ''} onClick={() => props.canvasProps.onSelect(region.id)}><span style={{ borderColor: region.color }} /><strong>{region.label}</strong><small>{region.source}</small></button>)}</div></aside>
            <main className="spatial-canvas"><ManuscriptCanvas {...props.canvasProps} /><div className="spatial-status"><span>X 512 · Y 884</span><span>Zoom 43%</span><span>{props.canvasProps.drawMode === 'select' ? 'Select' : props.canvasProps.drawMode}</span><Tag minimal>{props.types.find((type) => type.id === props.activeTypeId)?.name}</Tag></div></main>
            <aside className="spatial-right"><RegionInspector region={props.selectedRegion} types={props.types} onChange={props.onUpdateRegion} onDelete={props.onDeleteRegion} onOpenEntity={props.onOpenEntity} /><Divider /><NotesEditor scope={props.noteScope} value={props.notes[props.noteScope]} selectedRegionLabel={props.selectedRegion?.label} onChangeScope={props.setNoteScope} onChange={(value) => props.setNotes((current) => ({ ...current, [props.noteScope]: value }))} /></aside>
            <div className="spatial-bottom-drawer"><div className="drawer-handle"><span><Icon icon="comparison" /> Aligned text</span><Tag minimal>Mistral 4 ↔ Edited ↔ English</Tag><Button minimal small icon="chevron-down" /></div><LayerText dense selectedRegionId={props.selectedRegion?.id ?? null} onOpenEntity={props.onOpenEntity} /></div>
          </div>
        )}
      </div>
    </div>
  )
}

function ReviewQueue(props: SharedEditionProps & { queueSelection: string; setQueueSelection: (id: string) => void }) {
  const [decision, setDecision] = useState<'pending' | 'accepted' | 'skipped'>('pending')
  return (
    <div className="mockup-window queue-window">
      <header className="queue-header"><div className="queue-brand"><span><Icon icon="flow-review" /></span><strong>Editorial Review</strong></div><WorkspaceNav value={props.workspace} onChange={props.setWorkspace} mode="compact" /><div><Tag intent="warning" round>14 need attention</Tag><Button minimal icon="user" text="A. Miller" /></div></header>
      {props.commonWorkspace ?? (
        <div className="queue-layout">
          <aside className="queue-list">
            <div className="queue-list__head"><span className="section-kicker">Queue</span><h2>Open items</h2><p>Impact order</p></div>
            <div className="queue-filters"><Button small active>All 14</Button><Button small>Layout 5</Button><Button small>Text 6</Button><Button small>Entities 3</Button></div>
            {queueItems.map((item, index) => <button key={item.id} className={props.queueSelection === item.id ? 'queue-item is-active' : 'queue-item'} onClick={() => { props.setQueueSelection(item.id); setDecision('pending') }}><span className={`queue-item__priority is-${item.severity}`}>{index + 1}</span><span><em>{item.kind}</em><strong>{item.title}</strong><small>{item.meta}</small></span><Icon icon="chevron-right" size={12} /></button>)}
            <div className="queue-progress"><span><strong>8</strong> reviewed today</span><span className="mini-progress"><i style={{ width: '57%' }} /></span></div>
          </aside>
          <main className="queue-focus">
            <header><div><Tag intent="danger" minimal>High impact</Tag><h2>Rubric merged with opening line</h2><p>Color and function change. Split before anchor update.</p></div><Button minimal icon="cross" aria-label="Close task" /></header>
            <div className="queue-evidence"><div className="queue-image"><span className="evidence-label">Page evidence</span><ManuscriptCanvas {...props.canvasProps} crop /></div><div className="queue-comparison"><span className="evidence-label">Engine comparison</span><div className="comparison-card"><div><strong>Mistral OCR 4</strong><Tag intent="warning" minimal>merged</Tag></div><p>Bere tuis pponas ye gode leibe et reilles of metes; byut te to use ye tyme…</p></div><div className="comparison-card is-proposed"><div><strong>Proposed split</strong><Tag intent="success" minimal>manual</Tag></div><label>Rubric</label><p contentEditable suppressContentEditableWarning>Here begins guidance on wholesome diet and the ordering of meals.</p><label>Body text · Hand A</label><p contentEditable suppressContentEditableWarning>…and on choosing the proper seasons for bloodletting throughout the year.</p></div><button className="entity-jump" onClick={props.onOpenEntity}><Icon icon="diagram-tree" /> “feuel” has a dependent plant assertion <Icon icon="arrow-right" /></button></div></div>
          </main>
          <aside className="queue-decision">
            <span className="section-kicker">Decision</span><h2>Confirm split</h2><p>Edit geometry. Record processing guidance.</p>
            <RegionInspector region={props.selectedRegion} types={props.types} onChange={props.onUpdateRegion} onDelete={props.onDeleteRegion} onOpenEntity={props.onOpenEntity} />
            <ReprocessPanel />
            <div className="queue-actions"><Button large fill icon="undo" onClick={() => setDecision('skipped')}>Escalate</Button><Button large fill intent="success" icon="tick" onClick={() => setDecision('accepted')}>Accept and next</Button></div>
            {decision !== 'pending' && <Tag large fill intent={decision === 'accepted' ? 'success' : 'warning'} icon={decision === 'accepted' ? 'tick-circle' : 'time'}>{decision === 'accepted' ? 'Decision saved · moving to task 2' : 'Held for paleography review'}</Tag>}
          </aside>
        </div>
      )}
    </div>
  )
}

function LayerMatrix(props: SharedEditionProps & { focus: MatrixFocusId; setFocus: (focus: MatrixFocusId) => void }) {
  const [showDiffs, setShowDiffs] = useState(true)
  return (
    <div className="mockup-window matrix-window">
      <header className="matrix-header"><div className="matrix-wordmark"><span>WHL</span><strong>Evidence Desk</strong></div><WorkspaceNav value={props.workspace} onChange={props.setWorkspace} mode="compact" /><div><InputSearch /><Button intent="primary" icon="git-merge">Prepare release</Button></div></header>
      {props.commonWorkspace ?? (
        <EditionChrome label={activeManuscript.witnessLabel}>
          <div className="matrix-toolbar"><ButtonGroup>{matrixFocusDefinitions.map((item) => <Button key={item.id} active={props.focus === item.id} onClick={() => props.setFocus(item.id)} icon={item.icon}>{item.label}</Button>)}</ButtonGroup><span /><Checkbox checked={showDiffs} onChange={(event) => setShowDiffs(event.currentTarget.checked)} label="Differences only" /><Button icon="filter-list">Filters</Button><Button icon="export">Export</Button></div>
          <div className="matrix-body">
            <aside className="matrix-page"><div className="matrix-page__head"><span className="section-kicker">Image witness</span><strong>{activeManuscript.folio}</strong><Tag minimal>{activeManuscript.width} × {activeManuscript.height}</Tag></div><ManuscriptCanvas {...props.canvasProps} compact /><div className="matrix-page__legend"><span><i className="mistral" />Mistral 4</span><span><i className="manual" />Manual</span><span><i className="entity" />Entity anchor</span></div></aside>
            <main className="matrix-grid">
              <div className="matrix-grid__header"><span>Region</span><span>Source / revision</span><span>Reading or claim</span><span>Status</span><span>Provenance</span></div>
              <MatrixGroup title="m4-03 · February regimen" subtitle="x 150 · y 617 · 686 × 224" onSelect={() => props.canvasProps.onSelect('m4-03')}>
                <MatrixRow source="Mistral OCR 4" badge="machine" intent="warning" text={<>H ye monithe of <mark>feuel</mark> potage of the lebes etc y non…</>} meta="ocr-4-blocks · 13 Aug" />
                <MatrixRow source="Local Tesseract" badge="machine" intent="danger" text={<>Ue MICS TP UU VIC tere Ye Slo¥e ony Cage…</>} meta="eng psm-6 · 13 Aug" />
                <MatrixRow source="Editorial rev. 3" badge="reviewed" intent="success" text={<>In the month of <button onClick={props.onOpenEntity}>feuel ↗</button>, use pottage of the leaves…</>} meta="A. Miller · 14 Aug" />
                <MatrixRow source="Modern English" badge="draft" intent="primary" text={<>In February, take leaf pottage and avoid excess…</>} meta="depends on rev. 3" />
              </MatrixGroup>
              <MatrixGroup title="Entity assertion · feuel" subtitle="mention whl:m:009184 · triple anchored" onSelect={props.onOpenEntity}>
                <MatrixRow source="Name form" badge="proposed" intent="warning" text={<><strong>feuel</strong> · Middle English · c.1450</>} meta="dictionary match" />
                <MatrixRow source="Historical concept" badge="disputed" intent="warning" text={<>Fennel preparation <span className="matrix-arrow">→</span> <em>Foeniculum vulgare</em>?</>} meta="2 competing assertions" />
              </MatrixGroup>
              <MatrixGroup title="Knowledge note · calendar medicine" subtitle="anchored to regions m4-01–m4-06" onSelect={() => props.canvasProps.onSelect('m4-01')}>
                <MatrixRow source="Editorial note" badge="draft" intent="primary" text={<>Month-by-month regimens combine diet, humoral balance, and bloodletting.</>} meta="S. Editor · rev. 1" />
              </MatrixGroup>
            </main>
            <aside className="matrix-inspector"><div className="matrix-inspector__head"><span className="section-kicker">Properties</span><Tag minimal intent="success">Anchor valid</Tag></div><RegionInspector region={props.selectedRegion} types={props.types} onChange={props.onUpdateRegion} onDelete={props.onDeleteRegion} onOpenEntity={props.onOpenEntity} /><Divider /><div className="lineage"><h3>Lineage</h3><div><i />Image witness <small>immutable</small></div><div><i />Mistral block <small>machine · proposed</small></div><div><i />Editorial revision 3 <small>reviewed</small></div><div><i />Translation draft <small>stale if source moves</small></div></div></aside>
          </div>
        </EditionChrome>
      )}
    </div>
  )
}

function InputSearch() {
  return <Button minimal icon="search" text="Search this edition" />
}

function MatrixGroup({ title, subtitle, children, onSelect }: { title: string; subtitle: string; children: React.ReactNode; onSelect: () => void }) {
  return <section className="matrix-group"><button className="matrix-group__title" onClick={onSelect}><Icon icon="chevron-down" size={12} /><strong>{title}</strong><small>{subtitle}</small></button>{children}</section>
}

function MatrixRow({ source, badge, intent, text, meta }: { source: string; badge: string; intent: 'success' | 'warning' | 'danger' | 'primary'; text: React.ReactNode; meta: string }) {
  return <div className="matrix-row"><span /><strong>{source}</strong><span>{text}</span><Tag minimal intent={intent}>{badge}</Tag><small>{meta}</small></div>
}

export default App
