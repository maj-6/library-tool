"use strict";

function dataName(attribute) {
  return attribute.slice(5).replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
}

class FakeNode {
  constructor(tagName, documentRef = null) {
    this.tagName = String(tagName || "div").toUpperCase();
    this.ownerDocument = documentRef;
    this.parentNode = null;
    this.children = [];
    this.attributes = new Map();
    this.dataset = {};
    this.style = {};
    this.className = "";
    this.id = "";
    this.value = "";
    this.type = "";
    this.rows = 0;
    this.maxLength = 0;
    this.disabled = false;
    this.hidden = false;
    this.scrollTop = 0;
    this.clientHeight = 336;
    this.listeners = new Map();
    this._textContent = "";
  }

  get textContent() {
    return this._textContent + this.children.map((child) =>
      child && child.textContent != null ? child.textContent : String(child || "")).join("");
  }

  set textContent(value) {
    this._textContent = String(value == null ? "" : value);
    this.children = [];
  }

  append(...nodes) {
    for (const node of nodes) {
      if (node == null) continue;
      if (typeof node === "string") {
        const text = new FakeNode("#text", this.ownerDocument);
        text._textContent = node;
        text.parentNode = this;
        this.children.push(text);
      } else {
        node.parentNode = this;
        this.children.push(node);
      }
    }
  }

  replaceChildren(...nodes) {
    for (const child of this.children) child.parentNode = null;
    this.children = [];
    this._textContent = "";
    this.append(...nodes);
  }

  removeChild(node) {
    const index = this.children.indexOf(node);
    if (index >= 0) {
      this.children.splice(index, 1);
      node.parentNode = null;
    }
    return node;
  }

  get firstChild() {
    return this.children[0] || null;
  }

  setAttribute(name, value) {
    const text = String(value);
    this.attributes.set(name, text);
    if (name === "id") this.id = text;
    if (name === "class") this.className = text;
    if (name.startsWith("data-")) this.dataset[dataName(name)] = text;
  }

  getAttribute(name) {
    if (name === "id" && this.id) return this.id;
    if (name === "class" && this.className) return this.className;
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }

  removeAttribute(name) {
    this.attributes.delete(name);
    if (name.startsWith("data-")) delete this.dataset[dataName(name)];
  }

  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  removeEventListener(type, listener) {
    this.listeners.set(type, (this.listeners.get(type) || [])
      .filter((candidate) => candidate !== listener));
  }

  emit(type, values = {}) {
    const event = {
      type,
      target: values.target || this,
      currentTarget: this,
      key: values.key,
      defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; },
      ...values,
    };
    for (const listener of this.listeners.get(type) || []) listener(event);
    return event;
  }

  focus() {
    if (this.ownerDocument) this.ownerDocument.activeElement = this;
    this.emit("focus");
  }

  matches(selector) {
    if (!selector) return false;
    if (selector.startsWith("#")) return this.id === selector.slice(1);
    if (selector.startsWith(".")) {
      return this.className.split(/\s+/).includes(selector.slice(1));
    }
    const attribute = selector.match(/^\[([^\]=]+)(?:=['"]?([^'"\]]+)['"]?)?\]$/);
    if (attribute) {
      const [, name, wanted] = attribute;
      let actual = this.getAttribute(name);
      if (actual == null && name.startsWith("data-")) {
        actual = this.dataset[dataName(name)];
      }
      return wanted == null ? actual != null : String(actual) === wanted;
    }
    return this.tagName === selector.toUpperCase();
  }

  querySelector(selector) {
    for (const child of this.children) {
      if (child.matches && child.matches(selector)) return child;
      const nested = child.querySelector && child.querySelector(selector);
      if (nested) return nested;
    }
    return null;
  }

  querySelectorAll(selector) {
    const result = [];
    for (const child of this.children) {
      if (child.matches && child.matches(selector)) result.push(child);
      if (child.querySelectorAll) result.push(...child.querySelectorAll(selector));
    }
    return result;
  }
}

function fakeDocument() {
  const documentRef = {
    activeElement: null,
    createElement(name) {
      return new FakeNode(name, documentRef);
    },
  };
  return documentRef;
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolveValue, rejectValue) => {
    resolve = resolveValue;
    reject = rejectValue;
  });
  return { promise, resolve, reject };
}

module.exports = {
  FakeNode,
  deferred,
  fakeDocument,
};
