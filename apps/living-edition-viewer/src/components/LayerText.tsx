import { useState } from 'react'
import { Button, ButtonGroup, Callout, Icon, Tag } from '@blueprintjs/core'
import { entityAssertions, manuscriptText } from '../data/mockData'
import { textSourceDefinitions } from '../data/registries'
import type { TextSourceId } from '../types'

interface Props {
  selectedRegionId: string | null
  onOpenEntity: () => void
  dense?: boolean
}

export function LayerText({ selectedRegionId, onOpenEntity, dense = false }: Props) {
  const [source, setSource] = useState<TextSourceId>('mistral')
  const [translationMode, setTranslationMode] = useState<'literal' | 'reading'>('literal')
  const lines = manuscriptText[source]
  const sourceDefinition = textSourceDefinitions.find((item) => item.id === source) ?? textSourceDefinitions[0]

  return (
    <div className={`layer-text ${dense ? 'is-dense' : ''}`}>
      <section className="text-column transcription-column">
        <div className="text-column__head">
          <div><span className="section-kicker">Transcription</span><h3>{sourceDefinition.title}</h3></div>
          <Tag minimal intent={sourceDefinition.status === 'reviewed' ? 'success' : 'warning'}>{sourceDefinition.status}</Tag>
        </div>
        <ButtonGroup minimal>
          {textSourceDefinitions.map((item) => (
            <Button key={item.id} small active={source === item.id} onClick={() => setSource(item.id)}>{item.label}</Button>
          ))}
        </ButtonGroup>
        <div className="edition-lines">
          {lines.map((line, index) => (
            <div className={`${selectedRegionId && index === 1 ? 'edition-line is-aligned' : 'edition-line'} ${source === 'local' ? 'is-low-confidence' : ''}`} key={line}>
              <span className="line-number">{index + 1}</span>
              <span contentEditable suppressContentEditableWarning>{line}</span>
              {index === 2 && source !== 'local' && (
                <button className="entity-mention" onClick={onOpenEntity}>feuel <Icon icon="diagram-tree" size={10} /></button>
              )}
            </div>
          ))}
        </div>
      </section>
      <section className="text-column translation-column">
        <div className="text-column__head">
          <div><span className="section-kicker">Modern English</span><h3>{translationMode === 'literal' ? 'Literal' : 'Reading'}</h3></div>
          <Tag minimal intent="primary">draft</Tag>
        </div>
        <ButtonGroup minimal>
          <Button small active={translationMode === 'literal'} onClick={() => setTranslationMode('literal')}>Literal</Button>
          <Button small active={translationMode === 'reading'} onClick={() => setTranslationMode('reading')}>Readable</Button>
        </ButtonGroup>
        <div className="edition-lines is-translation">
          {manuscriptText.translation.map((line, index) => (
            <div className={`${selectedRegionId && index === 1 ? 'edition-line is-aligned' : 'edition-line'}`} key={line}>
              <span className="line-number">{index + 1}</span>
              <span contentEditable suppressContentEditableWarning>{translationMode === 'reading' ? line.replace('wholesome diet', 'healthy eating') : line}</span>
            </div>
          ))}
        </div>
        {!dense && (
          <Callout className="knowledge-note" icon="learning" title="Calendar medicine">
            Month-by-month regimens combine diet, humoral balance, and bloodletting. This note is editorial and remains separate from the translation.
          </Callout>
        )}
      </section>
      {!dense && (
        <section className="entity-strip">
          <div className="entity-strip__label">Entity proposals</div>
          {entityAssertions.map((entity) => (
            <button key={entity.form} onClick={onOpenEntity}>
              <span>{entity.form}</span><small>{entity.concept}</small><Tag minimal intent={entity.state === 'reviewed' ? 'success' : 'warning'}>{entity.confidence}</Tag>
            </button>
          ))}
        </section>
      )}
    </div>
  )
}
