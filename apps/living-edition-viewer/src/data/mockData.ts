import type { DesignDirection, Region } from '../types'
import { activeManuscript, regionTypeById } from './registries'

export const designs: DesignDirection[] = [
  {
    id: 'scriptorium',
    marker: 'A',
    title: 'Scriptorium',
    subtitle: 'The aligned scholarly edition',
    description: 'A calm reading surface keeps image, transcription, and translation visible together; editing tools sit at the edge.',
    bestFor: 'Close reading, teaching, and passage-level comparison',
    tradeoff: 'Geometry work has less canvas space',
  },
  {
    id: 'spatial',
    marker: 'B',
    title: 'Spatial Lab',
    subtitle: 'The canvas is the instrument',
    description: 'A canvas-first workstation uses crisp light panels and dense spatial tools for boxes, polygons, hands, marginalia, and other evidence.',
    bestFor: 'Segmentation, paleography, and difficult layouts',
    tradeoff: 'Long-form reading moves to a drawer',
  },
  {
    id: 'queue',
    marker: 'C',
    title: 'Review Queue',
    subtitle: 'The next uncertainty, already framed',
    description: 'A task-led workflow turns low-confidence passages and stale anchors into discrete editorial decisions.',
    bestFor: 'Sustained review by staff and contributors',
    tradeoff: 'Less freedom to roam the whole edition',
  },
  {
    id: 'matrix',
    marker: 'D',
    title: 'Layer Matrix',
    subtitle: 'Every witness to every claim',
    description: 'A dense comparison table treats engines and editorial revisions as parallel evidence, with lineage always visible.',
    bestFor: 'Engine evaluation, provenance, and release QA',
    tradeoff: 'Highest information density',
  },
]

const mistralBoxes = [
  [160, 190, 824, 386],
  [151, 386, 824, 617],
  [150, 617, 836, 841],
  [150, 841, 836, 1059],
  [150, 1059, 836, 1289],
  [150, 1289, 824, 1690],
]

export const initialRegions: Region[] = mistralBoxes.map(([left, top, right, bottom], index) => ({
  id: `m4-${String(index + 1).padStart(2, '0')}`,
  label: index === 0 ? 'Calendar rubric' : `Body passage ${index}`,
  typeId: index === 0 ? 'rubric' : index > 3 ? 'hand-b' : 'hand-a',
  color: regionTypeById[index === 0 ? 'rubric' : index > 3 ? 'hand-b' : 'hand-a'].color,
  confidence: [0.81, 0.74, 0.68, 0.72, 0.59, 0.51][index],
  x: (left / activeManuscript.width) * 100,
  y: (top / activeManuscript.height) * 100,
  width: ((right - left) / activeManuscript.width) * 100,
  height: ((bottom - top) / activeManuscript.height) * 100,
  source: 'mistral-ocr-4',
}))

export const manuscriptText = {
  mistral: [
    'Bere tuis pponas ye gode leibe et reilles of metes; byut te to use ye tyme of blodlatynge…',
    'P the monithe of jancenet Whit Wynis god to dy to fustynge et blodlatynge…',
    'H ye monithe of feuel potage of the lebes etc y non. fer ye ben veni…',
    'H ye monithe of marthe figes y rasyumes et of were metes et drinkes vse…',
  ],
  local: [
    'oe er ee eh lh ore! eo / iD AS s i is i hy A we ECE…',
    'ge TAU MOMMIES Or ple VHIr— Usp Gg COs…',
    'my VC RE UittHe OF Heil LOTNED OF te…',
    'dios 7 WS SUT IRS OF THAI C 4 CAC…',
  ],
  edited: [
    'Here begins a calendar of good diet and rules of meals,',
    'with the times for bloodletting through the months of the year.',
    'In the month of January, white wine is good to drink…',
    'In the month of February, use pottage of leaves…',
  ],
  translation: [
    'Here begins guidance on wholesome diet and the ordering of meals,',
    'and on choosing the proper seasons for bloodletting throughout the year.',
    'In January, white wine is good to drink, with fasting…',
    'In February, take leaf pottage and avoid excess…',
  ],
}

export const queueItems = [
  { id: 'q1', kind: 'Segmentation', title: 'Rubric merged with opening line', meta: 'fol. 4r · Mistral OCR 4', severity: 'high' },
  { id: 'q2', kind: 'Anchor stale', title: '“feuel” no longer matches revision 3', meta: 'fol. 4r · entity mention', severity: 'high' },
  { id: 'q3', kind: 'Hand change', title: 'Possible second hand after March', meta: 'fol. 4r · 3 regions', severity: 'medium' },
  { id: 'q4', kind: 'Marginalia', title: 'Faint note outside text frame', meta: 'fol. 5v · local OCR', severity: 'low' },
]

export const entityAssertions = [
  { form: 'feuel', concept: 'fennel preparation?', confidence: 'possible', state: 'proposed' },
  { form: 'figes', concept: 'fig fruit', confidence: 'likely', state: 'reviewed' },
  { form: 'rasyumes', concept: 'raisin / dried grape', confidence: 'possible', state: 'proposed' },
]
