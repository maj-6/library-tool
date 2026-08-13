import type {
  ReaderAudienceDefinition,
  ReaderMaterialDefinition,
  ReaderPresentationDefinition,
  ReaderPublication,
} from '../types'
import {
  readerAudienceDefinitions,
  readerMaterialDefinitions,
  readerPresentationDefinitions,
} from './registries'

export interface ReaderCompositionResolution {
  state: 'compatible' | 'fallback' | 'blocked'
  audience: ReaderAudienceDefinition | null
  material: ReaderMaterialDefinition | null
  requested: ReaderPresentationDefinition | null
  effective: ReaderPresentationDefinition | null
  compatible: readonly ReaderPresentationDefinition[]
  missingCapabilities: readonly string[]
  explanation: string
}

function score(
  presentation: ReaderPresentationDefinition,
  audience: ReaderAudienceDefinition,
  material: ReaderMaterialDefinition,
) {
  const preferenceIndex = audience.preferredPresentationIds.indexOf(presentation.id)
  const preference = preferenceIndex < 0 ? 0 : 30 - preferenceIndex * 6
  const audienceAffinity = presentation.audienceAffinity.includes(audience.id) ? 12 : 0
  const materialAffinity = presentation.materialAffinity.includes(material.id) ? 8 : 0
  return preference + audienceAffinity + materialAffinity
}

function blocked(explanation: string): ReaderCompositionResolution {
  return {
    state: 'blocked',
    audience: null,
    material: null,
    requested: null,
    effective: null,
    compatible: [],
    missingCapabilities: [],
    explanation,
  }
}

export function resolveReaderComposition(
  audienceId: string,
  requestedPresentationId: string,
  publication: ReaderPublication,
): ReaderCompositionResolution {
  const audience = readerAudienceDefinitions.find((item) => item.id === audienceId)
  if (!audience) return blocked('Unknown audience profile: ' + audienceId + '.')
  const material = readerMaterialDefinitions.find(
    (item) => item.id === publication.materialProfileId,
  )
  if (!material) {
    return blocked('Unknown declared material profile: ' + publication.materialProfileId + '.')
  }
  const requested = readerPresentationDefinitions.find(
    (item) => item.id === requestedPresentationId,
  )
  if (!requested) return blocked('No renderer is registered for presentation: ' + requestedPresentationId + '.')

  // Effective capabilities are the intersection of the declared projection and
  // its registered material profile. A UI choice can never manufacture either.
  const declaredCapabilities = new Set<string>(publication.capabilities)
  const profileCapabilities = new Set<string>(material.capabilities)
  const capabilities = new Set(
    [...declaredCapabilities].filter((item) => profileCapabilities.has(item)),
  )
  const policyAllows = (presentation: ReaderPresentationDefinition) => (
    publication.projection.allowedPresentationIds.includes(presentation.id)
    && publication.projection.rights.allowedPresentationIds.includes(presentation.id)
  )
  const compatible = [...readerPresentationDefinitions]
    .filter((presentation) => (
      policyAllows(presentation)
      && presentation.requiresAll.every((item) => capabilities.has(item))
    ))
    .sort((
      left: ReaderPresentationDefinition,
      right: ReaderPresentationDefinition,
    ) => score(right, audience, material) - score(left, audience, material))
  const missingCapabilities = requested.requiresAll.filter(
    (item) => !capabilities.has(item),
  )
  const policyBlock = !policyAllows(requested)
  const requestedCompatible = missingCapabilities.length === 0 && !policyBlock
  const declaredFallback = readerPresentationDefinitions.find(
    (item) => (
      item.id === requested.fallbackId
      && compatible.some((candidate) => candidate.id === item.id)
    ),
  )
  const effective = requestedCompatible
    ? requested
    : declaredFallback ?? compatible[0] ?? null

  if (!effective) {
    return {
      state: 'blocked',
      audience,
      material,
      requested,
      effective: null,
      compatible,
      missingCapabilities,
      explanation: 'No registered presentation satisfies this profile and publication policy.',
    }
  }
  if (effective.id !== requested.id) {
    const policyReason = publication.projection.blocked.find(
      (item) => item.presentationId === requested.id,
    )?.reason
    const reason = policyBlock
      ? policyReason ?? 'Publication policy blocks this presentation.'
      : requested.label + ' needs ' + missingCapabilities.join(' + ') + '.'
    return {
      state: 'fallback',
      audience,
      material,
      requested,
      effective,
      compatible,
      missingCapabilities,
      explanation: reason + ' Showing ' + effective.label + ' as the nearest compatible composition.',
    }
  }

  return {
    state: 'compatible',
    audience,
    material,
    requested,
    effective,
    compatible,
    missingCapabilities,
    explanation: effective.label + ' is compatible with '
      + material.label.toLowerCase() + ' and the '
      + audience.label.toLowerCase() + ' profile.',
  }
}
