import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../sandbox_agent/static/app.js", import.meta.url), "utf8");

function page(fetch) {
  const elements = new Map();
  function element(selector) {
    if (!elements.has(selector)) elements.set(selector, {
      textContent: "", value: "test", className: "", hidden: false,
      querySelector: (child) => element(`${selector} ${child}`),
      addEventListener(name, handler) { this[name] = handler; },
      replaceChildren() {}, scrollIntoView() {}, append() {},
    });
    return elements.get(selector);
  }
  const context = vm.createContext({
    document: { querySelector: element, querySelectorAll: () => [], createElement: element },
    fetch, TextDecoder, Uint8Array, Error, console,
  });
  vm.runInContext(source, context);
  return { context, element };
}

function stream(chunks) {
  return new ReadableStream({ start(controller) {
    for (const chunk of chunks) controller.enqueue(new TextEncoder().encode(chunk));
    controller.close();
  } });
}

test("validation errors are readable and the submit button recovers", async () => {
  const { element } = page(async () => ({ ok: false, status: 422,
    json: async () => ({ detail: [{ msg: "Use an HTTPS GitHub repository URL" }] }),
  }));
  await element("#inspection-form").submit({ preventDefault() {} });
  assert.equal(element("#form-error").textContent, "Use an HTTPS GitHub repository URL");
  assert.equal(element("#inspection-form button[type='submit']").disabled, false);
});

test("fragmented CRLF stream displays text and completes", async () => {
  const { context, element } = page();
  await context.consumeEventStream(stream([
    'event: answer_delta\r\ndata: {"delta":"Hello"}\r',
    '\n\r\nevent: complete\r\ndata: {"ok":true}\r\n\r\n',
  ]));
  assert.equal(element("#answer").textContent, "Hello");
  assert.equal(element("#run-badge span").textContent, "Complete");
});

test("premature EOF cannot leave a run looking active", async () => {
  const { element } = page(async () => ({ ok: true, body: stream([
    'event: status\ndata: {"stage":"inspect","message":"Inspecting"}\n\n',
  ]) }));
  await element("#inspection-form").submit({ preventDefault() {} });
  assert.match(element("#form-error").textContent, /connection ended/);
  assert.equal(element("#run-badge span").textContent, "Failed");
  assert.equal(element("#inspection-form button[type='submit']").disabled, false);
});

test("a completed failed inspection remains incomplete", async () => {
  const { context, element } = page();
  await context.consumeEventStream(stream(['event: complete\ndata: {"ok":false}\n\n']));
  assert.equal(element("#run-badge span").textContent, "Incomplete");
});
