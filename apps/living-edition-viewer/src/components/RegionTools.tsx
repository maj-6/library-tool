import { useState } from 'react'
import { Button, Callout, Divider, HTMLSelect, Icon, InputGroup, Tag } from '@blueprintjs/core'
import type { Region, RegionType } from '../types'

interface RegionTreeProps {
  types: RegionType[]
  activeTypeId: string
  regionCounts: Record<string, number>
  onSelect: (id: string) => void
  onAddSubclass: (parentId: string, name: string) => void
}

export function RegionTree({ types, activeTypeId, regionCounts, onSelect, onAddSubclass }: RegionTreeProps) {
  const [addingTo, setAddingTo] = useState<string | null>(null)
  const [newName, setNewName] = useState('')
  const roots = types.filter((type) => !type.parentId)

  const row = (type: RegionType, depth = 0) => (
    <div key={type.id}>
      <button
        className={activeTypeId === type.id ? 'type-row is-active' : 'type-row'}
        style={{ paddingLeft: `${10 + depth * 20}px` }}
        onClick={() => onSelect(type.id)}
      >
        {depth > 0 && <Icon icon="chevron-right" size={10} />}
        <span className="type-swatch" style={{ background: type.color }} />
        <span>{type.name}</span>
        <Tag minimal round>{regionCounts[type.id] ?? 0}</Tag>
        <Button
          minimal
          small
          icon="plus"
          aria-label={`Add subclass to ${type.name}`}
          onClick={(event) => { event.stopPropagation(); setAddingTo(type.id); setNewName('') }}
        />
      </button>
      {types.filter((candidate) => candidate.parentId === type.id).map((child) => row(child, depth + 1))}
      {addingTo === type.id && (
        <form
          className="type-add-row"
          style={{ marginLeft: `${16 + depth * 20}px` }}
          onSubmit={(event) => {
            event.preventDefault()
            if (!newName.trim()) return
            onAddSubclass(type.id, newName.trim())
            setAddingTo(null)
          }}
        >
          <InputGroup small autoFocus placeholder="Subclass name" value={newName} onChange={(event) => setNewName(event.target.value)} />
          <Button small intent="primary" type="submit" icon="tick" aria-label="Add subclass" />
        </form>
      )}
    </div>
  )

  return (
    <section className="panel-section region-tree">
      <div className="section-heading">
        <div><span className="section-kicker">Types</span><h3>Region types</h3></div>
        <Button minimal small icon="settings" aria-label="Region type settings" />
      </div>
      <p className="section-help">Select type. Add subclass.</p>
      <div className="type-tree">{roots.map((type) => row(type))}</div>
    </section>
  )
}

interface InspectorProps {
  region: Region | null
  types: RegionType[]
  onChange: (region: Region) => void
  onDelete: (id: string) => void
  onOpenEntity: () => void
}

export function RegionInspector({ region, types, onChange, onDelete, onOpenEntity }: InspectorProps) {
  if (!region) {
    return (
      <Callout className="empty-inspector" icon="select" title="Select a region">
        Select a box or polygon.
      </Callout>
    )
  }

  const updateNumber = (key: 'x' | 'y' | 'width' | 'height', raw: string) => {
    const value = Number.parseFloat(raw)
    if (Number.isFinite(value)) onChange({ ...region, [key]: Math.max(0, Math.min(100, value)) })
  }

  return (
    <section className="panel-section region-inspector">
      <div className="section-heading">
        <div><span className="section-kicker">{region.source}</span><h3>{region.label}</h3></div>
        <Tag intent={region.source === 'manual' ? 'success' : 'primary'} minimal>{region.source === 'manual' ? 'manual' : `${Math.round((region.confidence ?? 0) * 100)}%`}</Tag>
      </div>
      <label className="field-label">Label</label>
      <InputGroup value={region.label} onChange={(event) => onChange({ ...region, label: event.target.value })} />
      <label className="field-label">Region type</label>
      <HTMLSelect
        fill
        value={region.typeId}
        onChange={(event) => {
          const type = types.find((candidate) => candidate.id === event.target.value)
          onChange({ ...region, typeId: event.target.value, color: type?.color ?? region.color })
        }}
        options={types.map((type) => ({ value: type.id, label: type.parentId ? `↳ ${type.name}` : type.name }))}
      />
      <div className="geometry-grid">
        {(['x', 'y', 'width', 'height'] as const).map((key) => (
          <label key={key}>
            <span>{key === 'width' ? 'W' : key === 'height' ? 'H' : key.toUpperCase()} %</span>
            <input type="number" step="0.1" value={region[key].toFixed(1)} onChange={(event) => updateNumber(key, event.target.value)} />
          </label>
        ))}
      </div>
      <Divider />
      <div className="anchor-summary">
        <Icon icon="locate" size={14} />
        <div><strong>Triple anchor</strong><span>region · text range · quote</span></div>
        <Tag intent="success" minimal>healthy</Tag>
      </div>
      {region.typeId === 'plant-name' && (
        <Button fill icon="diagram-tree" intent="success" onClick={onOpenEntity}>Open entity</Button>
      )}
      <Button minimal fill icon="trash" intent="danger" onClick={() => onDelete(region.id)}>Delete</Button>
    </section>
  )
}
