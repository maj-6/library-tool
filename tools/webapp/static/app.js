"use strict";

const FIELDS = [
  "title", "subtitle", "author", "publisher", "published_date",
  "language", "edition", "page_count", "notes",
];

const state = {
  books: [],
  currentId: null,
  images: [],
  imageIndex: 0,
  titlePage: 1,
};

const el = (id) => document.getElementById(id);

async function loadBooks() {
  const res = await fetch("/api/books");
  state.books = await res.json();
  renderSidebar();
  updateCounts();
  if (state.books.length && !state.currentId) {
    selectBook(state.books[0].id);
  }
}

function updateCounts() {
  const total = state.books.length;
  const done = state.books.filter((b) => b.submitted).length;
  el("counts").textContent = `${done}/${total} submitted`;
}

function renderSidebar() {
  const filter = el("filter").value.trim().toLowerCase();
  const list = el("book-list");
  list.innerHTML = "";
  for (const b of state.books) {
    if (filter && !b.title.toLowerCase().includes(filter)) continue;
    const li = document.createElement("li");
    li.dataset.id = b.id;
    if (b.id === state.currentId) li.classList.add("active");

    const name = document.createElement("span");
    name.textContent = b.title;
    li.appendChild(name);

    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = b.submitted ? "done" : `${b.image_count} img`;
    li.appendChild(tag);

    li.addEventListener("click", () => selectBook(b.id));
    list.appendChild(li);
  }
}

async function selectBook(id) {
  state.currentId = id;
  el("status").textContent = "";
  renderSidebar();

  const res = await fetch(`/api/book/${id}`);
  if (!res.ok) return;
  const data = await res.json();

  state.images = data.images || [];
  state.titlePage = parseInt(data.title_page_image, 10) || 1;
  state.imageIndex = Math.min(Math.max(state.titlePage - 1, 0), Math.max(state.images.length - 1, 0));
  renderImage();

  el("time-region").textContent = formatRegion(data.time_region);
  el("transcript").textContent = data.transcript || "(no transcript)";

  const m = data.metadata || {};
  for (const f of FIELDS) el(f).value = m[f] || "";
}

function formatRegion(tr) {
  if (!tr) return "";
  let s = `Offset ${tr.start_offset || "?"} - ${tr.end_offset || "?"}`;
  if (tr.start && tr.end) s += `  |  ${tr.start} - ${tr.end}`;
  return s;
}

function renderImage() {
  const img = el("book-image");
  const none = el("no-image");
  if (!state.images.length) {
    img.hidden = true;
    none.hidden = false;
    el("image-pos").textContent = "";
    el("title-page-note").textContent = "";
    return;
  }
  img.hidden = false;
  none.hidden = true;
  const file = state.images[state.imageIndex];
  img.src = `/images/${state.currentId}/${file}`;
  el("image-pos").textContent = `Image ${state.imageIndex + 1} of ${state.images.length}`;
  el("title-page-note").textContent = `Title page: image ${state.titlePage}`;
}

function step(delta) {
  if (!state.images.length) return;
  state.imageIndex = (state.imageIndex + delta + state.images.length) % state.images.length;
  renderImage();
}

async function submitForm(ev) {
  ev.preventDefault();
  if (!state.currentId) return;
  const metadata = {};
  for (const f of FIELDS) metadata[f] = el(f).value;
  const body = { metadata, title_page_image: String(state.titlePage) };

  const res = await fetch(`/api/book/${state.currentId}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.ok) {
    el("status").textContent = "Saved";
    const b = state.books.find((x) => x.id === state.currentId);
    if (b) {
      b.submitted = true;
      b.title = metadata.title.trim() || b.title;
    }
    renderSidebar();
    updateCounts();
  } else {
    el("status").textContent = "Save failed";
  }
}

el("prev-img").addEventListener("click", () => step(-1));
el("next-img").addEventListener("click", () => step(1));
el("set-title-page").addEventListener("click", () => {
  state.titlePage = state.imageIndex + 1;
  renderImage();
});
el("filter").addEventListener("input", renderSidebar);
el("meta-form").addEventListener("submit", submitForm);

loadBooks();
