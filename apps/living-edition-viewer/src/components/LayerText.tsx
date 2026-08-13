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

type EntityAssertion = (typeof entityAssertions)[number]

interface LineSegment {
  text: string
  entity?: EntityAssertion
}

const entityByForm = new Map(
  entityAssertions.map((entity) => [entity.form.toLocaleLowerCase(), entity]),
)

const entityFormPattern = entityAssertions
  .map((entity) => entity.form)
  .sort((left, right) => right.length - left.length)
  .map((form) => form.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
  .join('|')

const isWordCharacter = (value: string | undefined) => Boolean(value && /[\p{L}\p{N}]/u.test(value))

function segmentEntityMentions(line: string): LineSegment[] {
  if (!entityFormPattern) return [{ text: line }]

  const segments: LineSegment[] = []
  const pattern = new RegExp(entityFormPattern, 'giu')
  let cursor = 0

  for (const match of line.matchAll(pattern)) {
    const start = match.index
    const text = match[0]
    const end = start + text.length
    if (isWordCharacter(line[start - 1]) || isWordCharacter(line[end])) continue

    if (start > cursor) segments.push({ text: line.slice(cursor, start) })
    segments.push({ text, entity: entityByForm.get(text.toLocaleLowerCase()) })
    cursor = end
  }

  if (cursor < line.length) segments.push({ text: line.slice(cursor) })
  return segments.length > 0 ? segments : [{ text: line }]
}

function InlineEntityLine({ line, onOpenEntity }: { line: string; onOpenEntity: () => void }) {
  return (
    <span className="edition-line__text" contentEditable suppressContentEditableWarning>
      {segmentEntityMentions(line).map((segment, index) => segment.entity ? (
        <button
          type="button"
          className="entity-mention"
          contentEditable={false}
          data-state={segment.entity.state}
          title={`${segment.entity.concept} (${segment.entity.confidence})`}
          aria-label={`Open plant entity for ${segment.text}: ${segment.entity.concept}`}
          onClick={(event) => {
            event.stopPropagation()
            onOpenEntity()
          }}
          key={`${segment.text}-${index}`}
        >
          {segment.text}<Icon icon="diagram-tree" size={10} />
        </button>
      ) : <span key={`text-${index}`}>{segment.text}</span>)}
    </span>
  )
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
              <InlineEntityLine line={line} onOpenEntity={onOpenEntity} />
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
    </div>
  )
}
