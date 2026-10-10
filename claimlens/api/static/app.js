"use strict";
const $ = (id) => document.getElementById(id);
const fieldNames = ["provider_npi", "provider_name", "payer_name", "claim_reference",
  "service_date", "disputed_amount", "billing_violation_type"];
let bearer = "", selected = null, cursor = null, queueGeneration = 0, selectionGeneration = 0;
const notice = (message) => { $("notice").textContent = message; };
async function api(path, options = {}) {
  const response = await fetch(path, {...options, cache: "no-store", credentials: "omit",
    headers: {"Content-Type": "application/json", ...(bearer ? {Authorization: `Bearer ${bearer}`} : {})}});
  if (!response.ok) {
    if (response.status === 409) throw new Error("This document changed. Reload it before making another decision.");
    if (response.status === 401 || response.status === 403) throw new Error("Access denied. Check your token and role.");
    if (response.status === 422) throw new Error("The decision failed validation. Check required fields, NPI, dates, and the rejection note.");
    throw new Error(`Request failed (${response.status}).`);
  }
  return response.json();
}
function clearDetail() {
  selected = null; selectionGeneration++;
  $("review-form").hidden = true; $("fields").replaceChildren(); $("findings").replaceChildren();
  $("detail-title").textContent = "Select a document";
  $("metadata").textContent = "Corrections become evaluation answer keys. Invalid data cannot be approved.";
  $("note").value = "";
}
async function loadQueue(next = false) {
  const generation = ++queueGeneration;
  clearDetail();
  const params = new URLSearchParams({status: $("status").value, limit: "20"});
  if (next && cursor) params.set("cursor", cursor);
  const data = await api(`/claims?${params}`);
  if (generation !== queueGeneration) return;
  cursor = data.next_cursor; $("more").hidden = !cursor;
  $("documents").replaceChildren();
  for (const doc of data.items) {
    const item = document.createElement("li"), button = document.createElement("button");
    button.type = "button";
    button.textContent = `${doc.doc_type || "Unclassified"} · ${doc.id.slice(0, 8)} · ${doc.status}`;
    button.addEventListener("click", () => selectDocument(doc.id).catch((e) => notice(e.message)));
    item.append(button); $("documents").append(item);
  }
  notice(data.items.length ? `${data.items.length} documents on this page.` : "No documents waiting in this queue.");
}
async function selectDocument(id) {
  const generation = ++selectionGeneration;
  const doc = await api(`/documents/${encodeURIComponent(id)}`);
  if (generation !== selectionGeneration) return;
  selected = doc; $("review-form").hidden = false;
  $("detail-title").textContent = `Document ${doc.id.slice(0, 8)}`;
  $("metadata").textContent = `${doc.status} · version ${doc.version} · ${doc.doc_type || "unclassified"}`;
  $("findings").replaceChildren();
  for (const finding of doc.findings) {
    const item = document.createElement("li");
    item.textContent = `${finding.rule}: ${finding.message}`; $("findings").append(item);
  }
  $("fields").replaceChildren();
  const typeLabel = document.createElement("label"); typeLabel.textContent = "Document type";
  const typeSelect = document.createElement("select"); typeSelect.id = "field-doc_type";
  for (const type of ["other", "provider_appeal", "surprise_billing_complaint", "idr_registration"]) {
    const option = document.createElement("option"); option.value = type; option.textContent = type;
    typeSelect.append(option);
  }
  typeSelect.value = doc.doc_type || "other"; typeLabel.append(typeSelect); $("fields").append(typeLabel);
  for (const field of fieldNames) {
    const label = document.createElement("label"); label.textContent = field.replaceAll("_", " ");
    const input = document.createElement("input"); input.id = `field-${field}`; input.autocomplete = "off";
    input.value = doc.claim?.[field] ?? "";
    if (field === "disputed_amount") { input.type = "number"; input.step = "0.01"; input.min = "0"; }
    if (field === "service_date") input.type = "date";
    label.append(input); $("fields").append(label);
  }
  $("note").value = "";
}
async function decide(decision) {
  if (!selected) return;
  const current = selected, corrections = {};
  if (decision === "approve") {
    const type = $("field-doc_type").value;
    if (type !== current.doc_type) corrections.doc_type = type;
    for (const field of fieldNames) {
      const raw = $(`field-${field}`).value.trim();
      const value = raw === "" ? null : field === "disputed_amount" ? Number(raw) : raw;
      if (value !== (current.claim?.[field] ?? null)) corrections[field] = value;
    }
  }
  $("approve").disabled = $("reject").disabled = true;
  try {
    await api(`/documents/${encodeURIComponent(current.id)}/review`, {method: "POST",
      body: JSON.stringify({decision, expected_version: current.version, corrections, note: $("note").value || null})});
    await loadQueue(); notice(`Decision saved: ${decision}.`);
  } finally { $("approve").disabled = $("reject").disabled = false; }
}
$("connect").addEventListener("click", () => {
  bearer = $("token").value.trim(); $("token").value = "";
  loadQueue().catch((e) => notice(e.message));
});
$("disconnect").addEventListener("click", () => {
  bearer = ""; $("token").value = ""; queueGeneration++; cursor = null;
  $("documents").replaceChildren(); $("more").hidden = true; clearDetail(); notice("Disconnected.");
});
$("refresh").addEventListener("click", () => loadQueue().catch((e) => notice(e.message)));
$("status").addEventListener("change", () => loadQueue().catch((e) => notice(e.message)));
$("more").addEventListener("click", () => loadQueue(true).catch((e) => notice(e.message)));
$("review-form").addEventListener("submit", (e) => { e.preventDefault(); decide("approve").catch((err) => notice(err.message)); });
$("reject").addEventListener("click", () => decide("reject").catch((e) => notice(e.message)));
