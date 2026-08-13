import { Button, Card, Icon, Tag } from '@blueprintjs/core'
import type { DesignDirection, DesignId } from '../types'

interface Props {
  designs: DesignDirection[]
  selected: DesignId
  shortlist: DesignId[]
  onSelect: (id: DesignId) => void
  onShortlist: (id: DesignId) => void
}

export function DesignPicker({ designs, selected, shortlist, onSelect, onShortlist }: Props) {
  return (
    <section className="design-picker" aria-labelledby="design-picker-title">
      <div className="design-picker__heading">
        <div>
          <div className="eyebrow" id="design-picker-title">Choose a working direction</div>
          <p>Explore each concept in Library, Edition, and Entity workspaces. Nothing here commits the final build.</p>
        </div>
        <Tag minimal icon="lightbulb">4 distinct workflows</Tag>
      </div>
      <div className="design-picker__grid">
        {designs.map((design) => {
          const isSelected = selected === design.id
          const isShortlisted = shortlist.includes(design.id)
          return (
            <Card
              className={`design-card design-card--${design.id} ${isSelected ? 'is-selected' : ''}`}
              key={design.id}
              interactive
              onClick={() => onSelect(design.id)}
              aria-current={isSelected ? 'true' : undefined}
            >
              <div className="design-card__topline">
                <span className="design-card__marker">{design.marker}</span>
                <Button
                  minimal
                  small
                  icon={isShortlisted ? 'star' : 'star-empty'}
                  intent={isShortlisted ? 'warning' : 'none'}
                  aria-label={isShortlisted ? `Remove ${design.title} from shortlist` : `Shortlist ${design.title}`}
                  onClick={(event) => { event.stopPropagation(); onShortlist(design.id) }}
                />
              </div>
              <h2>{design.title}</h2>
              <strong>{design.subtitle}</strong>
              <p>{design.description}</p>
              <div className="design-card__fit"><Icon icon="tick-circle" size={12} /> {design.bestFor}</div>
              {isSelected && <Tag intent="primary" round>Viewing now</Tag>}
            </Card>
          )
        })}
      </div>
    </section>
  )
}
