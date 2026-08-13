import { Button, ButtonGroup, Icon } from '@blueprintjs/core'
import { workspaceDefinitions } from '../data/registries'
import type { Workspace } from '../types'

interface Props {
  value: Workspace
  onChange: (workspace: Workspace) => void
  mode?: 'tabs' | 'rail' | 'compact'
}

export function WorkspaceNav({ value, onChange, mode = 'tabs' }: Props) {
  if (mode === 'rail') {
    return (
      <nav className="workspace-rail" aria-label="Workspaces">
        <div className="workspace-rail__brand">WHL</div>
        {workspaceDefinitions.map((item) => (
          <button
            className={value === item.id ? 'workspace-rail__item is-active' : 'workspace-rail__item'}
            key={item.id}
            onClick={() => onChange(item.id)}
            aria-pressed={value === item.id}
            title={`${item.label} · ${item.detail}`}
          >
            <Icon icon={item.icon} size={18} />
            <span>{item.label}</span>
          </button>
        ))}
        <span className="workspace-rail__spacer" />
        <button className="workspace-rail__item" title="Settings"><Icon icon="cog" size={18} /><span>Settings</span></button>
      </nav>
    )
  }

  return (
    <div className={`workspace-tabs workspace-tabs--${mode}`}>
      <ButtonGroup minimal={mode === 'compact'} fill={mode !== 'compact'}>
        {workspaceDefinitions.map((item) => (
          <Button
            key={item.id}
            icon={item.icon}
            active={value === item.id}
            intent={value === item.id ? 'primary' : 'none'}
            text={item.label}
            onClick={() => onChange(item.id)}
            aria-label={`${item.label}, ${item.detail}`}
          />
        ))}
      </ButtonGroup>
      {mode === 'tabs' && <span className="workspace-tabs__context">{workspaceDefinitions.find((item) => item.id === value)?.detail}</span>}
    </div>
  )
}
