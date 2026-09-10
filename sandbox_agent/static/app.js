const form = document.querySelector("#inspection-form");
const submitButton = form.querySelector("button[type='submit']");
const runPanel = document.querySelector("#run-panel");
const badge = document.querySelector("#run-badge");
const answer = document.querySelector("#answer");
const activityLog = document.querySelector("#activity-log");
const activityCount = document.querySelector("#activity-count");
const runMeta = document.querySelector("#run-meta");
const evidencePanel = document.querySelector("#evidence");
const artifactsPanel = document.querySelector("#artifacts");
const formError = document.querySelector("#form-error");
const stages = ["session", "sandbox", "inspect", "result"];
let eventCount = 0;
let streamedAnswer = "";

document.querySelectorAll("[data-question]").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelector("#question").value = button.dataset.question;
    document.querySelector("#question").focus();
  });
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  resetRun();
  setBusy(true);
  setStage("session", "Starting");
  runPanel.hidden = false;
  runPanel.scrollIntoView({ behavior: "smooth", block: "start" });

  const payload = {
    repo_url: document.querySelector("#repo-url").value.trim(),
    ref: document.querySelector("#git-ref").value.trim(),
    question: document.querySelector("#question").value.trim(),
  };

  try {
    const response = await fetch("/api/inspect", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      const message = Array.isArray(detail.detail)
        ? detail.detail.map((issue) => issue.msg).filter(Boolean).join("; ")
        : detail.detail;
      throw new Error(message || `Request failed with status ${response.status}`);
    }
    if (!response.body) throw new Error("This browser did not receive a response stream.");
    await consumeEventStream(response.body);
  } catch (error) {
    failRun(error instanceof Error ? error.message : String(error));
  } finally {
    setBusy(false);
  }
});

async function consumeEventStream(stream) {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let completed = false;

  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const frames = buffer.replaceAll("\r\n", "\n").split("\n\n");
      buffer = frames.pop() || "";
      for (const frame of frames.filter(Boolean)) {
        completed = handleFrame(frame) || completed;
      }
      if (done) break;
    }
    if (buffer.trim()) completed = handleFrame(buffer) || completed;
    if (!completed) throw new Error("The connection ended before the inspection finished. Try again.");
  } finally {
    reader.releaseLock();
  }
}

function handleFrame(frame) {
  let eventName = "message";
  const dataLines = [];
  frame.split("\n").forEach((line) => {
    if (line.startsWith("event:")) eventName = line.slice(6).trim();
    if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  });
  if (!dataLines.length) return;

  const data = JSON.parse(dataLines.join("\n"));
  if (eventName === "status") setStage(data.stage, data.message);
  if (eventName === "activity") addActivity(data.message);
  if (eventName === "answer_delta") {
    streamedAnswer += data.delta || "";
    answer.textContent = streamedAnswer || "The agent is preparing an answer…";
  }
  if (eventName === "error") {
    addActivity(data.message);
    badge.className = "run-badge failed";
    badge.querySelector("span").textContent = "Needs attention";
  }
  if (eventName === "warning") addActivity(`Cleanup warning: ${data.message}`);
  if (eventName === "result") renderResult(data);
  if (eventName === "complete") {
    badge.className = `run-badge ${data.ok ? "" : "failed"}`;
    badge.querySelector("span").textContent = data.ok ? "Complete" : "Incomplete";
    return true;
  }
}

function setStage(stage, message) {
  const stageIndex = stages.indexOf(stage);
  document.querySelectorAll("#progress li").forEach((item, index) => {
    item.classList.toggle("active", index === stageIndex);
    item.classList.toggle("done", stageIndex > index);
  });
  badge.className = "run-badge running";
  badge.querySelector("span").textContent = message || "Working";
}

function renderResult(data) {
  setStage("result", "Answer ready");
  answer.textContent = data.answer || streamedAnswer || "No answer was returned.";
  const metadata = [
    ["Model", data.model],
    ["Duration", `${data.duration_seconds}s`],
    ["Git ref", data.ref],
    ["Session", data.session_id],
    ["Sandbox", data.sandbox_id],
  ];
  runMeta.replaceChildren();
  metadata.forEach(([label, value]) => {
    const term = document.createElement("dt");
    term.textContent = label;
    const description = document.createElement("dd");
    description.textContent = value || "—";
    description.title = value || "";
    runMeta.append(term, description);
  });
  renderEvidence(data.evidence || {});
  renderArtifacts(data.artifacts || []);
}

function renderEvidence(evidence) {
  evidencePanel.hidden = false;
  ["files_read", "limitations"].forEach((key) => {
    const list = evidencePanel.querySelector(`[data-evidence='${key}']`);
    list.replaceChildren();
    const items = evidence[key] || [];
    if (!items.length) items.push(key === "files_read" ? "Not reported" : "None reported");
    items.slice(0, 12).forEach((value) => {
      const item = document.createElement("li");
      item.textContent = value;
      list.append(item);
    });
  });
}

function renderArtifacts(artifacts) {
  artifactsPanel.replaceChildren();
  artifacts.forEach((artifact) => {
    const blob = new Blob([artifact.content], { type: artifact.media_type });
    const link = document.createElement("a");
    link.className = "artifact-link";
    link.href = URL.createObjectURL(blob);
    link.download = artifact.name;
    link.innerHTML = `<span>${escapeHtml(artifact.name)}</span><span>↓</span>`;
    artifactsPanel.append(link);
  });
}

function addActivity(message) {
  if (!message) return;
  eventCount += 1;
  const item = document.createElement("li");
  item.textContent = message;
  activityLog.append(item);
  activityCount.textContent = `${eventCount} ${eventCount === 1 ? "event" : "events"}`;
}

function resetRun() {
  eventCount = 0;
  streamedAnswer = "";
  answer.textContent = "Waiting for the agent…";
  activityLog.replaceChildren();
  activityCount.textContent = "0 events";
  runMeta.replaceChildren();
  artifactsPanel.replaceChildren();
  evidencePanel.hidden = true;
  formError.textContent = "";
  document.querySelectorAll("#progress li").forEach((item) => {
    item.classList.remove("active", "done");
  });
}

function failRun(message) {
  formError.textContent = message;
  answer.textContent = `The inspection could not finish.\n\n${message}`;
  badge.className = "run-badge failed";
  badge.querySelector("span").textContent = "Failed";
}

function setBusy(busy) {
  submitButton.disabled = busy;
  submitButton.querySelector("span").textContent = busy ? "Inspecting…" : "Inspect repository";
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
