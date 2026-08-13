PRAGMA foreign_keys = ON;
PRAGMA user_version = 1;

CREATE TABLE IF NOT EXISTS authority_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS agent (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('human', 'model', 'software', 'import')),
    display_name TEXT NOT NULL CHECK (length(trim(display_name)) > 0),
    version TEXT,
    uri TEXT,
    note TEXT NOT NULL DEFAULT ''
) STRICT;

-- A registry makes assertion endpoints real foreign keys while keeping the
-- subject/object model extensible. Child-table triggers enforce node kind.
CREATE TABLE IF NOT EXISTS node (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN (
        'name', 'mention', 'concept', 'referent',
        'assertion', 'evidence', 'review'
    )),
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS authority_source (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    base_uri TEXT,
    license TEXT,
    scope_note TEXT NOT NULL DEFAULT ''
) STRICT;

CREATE TABLE IF NOT EXISTS witness (
    id TEXT PRIMARY KEY,
    catalog_uri TEXT NOT NULL UNIQUE,
    edition_uri TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    repository TEXT NOT NULL,
    call_number TEXT NOT NULL,
    external_record_id TEXT,
    source_url TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS name_form (
    id TEXT PRIMARY KEY REFERENCES node(id),
    literal TEXT NOT NULL CHECK (length(literal) > 0),
    normalized_key TEXT NOT NULL CHECK (length(normalized_key) > 0),
    language TEXT NOT NULL,
    script TEXT NOT NULL,
    transliteration TEXT,
    period_label TEXT NOT NULL DEFAULT '',
    period_start INTEGER,
    period_end INTEGER,
    geographic_scope TEXT NOT NULL DEFAULT '',
    normalization_profile TEXT NOT NULL,
    source_note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('curated', 'candidate', 'deprecated')),
    created_by TEXT NOT NULL REFERENCES agent(id),
    created_at TEXT NOT NULL,
    UNIQUE (literal, language, script, period_label, geographic_scope),
    CHECK (period_start IS NULL OR period_end IS NULL OR period_start <= period_end)
) STRICT;

CREATE TABLE IF NOT EXISTS concept (
    id TEXT PRIMARY KEY REFERENCES node(id),
    label TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN (
        'plant', 'preparation', 'process', 'ailment', 'body-part',
        'measure', 'person', 'place', 'work', 'illustration'
    )),
    tradition TEXT NOT NULL,
    period_label TEXT NOT NULL,
    period_start INTEGER,
    period_end INTEGER,
    geographic_scope TEXT NOT NULL,
    scope_note TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'candidate', 'deprecated')),
    created_by TEXT NOT NULL REFERENCES agent(id),
    created_at TEXT NOT NULL,
    UNIQUE (label, kind, tradition, period_label, geographic_scope),
    CHECK (period_start IS NULL OR period_end IS NULL OR period_start <= period_end)
) STRICT;

CREATE TABLE IF NOT EXISTS referent (
    id TEXT PRIMARY KEY REFERENCES node(id),
    authority_source_id TEXT NOT NULL REFERENCES authority_source(id),
    authority_identifier TEXT NOT NULL,
    cached_label TEXT NOT NULL,
    cached_at TEXT NOT NULL,
    authority_uri TEXT,
    authority_snapshot TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('candidate', 'active', 'deprecated', 'unresolved')),
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL REFERENCES agent(id),
    created_at TEXT NOT NULL,
    UNIQUE (authority_source_id, authority_identifier)
) STRICT;

-- A mention is one occurrence, never deduplicated. Page-region geometry is its
-- durable anchor; text-anchor revisions are append-only below.
CREATE TABLE IF NOT EXISTS mention (
    id TEXT PRIMARY KEY REFERENCES node(id),
    witness_id TEXT NOT NULL REFERENCES witness(id),
    canvas_uri TEXT NOT NULL,
    region_uri TEXT NOT NULL,
    region_revision TEXT NOT NULL,
    selector_json TEXT NOT NULL CHECK (json_valid(selector_json)),
    name_form_id TEXT NOT NULL REFERENCES name_form(id),
    reading_state TEXT NOT NULL CHECK (reading_state IN (
        'proposed', 'confirmed', 'rejected', 'disputed', 'unresolved'
    )),
    created_by TEXT NOT NULL REFERENCES agent(id),
    created_at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
) STRICT;

CREATE TABLE IF NOT EXISTS mention_anchor (
    id TEXT PRIMARY KEY,
    mention_id TEXT NOT NULL REFERENCES mention(id),
    transcription_layer_uri TEXT NOT NULL,
    transcription_revision TEXT NOT NULL,
    passage_id TEXT NOT NULL,
    char_start INTEGER NOT NULL CHECK (char_start >= 0),
    char_end INTEGER NOT NULL CHECK (char_end >= char_start),
    exact TEXT NOT NULL CHECK (length(exact) = char_end - char_start),
    prefix TEXT NOT NULL,
    suffix TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('current', 'stale', 'repaired', 'ambiguous')),
    supersedes_id TEXT REFERENCES mention_anchor(id),
    created_by TEXT NOT NULL REFERENCES agent(id),
    created_at TEXT NOT NULL,
    UNIQUE (mention_id, transcription_layer_uri, transcription_revision, passage_id, char_start, char_end)
) STRICT;

CREATE TABLE IF NOT EXISTS assertion (
    id TEXT PRIMARY KEY REFERENCES node(id),
    subject_node_id TEXT NOT NULL REFERENCES node(id),
    predicate TEXT NOT NULL CHECK (predicate IN (
        'historical-name-for',
        'modern-name-for',
        'denotes-concept',
        'identified-as',
        'modern-synonym-of'
    )),
    object_node_id TEXT NOT NULL REFERENCES node(id),
    created_by TEXT NOT NULL REFERENCES agent(id),
    created_at TEXT NOT NULL,
    confidence TEXT NOT NULL CHECK (confidence IN (
        'certain', 'likely', 'possible', 'disputed', 'unresolved'
    )),
    state TEXT NOT NULL CHECK (state IN (
        'proposed', 'accepted', 'rejected', 'disputed', 'superseded'
    )),
    method TEXT NOT NULL,
    rationale TEXT NOT NULL,
    supersedes_id TEXT REFERENCES assertion(id),
    CHECK (subject_node_id <> object_node_id),
    UNIQUE (subject_node_id, predicate, object_node_id, created_by, created_at)
) STRICT;

CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY REFERENCES node(id),
    assertion_id TEXT NOT NULL REFERENCES assertion(id),
    kind TEXT NOT NULL CHECK (kind IN (
        'quoted-span', 'page-region', 'external-citation', 'reasoning', 'bibliography'
    )),
    quote TEXT NOT NULL DEFAULT '',
    page_uri TEXT,
    region_uri TEXT,
    selector_json TEXT CHECK (selector_json IS NULL OR json_valid(selector_json)),
    citation_uri TEXT,
    citation_label TEXT NOT NULL DEFAULT '',
    reasoning TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL REFERENCES agent(id),
    created_at TEXT NOT NULL,
    CHECK (
        length(quote) > 0 OR page_uri IS NOT NULL OR region_uri IS NOT NULL OR
        citation_uri IS NOT NULL OR length(reasoning) > 0 OR length(citation_label) > 0
    )
) STRICT;

-- Reviews are append-only. A reviewer who changes the scholarly claim authors
-- a new assertion; the review never rewrites the original assertion.
CREATE TABLE IF NOT EXISTS review (
    id TEXT PRIMARY KEY REFERENCES node(id),
    assertion_id TEXT NOT NULL REFERENCES assertion(id),
    reviewer_id TEXT NOT NULL REFERENCES agent(id),
    decision TEXT NOT NULL CHECK (decision IN (
        'approve', 'reject', 'request-changes', 'abstain'
    )),
    rationale TEXT NOT NULL,
    created_at TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_name_form_normalized
    ON name_form (normalized_key, language, script);
CREATE INDEX IF NOT EXISTS idx_mention_name_form
    ON mention (name_form_id, witness_id);
CREATE INDEX IF NOT EXISTS idx_anchor_mention_time
    ON mention_anchor (mention_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_assertion_subject
    ON assertion (subject_node_id, predicate);
CREATE INDEX IF NOT EXISTS idx_assertion_object
    ON assertion (object_node_id, predicate);
CREATE INDEX IF NOT EXISTS idx_evidence_assertion
    ON evidence (assertion_id);
CREATE INDEX IF NOT EXISTS idx_review_assertion_time
    ON review (assertion_id, created_at DESC, id DESC);

CREATE TRIGGER IF NOT EXISTS name_form_kind_guard
BEFORE INSERT ON name_form
BEGIN
    SELECT CASE WHEN (SELECT kind FROM node WHERE id = NEW.id) <> 'name'
        THEN RAISE(ABORT, 'name_form node must have kind name') END;
END;

CREATE TRIGGER IF NOT EXISTS concept_kind_guard
BEFORE INSERT ON concept
BEGIN
    SELECT CASE WHEN (SELECT kind FROM node WHERE id = NEW.id) <> 'concept'
        THEN RAISE(ABORT, 'concept node must have kind concept') END;
END;

CREATE TRIGGER IF NOT EXISTS referent_kind_guard
BEFORE INSERT ON referent
BEGIN
    SELECT CASE WHEN (SELECT kind FROM node WHERE id = NEW.id) <> 'referent'
        THEN RAISE(ABORT, 'referent node must have kind referent') END;
END;

CREATE TRIGGER IF NOT EXISTS mention_kind_guard
BEFORE INSERT ON mention
BEGIN
    SELECT CASE WHEN (SELECT kind FROM node WHERE id = NEW.id) <> 'mention'
        THEN RAISE(ABORT, 'mention node must have kind mention') END;
END;

CREATE TRIGGER IF NOT EXISTS assertion_guards
BEFORE INSERT ON assertion
BEGIN
    SELECT CASE WHEN (SELECT kind FROM node WHERE id = NEW.id) <> 'assertion'
        THEN RAISE(ABORT, 'assertion node must have kind assertion') END;
    SELECT CASE
        WHEN NEW.predicate = 'historical-name-for'
             AND ((SELECT kind FROM node WHERE id = NEW.subject_node_id) <> 'name'
                  OR (SELECT kind FROM node WHERE id = NEW.object_node_id) <> 'concept')
            THEN RAISE(ABORT, 'historical-name-for requires name -> concept')
        WHEN NEW.predicate = 'modern-name-for'
             AND ((SELECT kind FROM node WHERE id = NEW.subject_node_id) <> 'name'
                  OR (SELECT kind FROM node WHERE id = NEW.object_node_id) <> 'referent')
            THEN RAISE(ABORT, 'modern-name-for requires name -> referent')
        WHEN NEW.predicate = 'denotes-concept'
             AND ((SELECT kind FROM node WHERE id = NEW.subject_node_id) <> 'mention'
                  OR (SELECT kind FROM node WHERE id = NEW.object_node_id) <> 'concept')
            THEN RAISE(ABORT, 'denotes-concept requires mention -> concept')
        WHEN NEW.predicate = 'identified-as'
             AND ((SELECT kind FROM node WHERE id = NEW.subject_node_id) <> 'concept'
                  OR (SELECT kind FROM node WHERE id = NEW.object_node_id) <> 'referent')
            THEN RAISE(ABORT, 'identified-as requires concept -> referent')
        WHEN NEW.predicate = 'modern-synonym-of'
             AND ((SELECT kind FROM node WHERE id = NEW.subject_node_id) <> 'referent'
                  OR (SELECT kind FROM node WHERE id = NEW.object_node_id) <> 'referent')
            THEN RAISE(ABORT, 'modern-synonym-of requires referent -> referent')
    END;
    SELECT CASE WHEN NEW.state <> 'proposed'
            AND (SELECT kind FROM agent WHERE id = NEW.created_by) <> 'human'
        THEN RAISE(ABORT, 'only a human may author a non-proposed assertion') END;
END;

CREATE TRIGGER IF NOT EXISTS evidence_kind_guard
BEFORE INSERT ON evidence
BEGIN
    SELECT CASE WHEN (SELECT kind FROM node WHERE id = NEW.id) <> 'evidence'
        THEN RAISE(ABORT, 'evidence node must have kind evidence') END;
END;

CREATE TRIGGER IF NOT EXISTS review_guards
BEFORE INSERT ON review
BEGIN
    SELECT CASE WHEN (SELECT kind FROM node WHERE id = NEW.id) <> 'review'
        THEN RAISE(ABORT, 'review node must have kind review') END;
    SELECT CASE WHEN (SELECT kind FROM agent WHERE id = NEW.reviewer_id) <> 'human'
        THEN RAISE(ABORT, 'only a human may review an assertion') END;
END;

-- Evidence, assertions, reviews, and anchor repairs are historical records.
-- Corrections append replacements; they never erase or mutate an argument.
CREATE TRIGGER IF NOT EXISTS assertion_no_update BEFORE UPDATE ON assertion
BEGIN SELECT RAISE(ABORT, 'assertions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS assertion_no_delete BEFORE DELETE ON assertion
BEGIN SELECT RAISE(ABORT, 'assertions are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS evidence_no_update BEFORE UPDATE ON evidence
BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;
CREATE TRIGGER IF NOT EXISTS evidence_no_delete BEFORE DELETE ON evidence
BEGIN SELECT RAISE(ABORT, 'evidence is never deleted'); END;
CREATE TRIGGER IF NOT EXISTS review_no_update BEFORE UPDATE ON review
BEGIN SELECT RAISE(ABORT, 'reviews are append-only'); END;
CREATE TRIGGER IF NOT EXISTS review_no_delete BEFORE DELETE ON review
BEGIN SELECT RAISE(ABORT, 'reviews are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS anchor_no_update BEFORE UPDATE ON mention_anchor
BEGIN SELECT RAISE(ABORT, 'mention anchors are append-only'); END;
CREATE TRIGGER IF NOT EXISTS anchor_no_delete BEFORE DELETE ON mention_anchor
BEGIN SELECT RAISE(ABORT, 'mention anchors are never deleted'); END;

CREATE VIEW IF NOT EXISTS assertion_effective_state AS
SELECT
    a.*,
    COALESCE(
        (
            SELECT CASE r.decision
                WHEN 'approve' THEN 'accepted'
                WHEN 'reject' THEN 'rejected'
                WHEN 'request-changes' THEN 'proposed'
                ELSE a.state
            END
            FROM review r
            WHERE r.assertion_id = a.id
            ORDER BY r.created_at DESC, r.id DESC
            LIMIT 1
        ),
        a.state
    ) AS effective_state,
    (
        SELECT newer.id
        FROM assertion newer
        WHERE newer.supersedes_id = a.id
        ORDER BY newer.created_at DESC, newer.id DESC
        LIMIT 1
    ) AS superseded_by
FROM assertion a;

-- The reconciliation view intentionally follows exactly one assertion edge.
-- It never computes identity transitively.
CREATE VIEW IF NOT EXISTS concept_name_form AS
SELECT
    a.object_node_id AS concept_id,
    n.id AS name_form_id,
    n.literal,
    n.normalized_key,
    n.language,
    n.script,
    n.period_label,
    n.geographic_scope,
    a.id AS assertion_id,
    a.confidence,
    a.effective_state
FROM assertion_effective_state a
JOIN name_form n ON n.id = a.subject_node_id
WHERE a.predicate = 'historical-name-for';
