import { useState } from 'react'
import {
  Button,
  ButtonGroup,
  Checkbox,
  Divider,
  Icon,
  Menu,
  MenuItem,
  Popover,
  Tab,
  Tabs,
  Tag,
} from '@blueprintjs/core'
import { activeManuscript, layerDefinitions } from '../data/registries'
import type {
  LayerId,
  NoteScope,
  Region,
  RegionType,
  WorkbenchLayoutDefinition,
  Workspace,
} from '../types'
import { LayerText } from './LayerText'
import { ManuscriptCanvas } from './ManuscriptCanvas'
import { NotesEditor } from './NotesEditor'
import { RegionInspector, RegionTree } from './RegionTools'
import { ReprocessPanel } from './ReprocessPanel'
import { WorkspaceNav } from './WorkspaceNav'

interface Props {
  layout: WorkbenchLayoutDefinition
  workspace: Workspace
  setWorkspace: (workspace: Workspace) => void
  commonWorkspace: React.ReactNode
  canvasProps: React.ComponentProps<typeof ManuscriptCanvas>
  selectedRegion: Region | null
  types: RegionType[]
  activeTypeId: string
  setActiveTypeId: (id: string) => void
  regionCounts: Record<string, number>
  addSubclass: (parentId: string, name: string) => void
  onUpdateRegion: (region: Region) => void
  onDeleteRegion: (id: string) => void
  onOpenEntity: () => void
  noteScope: NoteScope
  setNoteScope: (scope: NoteScope) => void
  notes: Record<NoteScope, string>
  setNotes: React.Dispatch<React.SetStateAction<Record<NoteScope, string>>>
  visibleLayerIds: LayerId[]
  setLayerVisibility: (id: LayerId, visible: boolean) => void
}

const menuDefinitions = [
  { label: 'File', items: ['Open package', 'Save revision', 'Export layer'] },
  { label: 'Edit', items: ['Undo', 'Redo', 'Find'] },
  { label: 'View', items: ['Fit page', 'Actual size', 'Reset panes'] },
  { label: 'Tools', items: ['Validate anchors', 'Reprocess', 'Region types'] },
] as const

export function DesktopWorkbench(props: Props) {
  const [inspectorTab, setInspectorTab] = useState<string>('properties')
  const [problem, setProblem] = useState('m4-03')
  const hasFeature = (id: string) => props.layout.features.includes(id)

  return (
    <div className={`mockup-window desktop-workbench desktop-workbench--${props.layout.layoutClass}`}>
      <header className="dw-titlebar">
        <div><span className="dw-appmark">WHL</span><strong>{props.layout.windowTitle}</strong></div>
        <span>{props.layout.documentCode}</span>
        <div><Button minimal small icon="minus" aria-label="Minimize" /><Button minimal small icon="application" aria-label="Restore" /><Button minimal small icon="cross" aria-label="Close" /></div>
      </header>
      <div className="dw-menubar">
        {menuDefinitions.map((menu) => (
          <Popover
            key={menu.label}
            placement="bottom-start"
            content={<Menu>{menu.items.map((item) => <MenuItem key={item} text={item} />)}</Menu>}
          >
            <Button minimal small>{menu.label}</Button>
          </Popover>
        ))}
        <span />
        <Button minimal small icon="help">Help</Button>
      </div>
      <div className="dw-commandbar">
        <ButtonGroup minimal>
          <Button small icon="folder-open" aria-label="Open" />
          <Button small icon="floppy-disk" aria-label="Save" />
          <Button small icon="undo" aria-label="Undo" />
          <Button small icon="redo" aria-label="Redo" />
        </ButtonGroup>
        <Divider />
        <ButtonGroup minimal>
          <Button small icon="chevron-left" aria-label="Previous page" />
          <Button small text="4r / 55" />
          <Button small icon="chevron-right" aria-label="Next page" />
        </ButtonGroup>
        <Divider />
        <Popover
          placement="bottom-start"
          content={<Menu>{layerDefinitions.map((layer) => (
            <MenuItem
              key={layer.id}
              icon={props.visibleLayerIds.includes(layer.id) ? 'tick' : 'blank'}
              text={layer.label}
              onClick={() => props.setLayerVisibility(layer.id, !props.visibleLayerIds.includes(layer.id))}
            />
          ))}</Menu>}
        >
          <Button small icon="layers">Layers</Button>
        </Popover>
        <span />
        <WorkspaceNav value={props.workspace} onChange={props.setWorkspace} mode="compact" />
      </div>

      {props.commonWorkspace ?? (
        <>
          <div className="dw-document-tabs">
            <button className="is-active"><Icon icon="document" size={12} /> {activeManuscript.folio}<Icon icon="small-cross" size={11} /></button>
            <button><Icon icon="properties" size={12} /> Edition metadata</button>
            <span />
            <Tag minimal intent="warning">Draft 3</Tag>
          </div>
          <div className="dw-workarea">
            {hasFeature('navigator') && (
              <aside className="dw-pane dw-navigator">
                <PaneHeader title={hasFeature('catalog-index') ? 'Object index' : 'Navigator'} icon="folder-close" />
                {hasFeature('catalog-index') && (
                  <div className="dw-object-tree">
                    <button><Icon icon="book" /> Takamiya MS 46 1</button>
                    <button><Icon icon="document" /> Canvas 0007</button>
                    <button className="is-active"><Icon icon="selection" /> Regions</button>
                    <button onClick={props.onOpenEntity}><Icon icon="diagram-tree" /> Plant entities</button>
                  </div>
                )}
                <RegionTree
                  types={props.types}
                  activeTypeId={props.activeTypeId}
                  regionCounts={props.regionCounts}
                  onSelect={props.setActiveTypeId}
                  onAddSubclass={props.addSubclass}
                />
                <Divider />
                <div className="dw-region-list">
                  <div className="dw-list-head"><span>Page regions</span><Tag minimal>{props.canvasProps.regions.length}</Tag></div>
                  {props.canvasProps.regions.map((region) => (
                    <button
                      key={region.id}
                      className={props.selectedRegion?.id === region.id ? 'is-active' : ''}
                      onClick={() => props.canvasProps.onSelect(region.id)}
                    >
                      <i style={{ background: region.color }} /><span>{region.label}</span><small>{region.id}</small>
                    </button>
                  ))}
                </div>
              </aside>
            )}

            <main className="dw-pane dw-canvas-pane">
              <PaneHeader title="Page" icon="media" actions={<><Button minimal small icon="zoom-out" /><Tag minimal>43%</Tag><Button minimal small icon="zoom-in" /><Button minimal small icon="maximize" /></>} />
              <ManuscriptCanvas {...props.canvasProps} compact={props.layout.layoutClass !== 'drafting'} />
            </main>

            {hasFeature('text') && (
              <section className="dw-pane dw-text-pane">
                <PaneHeader title="Aligned text" icon="comparison" actions={<Tag minimal>Region {props.selectedRegion?.id ?? 'none'}</Tag>} />
                <LayerText dense selectedRegionId={props.selectedRegion?.id ?? null} onOpenEntity={props.onOpenEntity} />
              </section>
            )}

            {hasFeature('properties') && (
              <aside className="dw-pane dw-inspector-pane">
                <Tabs selectedTabId={inspectorTab} onChange={(id) => setInspectorTab(String(id))} animate={false} renderActiveTabPanelOnly>
                  <Tab id="properties" title="Properties" panel={<RegionInspector region={props.selectedRegion} types={props.types} onChange={props.onUpdateRegion} onDelete={props.onDeleteRegion} onOpenEntity={props.onOpenEntity} />} />
                  <Tab id="notes" title="Notes" panel={<NotesEditor scope={props.noteScope} value={props.notes[props.noteScope]} selectedRegionLabel={props.selectedRegion?.label} onChangeScope={props.setNoteScope} onChange={(value) => props.setNotes((current) => ({ ...current, [props.noteScope]: value }))} />} />
                  <Tab id="process" title="Process" panel={<ReprocessPanel />} />
                </Tabs>
              </aside>
            )}

            {hasFeature('problems') && (
              <section className="dw-pane dw-problems-pane">
                <PaneHeader title="Problems" icon="issue" actions={<><Checkbox inline defaultChecked label="Open only" /><Button minimal small icon="filter-list" /></>} />
                <div className="dw-problem-grid" role="grid">
                  <div className="dw-problem-row is-head"><span>ID</span><span>Class</span><span>Description</span><span>Source</span><span>Status</span></div>
                  {[
                    ['m4-03', 'Layout', 'Rubric joined to body text', 'Mistral 4', 'Open'],
                    ['m4-04', 'Anchor', 'Entity quote is stale', 'Revision 3', 'Open'],
                    ['m4-05', 'Hand', 'Hand change requires review', 'Manual', 'Held'],
                  ].map((row) => <button key={row[0]} className={`dw-problem-row ${problem === row[0] ? 'is-active' : ''}`} onClick={() => { setProblem(row[0]); props.canvasProps.onSelect(row[0]) }}>{row.map((cell) => <span key={cell}>{cell}</span>)}</button>)}
                </div>
              </section>
            )}
          </div>
          <footer className="dw-statusbar">
            <span><Icon icon="tick-circle" size={11} /> {props.layout.statusText}</span>
            <span>Selection: {props.selectedRegion?.id ?? 'None'}</span>
            <span>Tool: {props.canvasProps.drawMode}</span>
            <span>X 512&nbsp;&nbsp;Y 884</span>
            <span>Zoom 43%</span>
          </footer>
        </>
      )}
    </div>
  )
}

function PaneHeader({ title, icon, actions }: { title: string; icon: 'folder-close' | 'media' | 'comparison' | 'issue'; actions?: React.ReactNode }) {
  return <header className="dw-pane-header"><span><Icon icon={icon} size={12} />{title}</span><div>{actions}</div></header>
}
