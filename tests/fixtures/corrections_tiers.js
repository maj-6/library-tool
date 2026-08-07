// Projects the /2 Corrections index into the three tiers the shell now reads.
//
// The fixtures stay the /2 payload: it is still what an untiered sidecar
// serves, it is what the tiered routes are defined against, and a test that
// writes one book should not have to write it three times. These do to a
// fixture what the engine does to the catalogue, so a test keeps describing a
// library rather than a set of responses.

const CORRECTIONS_INDEX_SUMMARY_SCHEMA =
  "librarytool.corrections-index-summary/1";
const CORRECTIONS_INDEX_DETAIL_SCHEMA =
  "librarytool.corrections-index-detail/1";
const CORRECTIONS_CAPTURE_MARKS_SCHEMA =
  "librarytool.corrections-capture-marks/1";


function summaryOf(index) {
  return {
    schema: CORRECTIONS_INDEX_SUMMARY_SCHEMA,
    revision: index.revision,
    books: index.books.map((book) => ({
      id: book.id,
      revision: book.revision,
      kind: book.kind,
      title: book.title,
      review: book.review,
    })),
    attention: index.attention,
  };
}


// Only books that have captures are marked, exactly as the engine reports
// them, so an absent id is the positive claim "no captures".
function marksOf(index) {
  return {
    schema: CORRECTIONS_CAPTURE_MARKS_SCHEMA,
    revision: index.revision,
    marks: index.books
      .filter((book) => book.captures.length > 0)
      .map((book) => ({
        item_id: book.id,
        capture_count: book.captures.length,
        latest_imported_at: book.latest_imported_at || "",
      }))
      .sort((left, right) =>
        left.item_id < right.item_id ? -1
          : left.item_id > right.item_id ? 1 : 0),
  };
}


function detailsOf(index, itemIds) {
  const books = new Map(index.books.map((book) => [book.id, book]));
  return {
    schema: CORRECTIONS_INDEX_DETAIL_SCHEMA,
    revision: index.revision,
    books: itemIds.filter((itemId) => books.has(itemId))
      .map((itemId) => books.get(itemId)),
    missing: itemIds.filter((itemId) => !books.has(itemId)),
  };
}


// Wraps an api that serves /2 in the tiered one the store drives, answering
// the capture tiers from whichever payload loadIndex last returned.
function tiered(api) {
  let last = null;
  return {
    ...api,
    async loadIndex(options) {
      last = await api.loadIndex(options);
      return summaryOf(last);
    },
    async loadCaptureMarks() {
      return marksOf(last);
    },
    async loadDetails({ itemIds }) {
      return detailsOf(last, itemIds);
    },
  };
}


// The store coalesces a render burst into one detail request, so settling
// takes a turn for the queue and a turn for the response. setImmediate rather
// than a bare microtask because each turn must let the whole microtask queue
// drain, not advance it by one.
async function settle(times = 2) {
  for (let round = 0; round < times; round += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}


module.exports = {
  CORRECTIONS_CAPTURE_MARKS_SCHEMA,
  CORRECTIONS_INDEX_DETAIL_SCHEMA,
  CORRECTIONS_INDEX_SUMMARY_SCHEMA,
  detailsOf,
  marksOf,
  settle,
  summaryOf,
  tiered,
};
