import { Button, ButtonGroup, Icon, Tag, TextArea } from '@blueprintjs/core'
import type { NoteScope } from '../types'

interface Props {
  scope: NoteScope
  value: string
  selectedRegionLabel?: string
  onChangeScope: (scope: NoteScope) => void
  onChange: (value: string) => void
}

const scopes: Array<{ id: NoteScope; label: string; icon: 'book' | 'document' | 'selection' }> = [
  { id: 'book', label: 'Book', icon: 'book' },
  { id: 'page', label: 'Page', icon: 'document' },
  { id: 'region', label: 'Region', icon: 'selection' },
]

export function NotesEditor({ scope, value, selectedRegionLabel, onChangeScope, onChange }: Props) {
  return (
    <section className="panel-section notes-editor">
      <div className="section-heading">
        <div><span className="section-kicker">Annotations</span><h3>Notes</h3></div>
        <Tag minimal icon="saved">Saved locally</Tag>
      </div>
      <ButtonGroup fill>
        {scopes.map((item) => (
          <Button key={item.id} small icon={item.icon} active={scope === item.id} onClick={() => onChangeScope(item.id)}>{item.label}</Button>
        ))}
      </ButtonGroup>
      <div className="note-context">
        <Icon icon="link" size={12} />
        {scope === 'book' ? 'The Herbal · all pages' : scope === 'page' ? 'fol. 4r · current page' : selectedRegionLabel ?? 'No region selected'}
      </div>
      <TextArea
        fill
        rows={4}
        placeholder={scope === 'region' && !selectedRegionLabel ? 'Select region' : 'Enter note'}
        value={value}
        disabled={scope === 'region' && !selectedRegionLabel}
        onChange={(event) => onChange(event.target.value)}
      />
      <div className="note-footer"><span><kbd>@</kbd> Mention</span><span><kbd>/</kbd> Evidence link</span></div>
    </section>
  )
}
