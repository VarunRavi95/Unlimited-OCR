const $ = (selector) => document.querySelector(selector);

const form = $("#upload-form");
const fileInput = $("#invoice-file");
const fileLabel = $("#file-label");
const fileMeta = $("#file-meta");
const dropZone = $("#drop-zone");
const submitButton = $("#submit-button");
const formMessage = $("#form-message");
const jobCard = $("#job-card");
const retryButton = $("#retry-button");
const storageKey = "invoice-intelligence:last-job";

let currentJobId = null;
let pollTimer = null;
let elapsedTimer = null;
let currentResult = null;
let currentTrust = null;
let currentJob = null;
let lastStatusAt = null;

const stages = {
  queued: ["Waiting for the GPU worker", 8, 0],
  validating: ["Validating document", 15, 0],
  ocr: ["Reading document with Unlimited-OCR", 43, 1],
  bedrock: ["Translating and mapping invoice fields", 74, 2],
  persisting: ["Writing traceability artifacts", 92, 2],
  completed: ["Extraction completed", 100, 3],
  failed: ["Processing failed", 100, -1],
};

fileInput.addEventListener("change", () => selectFile(fileInput.files[0]));

["dragenter", "dragover"].forEach((name) => {
  dropZone.addEventListener(name, (event) => {
    event.preventDefault();
    dropZone.classList.add("dragging");
  });
});

["dragleave", "drop"].forEach((name) => {
  dropZone.addEventListener(name, (event) => {
    event.preventDefault();
    dropZone.classList.remove("dragging");
  });
});

dropZone.addEventListener("drop", (event) => {
  if (!event.dataTransfer.files.length) return;
  fileInput.files = event.dataTransfer.files;
  selectFile(event.dataTransfer.files[0]);
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!fileInput.files.length) {
    setMessage("Choose an invoice before starting extraction.");
    return;
  }

  setSubmitting(true);
  setMessage("Uploading and validating your invoice...", "success");
  $("#results").classList.add("hidden");
  const body = new FormData();
  body.append("file", fileInput.files[0]);

  try {
    const response = await fetch("/api/jobs", { method: "POST", body });
    const payload = await parse(response);
    if (!response.ok) throw new Error(payload.detail || "Upload failed");

    currentJobId = payload.job_id;
    localStorage.setItem(storageKey, currentJobId);
    showJob(payload);
    showPreview(currentJobId, fileInput.files[0].name);
    setMessage(
      payload.stage === "bedrock"
        ? "Saved OCR will be reused; retrying the Bedrock stage only."
        : "The invoice has been re-queued.",
      "success",
    );
    await pollJob();
  } catch (error) {
    setMessage(error.message);
  } finally {
    setSubmitting(false);
  }
});

retryButton.addEventListener("click", async () => {
  if (!currentJobId) return;
  retryButton.disabled = true;
  setMessage("Re-queuing the invoice...", "success");
  try {
    const response = await fetch(`/api/jobs/${currentJobId}/retry`, { method: "POST" });
    const payload = await parse(response);
    if (!response.ok) throw new Error(payload.detail || "Retry failed");
    retryButton.classList.add("hidden");
    setMessage("");
    updateJob(payload);
    await pollJob();
  } catch (error) {
    setMessage(error.message);
  } finally {
    retryButton.disabled = false;
  }
});

async function pollJob() {
  clearTimeout(pollTimer);
  if (!currentJobId) return;

  try {
    const response = await fetch(`/api/jobs/${currentJobId}`, { cache: "no-store" });
    const job = await parse(response);
    if (!response.ok) throw new Error(job.detail || "Could not read job status");

    if (jobCard.classList.contains("hidden")) showJob(job);
    updateJob(job);

    if (job.status === "completed") {
      setMessage("");
      renderResults(job);
      return;
    }
    if (job.status === "failed") {
      setMessage(job.error || "Processing failed. Review the message and retry the job.");
      retryButton.classList.remove("hidden");
      return;
    }

    pollTimer = setTimeout(pollJob, 2000);
  } catch (error) {
    setMessage(`${error.message}. Reconnecting...`);
    pollTimer = setTimeout(pollJob, 5000);
  }
}

function selectFile(file) {
  if (!file) {
    fileLabel.textContent = "Drop an invoice here";
    fileMeta.classList.add("hidden");
    return;
  }
  fileLabel.textContent = file.name;
  const type = file.type === "application/pdf" ? "PDF" : (file.type.split("/")[1] || "document").toUpperCase();
  fileMeta.textContent = `${type}  |  ${formatBytes(file.size)}`;
  fileMeta.classList.remove("hidden");
}

function showJob(job) {
  const identifier = job.job_id || job.id;
  if (identifier) currentJobId = identifier;
  jobCard.classList.remove("hidden");
  $("#job-id").textContent = currentJobId;
  updateJob(job);
}

function updateJob(job) {
  currentJob = job;
  lastStatusAt = Date.now();
  const state = job.status || "queued";
  const stage = job.stage || state;
  const [label, progress, stageIndex] = stages[stage] || [labelize(stage), 12, 0];
  const stateElement = $("#job-status");

  stateElement.textContent = state;
  stateElement.className = `job-state ${state}`;
  $("#job-stage").textContent = label;
  $("#progress-bar").style.width = `${progress}%`;
  $("#progress-bar").style.background = state === "failed"
    ? "var(--red-700)"
    : "linear-gradient(90deg, var(--indigo-500), var(--cyan-400))";

  jobCard.classList.toggle("is-complete", state === "completed");
  jobCard.classList.toggle("is-failed", state === "failed");
  updatePipeline(stageIndex, state);
  updateLiveDetails(job);
  startElapsedTicker();
}

function updateLiveDetails(job) {
  const state = job.status || "queued";
  const stage = job.stage || state;
  const started = job.started_at || job.created_at;
  const finished = job.completed_at;
  const startTime = started ? Date.parse(started) : NaN;
  const endTime = finished ? Date.parse(finished) : Date.now();
  const elapsedSeconds = Number.isFinite(startTime)
    ? Math.max(0, (endTime - startTime) / 1000)
    : 0;
  $("#job-elapsed").textContent = `Elapsed ${formatDuration(elapsedSeconds) || "0s"}`;

  const checkedAgo = lastStatusAt ? Math.max(0, Math.floor((Date.now() - lastStatusAt) / 1000)) : 0;
  let activity = "Waiting for processing activity";
  if (stage === "ocr") activity = `GPU inference active · confirmed ${checkedAgo}s ago`;
  else if (stage === "bedrock") {
    activity = job.timings?.ocr_seconds
      ? `OCR preserved (${formatDuration(job.timings.ocr_seconds)}) · Bedrock active`
      : "Bedrock translation and extraction active";
  } else if (stage === "persisting") activity = "Writing result and traceability artifacts";
  else if (state === "completed") activity = "All processing stages completed";
  else if (state === "failed") activity = "Processing stopped · review the error message";
  $("#job-activity").textContent = activity;
}

function startElapsedTicker() {
  if (elapsedTimer) return;
  elapsedTimer = setInterval(() => {
    if (currentJob) updateLiveDetails(currentJob);
  }, 1000);
}

function updatePipeline(activeIndex, state) {
  document.querySelectorAll("#job-pipeline li").forEach((item, index) => {
    item.classList.toggle("done", state === "completed" || (activeIndex >= 0 && index < activeIndex));
    item.classList.toggle("active", state !== "failed" && state !== "completed" && index === activeIndex);
  });
}

function showPreview(jobId, filename = "Source loaded") {
  $("#preview-empty").classList.add("hidden");
  const frame = $("#source-preview");
  frame.src = `/api/jobs/${jobId}/artifacts/source`;
  frame.classList.remove("hidden");
  $("#preview-label").textContent = filename;
}

function renderResults(job) {
  const data = job.result;
  if (!data) return;
  currentResult = data;
  currentTrust = job.trust || null;
  $("#results").classList.remove("hidden");
  renderArtifacts(job);
  renderTrust(job.trust || null);
  renderWarnings(data.warnings || []);
  renderSummary(data, job.timings || {});
  renderFields(data);
  renderItems(data.line_items || []);
  $("#translation-content").textContent = data.translated_markdown || "No translated Markdown was returned.";
  $("#json-content").textContent = JSON.stringify(data, null, 2);
  $("#results").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderArtifacts(job) {
  const container = $("#artifact-links");
  container.replaceChildren();
  const artifacts = [
    ["clean", "OCR Markdown"],
    ["raw", "Raw OCR"],
    ["ocr_metadata", "OCR metadata"],
    ["ocr_likelihoods", "OCR likelihood diagnostics"],
    ["trust", "Trust assessment"],
    ["result", "Result JSON"],
    ["manifest", "Manifest"],
  ];
  artifacts
    .filter(([key]) => job.artifacts?.[key])
    .forEach(([key, label]) => {
      const link = node("a", "", label);
      link.href = `/api/jobs/${job.id}/artifacts/${key}`;
      link.setAttribute("target", "_blank");
      link.setAttribute("rel", "noopener");
      container.appendChild(link);
    });
}

function renderWarnings(warnings) {
  const container = $("#warnings");
  container.replaceChildren();
  if (!warnings.length) {
    container.classList.add("hidden");
    return;
  }

  const heading = node("div", "warning-heading");
  heading.append(
    node("strong", "", "Review notes"),
    node("span", "warning-count", `${warnings.length} ${warnings.length === 1 ? "note" : "notes"}`),
  );
  const list = document.createElement("ul");
  warnings.forEach((warning) => list.appendChild(node("li", "", warning)));
  container.append(heading, list);
  container.classList.remove("hidden");
}

function renderSummary(data, timings) {
  const values = [
    ["DOC", "Document", data.document_type],
    ["#", "Invoice number", data.invoice?.invoice_number],
    ["SUP", "Supplier", data.supplier?.name],
    ["TOT", "Grand total", [data.invoice?.currency, data.totals?.grand_total].filter(Boolean).join(" ")],
    ["TIME", "Processing time", formatDuration(timings.total_seconds)],
  ];
  const container = $("#summary-grid");
  container.replaceChildren();
  values.forEach(([icon, label, value]) => {
    const card = node("div", "summary-card");
    card.append(
      node("span", "summary-icon", icon),
      node("small", "", label),
      valueElement(value),
    );
    container.appendChild(card);
  });
}

function renderFields(data) {
  const container = $("#tab-fields");
  const grid = node("div", "field-grid");
  container.replaceChildren();

  const groups = [
    ["Supplier", "supplier", data.supplier, ["name", "address", "tax_id"]],
    ["Buyer", "buyer", data.buyer, ["name", "address", "tax_id"]],
    ["Invoice", "invoice", data.invoice, ["invoice_number", "purchase_order_number", "invoice_date", "due_date", "currency", "payment_terms"]],
    ["Totals", "totals", data.totals, ["subtotal", "discount", "shipping", "tax_total", "grand_total", "amount_due"]],
    ["Payment", "payment", data.payment, ["bank_name", "account_name", "account_number", "iban", "swift_bic"]],
  ];

  groups.forEach(([title, prefix, object, fields]) => {
    const group = node("section", "field-group");
    const header = node("div", "field-group-header");
    const populated = fields.filter((field) => object?.[field] !== null && object?.[field] !== "").length;
    header.append(node("h4", "", title), node("span", "field-count", `${populated}/${fields.length} populated`));
    group.appendChild(header);

    const list = node("div", "field-list");
    fields.forEach((field) => {
      const row = node("div", "field-row");
      row.append(
        node("span", "", labelize(field)),
        fieldValueWithTrust(object?.[field], `${prefix}.${field}`),
      );
      list.appendChild(row);
    });
    group.appendChild(list);

    if (title === "Totals" && object?.taxes?.length) group.appendChild(renderTaxes(object.taxes));
    if (object?.evidence?.length) group.appendChild(renderEvidence(object.evidence));
    grid.appendChild(group);
  });

  container.appendChild(grid);
}

function renderTaxes(taxes) {
  const wrapper = node("div", "tax-breakdown");
  wrapper.appendChild(node("strong", "", "Tax breakdown"));
  taxes.forEach((tax, index) => {
    const entry = node("div", "tax-entry");
    entry.append(
      fieldValueWithTrust(tax.tax_type, `totals.taxes[${index}].tax_type`),
      fieldValueWithTrust(tax.base_amount, `totals.taxes[${index}].base_amount`),
      fieldValueWithTrust(tax.rate, `totals.taxes[${index}].rate`),
      fieldValueWithTrust(tax.amount, `totals.taxes[${index}].amount`),
    );
    wrapper.appendChild(entry);
  });
  return wrapper;
}

function renderEvidence(evidence) {
  const details = node("details", "evidence-panel");
  details.appendChild(node("summary", "", `View source evidence (${evidence.length})`));
  const list = document.createElement("ul");
  evidence.forEach((snippet) => list.appendChild(node("li", "", snippet)));
  details.appendChild(list);
  return details;
}

function renderItems(items) {
  const container = $("#tab-items");
  container.replaceChildren();
  if (!items.length) {
    container.appendChild(node("div", "empty-table", "No line items were identified in this document."));
    return;
  }

  const shell = node("div", "table-shell");
  const table = document.createElement("table");
  const columns = [
    ["Description", "description_original"],
    ["English", "description_english"],
    ["Qty", "quantity"],
    ["Unit", "unit"],
    ["Unit price", "unit_price"],
    ["Tax", "tax_amount"],
    ["Total", "line_total"],
    ["Evidence", "evidence"],
  ];

  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  columns.forEach(([label]) => headRow.appendChild(node("th", "", label)));
  head.appendChild(headRow);
  table.appendChild(head);

  const body = document.createElement("tbody");
  items.forEach((item, itemIndex) => {
    const row = document.createElement("tr");
    columns.forEach(([, field]) => {
      const cell = document.createElement("td");
      if (field === "evidence") {
        cell.appendChild(node("span", "evidence-chip", item.evidence?.[0] || "No snippet"));
      } else {
        cell.appendChild(fieldValueWithTrust(item[field], `line_items[${itemIndex}].${field}`));
      }
      row.appendChild(cell);
    });
    body.appendChild(row);
  });
  table.appendChild(body);
  shell.appendChild(table);
  container.appendChild(shell);
}

function trustField(path) {
  return currentTrust?.fields?.find((field) => field.field_path === path) || null;
}

function fieldValueWithTrust(value, path) {
  const wrapper = node("div", "field-value-wrap");
  wrapper.appendChild(valueElement(value));
  const assessment = trustField(path);
  if (assessment) wrapper.appendChild(trustBadge(assessment.status, assessment.calibrated_confidence));
  return wrapper;
}

function trustBadge(status, calibratedConfidence = null) {
  const normalized = String(status || "not_assessed").replaceAll("_", "-");
  const labels = {
    supported: calibratedConfidence !== null && calibratedConfidence !== undefined
      ? `${Math.round(Number(calibratedConfidence) * 100)}%`
      : "Evidence supported",
    review: "Review",
    unsupported: "Unsupported",
    "not-assessed": "Not assessed",
    ready: "Ready",
    "review-required": "Review required",
    "insufficient-support": "Insufficient support",
  };
  return node("span", `trust-badge ${normalized}`, labels[normalized] || labelize(String(status || "not_assessed")));
}

function renderTrust(trust) {
  const panel = $("#trust-panel");
  const detailsContainer = $("#tab-trust");
  detailsContainer.replaceChildren();
  panel.classList.remove("hidden");
  if (!trust) {
    $("#trust-document-status").replaceWith(trustBadgeElement("not_assessed", null, "trust-document-status"));
    $("#trust-summary").textContent = "This historical result does not contain a trust assessment. Its successful OCR is preserved and will not be rerun automatically.";
    $("#trust-counts").replaceChildren();
    $("#trust-reasons").replaceChildren();
    $("#trust-disclaimer").textContent = "No confidence or likelihood value has been inferred for this job.";
    detailsContainer.appendChild(node("div", "empty-table", "Trust diagnostics are unavailable for this historical result."));
    return;
  }

  const documentStatus = trust.document?.status || "review_required";
  $("#trust-document-status").replaceWith(trustBadgeElement(documentStatus, null, "trust-document-status"));
  const calibrated = trust.mode === "calibrated" && trust.calibration_version;
  $("#trust-summary").textContent = calibrated
    ? `Calibrated field correctness is active (${trust.calibration_version}). Percentages estimate normalized field-value correctness.`
    : "This assessment uses exact OCR evidence and deterministic AP checks. Numerical confidence remains hidden until calibration passes its benchmark gates.";

  const counts = ["supported", "review", "unsupported", "not_assessed"].map((status) => [
    status,
    (trust.fields || []).filter((field) => field.status === status).length,
  ]);
  const countContainer = $("#trust-counts");
  countContainer.replaceChildren();
  counts.forEach(([status, count]) => {
    const item = node("span", "trust-count");
    item.append(node("strong", "", String(count)), document.createTextNode(labelize(status)));
    countContainer.appendChild(item);
  });

  const reasons = $("#trust-reasons");
  reasons.replaceChildren();
  (trust.document?.review_reasons || []).forEach((reason) => reasons.appendChild(node("li", "", reason)));
  $("#trust-disclaimer").textContent = trust.disclaimer || "Model likelihood is diagnostic and is not calibrated confidence.";
  renderTrustDetails(trust, detailsContainer);
}

function trustBadgeElement(status, confidence, id) {
  const badge = trustBadge(status, confidence);
  badge.id = id;
  return badge;
}

function renderTrustDetails(trust, container) {
  const list = node("div", "trust-detail-list");
  (trust.fields || []).forEach((field) => {
    const detail = document.createElement("details");
    detail.className = "trust-detail";
    const summary = document.createElement("summary");
    const score = field.calibrated_confidence !== null && field.calibrated_confidence !== undefined
      ? `${Math.round(Number(field.calibrated_confidence) * 100)}% estimated correctness`
      : "No calibrated percentage";
    summary.append(
      node("span", "trust-detail-path", field.field_path),
      node("span", "trust-detail-score", score),
      trustBadge(field.status, field.calibrated_confidence),
    );
    detail.appendChild(summary);
    const body = node("div", "trust-detail-body");
    body.appendChild(node("div", "", `Extracted value: ${display(field.value)}`));
    if (field.review_reasons?.length) {
      body.appendChild(node("h5", "", "Review reasons"));
      const reasons = document.createElement("ul");
      field.review_reasons.forEach((reason) => reasons.appendChild(node("li", "", reason)));
      body.appendChild(reasons);
    }
    if (field.evidence_matches?.length) {
      body.appendChild(node("h5", "", "OCR evidence"));
      field.evidence_matches.forEach((match) => {
        body.appendChild(node("div", "trust-evidence-quote", match.snippet));
        body.appendChild(node("div", "", `${labelize(match.match_type)} match · ${match.occurrences ?? 0} occurrence(s) · value ${match.value_supported ? "supported" : "not supported"}`));
      });
    }
    if (field.validation_signals?.length) {
      body.appendChild(node("h5", "", "Deterministic checks"));
      const signals = document.createElement("ul");
      field.validation_signals.forEach((signal) => signals.appendChild(node("li", "", signal.message)));
      body.appendChild(signals);
    }
    if (field.diagnostic_likelihood !== null && field.diagnostic_likelihood !== undefined) {
      body.appendChild(node("h5", "", "Engineering diagnostic"));
      body.appendChild(node("div", "", `OCR token likelihood: ${Number(field.diagnostic_likelihood).toFixed(4)} (uncalibrated; never shown as correctness probability).`));
    }
    detail.appendChild(body);
    list.appendChild(detail);
  });
  if (!list.childElementCount) list.appendChild(node("div", "empty-table", "No fields were available for assessment."));
  container.appendChild(list);
}

document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((tab) => {
      tab.classList.remove("active");
      tab.setAttribute("aria-selected", "false");
    });
    document.querySelectorAll(".tab-content").forEach((content) => content.classList.add("hidden"));
    button.classList.add("active");
    button.setAttribute("aria-selected", "true");
    $(`#tab-${button.dataset.tab}`).classList.remove("hidden");
  });
});

$("#copy-json-button").addEventListener("click", async (event) => {
  if (!currentResult) return;
  const button = event.currentTarget;
  const text = JSON.stringify(currentResult, null, 2);
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const helper = document.createElement("textarea");
    helper.value = text;
    helper.style.position = "fixed";
    helper.style.opacity = "0";
    document.body.appendChild(helper);
    helper.select();
    document.execCommand("copy");
    helper.remove();
  }
  button.textContent = "Copied";
  setTimeout(() => { button.textContent = "Copy JSON"; }, 1600);
});

function node(tag, className = "", text = "") {
  const element = document.createElement(tag);
  if (className) element.className = className;
  element.textContent = text;
  return element;
}

function valueElement(value) {
  const element = node("strong", "", display(value));
  if (isMissing(value)) element.classList.add("null-value");
  return element;
}

function display(value) {
  return isMissing(value) ? "Not available" : String(value);
}

function isMissing(value) {
  return value === null || value === undefined || value === "";
}

function labelize(value) {
  return value.replaceAll("_", " ").replace(/^./, (char) => char.toUpperCase());
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 KB";
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / (1024 ** index)).toFixed(index ? 1 : 0)} ${units[index]}`;
}

function formatDuration(seconds) {
  if (!Number.isFinite(Number(seconds))) return null;
  const value = Number(seconds);
  return value < 60 ? `${value.toFixed(1)} sec` : `${Math.floor(value / 60)}m ${Math.round(value % 60)}s`;
}

function setSubmitting(active) {
  submitButton.disabled = active;
  submitButton.classList.toggle("loading", active);
  submitButton.querySelector(".button-label").textContent = active ? "Submitting invoice" : "Start extraction";
}

function setMessage(message, type = "error") {
  formMessage.textContent = message;
  formMessage.classList.toggle("success", Boolean(message) && type === "success");
}

async function parse(response) {
  const text = await response.text();
  try {
    return JSON.parse(text);
  } catch {
    return { detail: text || response.statusText };
  }
}

async function checkHealth() {
  const status = $("#service-status");
  try {
    const response = await fetch("/api/health", { cache: "no-store" });
    if (!response.ok) throw new Error();
    status.childNodes[status.childNodes.length - 1].textContent = " Services online";
    status.classList.add("online");
    status.classList.remove("offline");
  } catch {
    status.childNodes[status.childNodes.length - 1].textContent = " Service unavailable";
    status.classList.add("offline");
    status.classList.remove("online");
  }
}

async function restoreLastJob() {
  const jobId = localStorage.getItem(storageKey);
  if (!jobId) return;
  currentJobId = jobId;
  try {
    const response = await fetch(`/api/jobs/${jobId}`, { cache: "no-store" });
    if (!response.ok) {
      localStorage.removeItem(storageKey);
      return;
    }
    const job = await parse(response);
    showJob(job);
    showPreview(jobId, job.filename || "Source loaded");
    if (job.status === "completed") renderResults(job);
    else if (job.status === "failed") {
      setMessage(job.error || "The previous job failed and can be retried.");
      retryButton.classList.remove("hidden");
    } else {
      pollJob();
    }
  } catch {
    setMessage("The last job could not be restored. You can submit a new invoice.");
  }
}

window.addEventListener("beforeunload", () => {
  clearTimeout(pollTimer);
  clearInterval(elapsedTimer);
});
checkHealth();
restoreLastJob();
