"""One self-contained page; response content is rendered through text-only DOM APIs."""

from datetime import date
from html import escape

from rent_navigator.models import disclaimer_for

_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rent Navigator</title>
<style>
  :root { color-scheme: light; font-family: system-ui, sans-serif; color: #18302e; }
  body { margin: 0; background: #f7f7f2; line-height: 1.6; }
  main { max-width: 56rem; margin: 2rem auto; padding: 0 1.25rem 3rem; }
  h1 { margin-bottom: 0; } h2, legend { font-weight: 650; }
  section { margin: 1.75rem 0; padding: 1.5rem; background: white; border: 1px solid #bdc8c3; }
  fieldset { margin: 1rem 0; padding: 1rem; border: 1px solid #bdc8c3; }
  label { display: block; margin: .6rem 0; }
  input:not([type=checkbox]), select, textarea { display: block; box-sizing: border-box;
    width: 100%; font: inherit; padding: .5rem; border: 1px solid #647b73; background: white; }
  textarea { min-height: 8rem; } input[type=checkbox] { margin-right: .5rem; }
  button { font: inherit; padding: .6rem 1rem; background: #18594c; color: white;
    border: 1px solid #18594c; cursor: pointer; margin: .4rem .4rem .4rem 0; }
  button:disabled { opacity: .55; cursor: wait; }
  :focus-visible { outline: 3px solid #bd670b; outline-offset: 2px; }
  .fields { display: grid; gap: 0 1.25rem; grid-template-columns: repeat(auto-fit, minmax(14rem,
1fr)); }
  .note, footer { font-size: .9rem; } #disclaimer { border-left: 4px solid #18594c;
padding-left: 1rem; }
  #status { min-height: 1.6rem; } #result p { white-space: pre-wrap; }
  [hidden] { display: none !important; } footer { overflow-wrap: anywhere; }
</style>
</head>
<body><main>
<h1>Rent Navigator</h1>
<p>Ontario rent-increase and notice information, grounded in official sources.</p>
<p id="disclaimer">@@DISCLAIMER@@</p>
<p id="status" role="status" aria-live="polite">Checking service availability.</p>
<section aria-labelledby="letter-title">
<h2 id="letter-title">Check a rent-increase letter</h2>
<p>Remove names, addresses and other personal details before pasting. Review every fact before
checking.</p>
<form id="extract-form">
  <label for="letter">Letter text</label>
  <textarea id="letter" maxlength="4000" required autocomplete="off"></textarea>
  <button id="extract-button" type="submit" disabled>Extract facts</button>
</form>
<form id="facts-form" hidden>
  <p id="extraction-note">These are suggested facts. Correct them and fill in what you know.</p>
  <label for="mode">What would you like to check?</label>
  <select id="mode" required>
    <option value="">Choose a check</option>
    <option value="notice">Notice timing</option><option value="rent">Rent increase</option>
  </select>
  <fieldset id="ordinary-checklist"><legend>Ordinary-scope checklist</legend>
    <label><input class="ordinary" type="checkbox">This is an Ontario private-residential
tenancy with undisputed RTA coverage.</label>
    <label><input class="ordinary" type="checkbox">This is not social housing, a care home, a
co-op or an assigned tenancy.</label>
    <label><input class="ordinary" type="checkbox">This is an ordinary future increase, without
an above-guideline increase, special agreement or additional services.</label>
    <label><input class="ordinary" type="checkbox">There are no seasonal air-conditioning
charges, retrospective paid increases or disputed base rent.</label>
    <label><input class="ordinary" type="checkbox">Only hand delivery or mail is involved. This
is not an eviction or filing deadline, form selection/completion or legal-advice
request.</label>
    <label><input id="scope-excluded" type="checkbox">I know an excluded condition applies.</label>
    <p class="note">Unchecked items mean the ordinary scope is not confirmed.</p>
  </fieldset>
  <fieldset><legend>Rental-period confirmation</legend>
    <label><input id="period-confirmed" type="checkbox">I confirm the proposed effective date
starts a rental period.</label>
    <label><input id="period-excluded" type="checkbox">I know the proposed date is within a
rental period.</label>
  </fieldset>
  <div class="fields">
    <label>Proposed effective date<input id="effective-on" type="date"></label>
    <label>Date the notice was served or mailed<input id="served-on" type="date"></label>
    <label>Service method<select id="service-method"><option value="unknown">I do not
know</option><option value="hand">Hand delivery</option><option
value="mail">Mail</option></select></label>
  </div>
  <fieldset id="rent-fields" hidden><legend>Rent facts</legend><div class="fields">
    <label>Current rent (CAD)<input id="current-rent" inputmode="decimal"
autocomplete="off"></label>
    <label>Proposed rent (CAD)<input id="proposed-rent" inputmode="decimal"
autocomplete="off"></label>
    <label>Tenancy start date<input id="tenancy-start" type="date"></label>
    <label>Previous ordinary increase<select id="last-state"><option value="unknown">I do not
know</option><option value="none">There has been no previous ordinary increase</option><option
value="known">I know its date</option></select></label>
    <label id="last-date-label" hidden>Previous increase date<input id="last-date"
type="date"></label>
    <label>Guideline status<select id="guideline-status"><option value="unknown">I do not
know</option><option value="controlled">Subject to the guideline</option><option
value="exempt_s6_1">Exempt under section 6.1</option></select></label>
    <label>Form named on the notice<select id="notice-form"><option value="unknown">I do not
know</option><option value="N1">N1</option><option value="N2">N2</option><option
value="other">Another form</option></select></label>
  </div></fieldset>
  <p class="note">Leave a date or amount blank if unknown. A late service date is still a fact
to check.</p>
  <label><input id="facts-confirmed" type="checkbox" required>I have reviewed all displayed
facts. Blank fields mean I do not know.</label>
  <button id="ask-button" type="submit" disabled>Confirm facts and check</button>
</form>
</section>
<section aria-labelledby="question-title">
<h2 id="question-title">Ask a general question</h2>
<p>Ask about the rules generally. A particular case needs confirmed facts in the letter form.</p>
<form id="question-form">
  <label for="question">General question</label>
  <textarea id="question" maxlength="1500" required autocomplete="off"></textarea>
  <button id="question-button" type="submit" disabled>Ask question</button>
</form>
</section>
<section id="result" aria-label="Result" aria-live="polite" hidden></section>
<button id="clear" type="button">Clear inputs and results</button>
<noscript><p>This page needs JavaScript to submit questions and confirm facts.</p></noscript>
<footer>Rules snapshot: <span id="snapshot-date">@@SNAPSHOT@@</span>.
Running source: <span id="source-commit">@@SOURCE@@</span>.<br>
Inputs are not saved by the service. Only service metadata is logged.</footer>
</main>
<script>
"use strict";
const byId = id => document.getElementById(id);
const errorWords = Object.freeze({
  invalid_request: "Please review the facts and input format, then try again.",
  body_too_large: "The text is too long. Please shorten it and try again.",
  rate_limited: "The demo has had too many requests this hour. Please try again later.",
  busy: "Another request is running. Please try again in a moment.",
  budget_exhausted: "Today's demo budget is used up. Please try again tomorrow.",
  stale_corpus: "The rules snapshot is more than 90 days old. " +
    "Checks are paused until it is reviewed and refreshed.",
  provider_error: "The information service is unavailable. Please try again later.",
  invalid_generated_output: "The service could not produce a usable answer. " +
    "Please try again later.",
  tool_protocol_error: "The service could not complete the check safely. Please try again later.",
  deadline_exceeded: "The request took too long. Please try again later."
});
let working = false, available = false, attempt = null;
let letterVersion = 0, questionVersion = 0, factsVersion = 0;
const buttons = ["extract-button", "ask-button", "question-button"];
function status(text) { byId("status").textContent = text; }
function lock(value) {
  working = value;
  buttons.forEach(id => { byId(id).disabled = value || !available; });
}
function textNode(tag, text, parent) {
  const node = document.createElement(tag);
  node.textContent = text;
  parent.appendChild(node);
  return node;
}
function newAttempt() {
  if (!globalThis.crypto || typeof crypto.randomUUID !== "function") {
    status("Open this page over HTTPS or localhost before submitting."); return null;
  }
  try { return crypto.randomUUID(); }
  catch (_) { status("Open this page over HTTPS or localhost before submitting."); return null; }
}
async function post(path, body) {
  let response;
  try {
    response = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json"},
      credentials: "same-origin", cache: "no-store", body: JSON.stringify(body)});
    const data = await response.json();
    if (!response.ok) {
      status(errorWords[data.error && data.error.code] ||
        "The request could not be completed. Please try again later.");
      return null;
    }
    return data;
  } catch (_) {
    status("The service could not be reached. Please try again later.");
    return null;
  }
}
function resetFacts() {
  factsVersion++;
  byId("facts-form").reset();
  byId("facts-form").hidden = true;
  byId("rent-fields").hidden = true;
  byId("rent-fields").disabled = true;
  byId("last-date-label").hidden = true;
  byId("last-date").required = false;
  attempt = null;
}
function hideResult() {
  byId("result").replaceChildren();
  byId("result").hidden = true;
}
function money(cents) {
  return Number.isSafeInteger(cents) && cents > 0
    ? `${Math.floor(cents / 100)}.${String(cents % 100).padStart(2, "0")}` : "";
}
function cents(id) {
  const value = byId(id).value.trim();
  if (!value) return null;
  if (!/^\d+(\.\d{1,2})?$/.test(value)) throw new Error("amount");
  const [whole, fraction = ""] = value.split(".");
  const amount = Number(whole) * 100 + Number(fraction.padEnd(2, "0"));
  if (!Number.isSafeInteger(amount) || amount <= 0) throw new Error("amount");
  return amount;
}
function dateValue(id) { return byId(id).value || null; }
function facts() {
  const ordinary = byId("scope-excluded").checked ? "excluded"
    : [...document.querySelectorAll(".ordinary")].every(box => box.checked)
      ? "confirmed" : "unknown";
  const period = byId("period-confirmed").checked ? "confirmed"
    : byId("period-excluded").checked ? "excluded" : "unknown";
  const value = {scope: {ordinary, period_start: period}, effective_on: dateValue("effective-on"),
    served_on: dateValue("served-on"), service_method: byId("service-method").value};
  if (byId("mode").value === "rent") Object.assign(value, {
    current_cents: cents("current-rent"), proposed_cents: cents("proposed-rent"),
    tenancy_start: dateValue("tenancy-start"),
    last_increase: {state: byId("last-state").value,
      date: byId("last-state").value === "known" ? dateValue("last-date") : null},
    guideline_status: byId("guideline-status").value, form: byId("notice-form").value
  });
  return value;
}
function renderAnswer(data) {
  hideResult();
  const result = byId("result");
  textNode("h2", data.status === "refused" ? "More confirmation is needed"
    : "Information and checked conditions", result);
  textNode("p", data.answer, result);
  if (data.tool_result) {
    const checks = Array.isArray(data.tool_result.checks) ? data.tool_result.checks : [];
    const missingFacts = checks.some(check =>
      check.status === "unknown" && check.reason === "missing_fact");
    const roundingUncertain = checks.some(check => check.reason === "rounding_uncertain");
    const labels = {passes_checked_rules: "The checked conditions are met.",
      fails_checked_rules: "At least one checked condition is not met.",
      cannot_determine: missingFacts
        ? "More facts are needed to determine some checked conditions."
        : "The checked conditions cannot be fully determined.",
      unsupported: "This scenario is outside the supported scope."};
    textNode("p", labels[data.tool_result.status] ||
      "Review the checked conditions above.", result);
    if (roundingUncertain) textNode("p",
      "The exact guideline cap includes a fraction of a cent. Under this project's policy, " +
      "a proposed rent at the next whole cent remains uncertain. " +
      "This is not a statutory rounding rule and does not establish a lawful increase.", result);
  }
  if (Array.isArray(data.citations) && data.citations.length) {
    textNode("h3", "Official sources", result);
    const list = document.createElement("ul"); result.appendChild(list);
    data.citations.forEach(citation => {
      const item = document.createElement("li"); list.appendChild(item);
      try {
        const url = new URL(citation.url);
        if (!["https:", "http:"].includes(url.protocol)) throw new Error("url");
        const link = textNode("a", citation.heading, item);
        link.href = url.href; link.target = "_blank"; link.rel = "noopener noreferrer";
      } catch (_) { textNode("span", citation.heading, item); }
    });
  }
  result.hidden = false;
}
byId("letter").addEventListener("input", () => { letterVersion++; resetFacts(); hideResult(); });
byId("question").addEventListener("input", () => { questionVersion++; });
byId("mode").addEventListener("change", () => {
  byId("rent-fields").hidden = byId("mode").value !== "rent";
  byId("rent-fields").disabled = byId("mode").value !== "rent";
  byId("last-date").required = byId("mode").value === "rent"
    && byId("last-state").value === "known";
});
byId("last-state").addEventListener("change", () => {
  byId("last-date-label").hidden = byId("last-state").value !== "known";
  byId("last-date").required = byId("mode").value === "rent"
    && byId("last-state").value === "known";
  if (byId("last-state").value !== "known") byId("last-date").value = "";
});
byId("facts-form").addEventListener("input", event => {
  factsVersion++;
  if (event.target.id !== "facts-confirmed") byId("facts-confirmed").checked = false;
});
byId("scope-excluded").addEventListener("change", () => {
  if (byId("scope-excluded").checked) {
    document.querySelectorAll(".ordinary").forEach(box => { box.checked = false; });
  }
});
document.querySelectorAll(".ordinary").forEach(box => box.addEventListener("change", () => {
  if (box.checked) byId("scope-excluded").checked = false;
}));
["period-confirmed", "period-excluded"].forEach((id, index, ids) =>
  byId(id).addEventListener("change", () => {
  if (byId(id).checked) byId(ids[1 - index]).checked = false;
}));
byId("extract-form").addEventListener("submit", async event => {
  event.preventDefault(); if (working || !available) return;
  const version = letterVersion; resetFacts(); hideResult(); lock(true);
  status("Reading the letter. You will confirm the facts next.");
  try {
    const id = newAttempt();
    if (!id) return;
    const data = await post("/api/extract", {attempt_id: id, letter: byId("letter").value});
    if (version !== letterVersion) {
      status("The letter changed. Extract its facts again."); return;
    }
    if (data) {
      attempt = id;
      byId("current-rent").value = money(data.extraction.current_cents);
      byId("proposed-rent").value = money(data.extraction.proposed_cents);
      byId("effective-on").value = data.extraction.effective_on || "";
      byId("facts-form").hidden = false;
      status("Review the suggested facts and make the confirmations below.");
    }
  } catch (_) { status("The service did not return usable facts. Please try again later."); }
  finally { lock(false); }
});
byId("facts-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (working || !available || !attempt || !byId("facts-confirmed").checked) return;
  const version = letterVersion; hideResult();
  const revision = factsVersion;
  try {
    const body = {mode: byId("mode").value, attempt_id: attempt, confirmed: true, facts: facts()};
    lock(true); status("Checking the confirmed facts.");
    const data = await post("/api/ask", body);
    if (version !== letterVersion) {
      status("The letter changed. Extract its facts again."); return;
    }
    if (revision !== factsVersion) {
      status("The facts changed. Confirm and check them again."); return;
    }
    if (data) {
      renderAnswer(data); status("The check is complete. Review the information and sources.");
    }
  } catch (_) {
    status("Enter positive CAD amounts with at most two decimal places, or leave them blank.");
  }
  finally { lock(false); }
});
byId("question-form").addEventListener("submit", async event => {
  event.preventDefault(); if (working || !available) return;
  const version = questionVersion; hideResult(); lock(true);
  status("Looking up general information.");
  try {
    const id = newAttempt(); if (!id) return;
    const data = await post("/api/ask", {mode: "question", attempt_id: id,
      question: byId("question").value});
    if (version !== questionVersion) { status("The question changed. Submit it again."); return; }
    if (data) { renderAnswer(data); status("The information is ready. Review its sources."); }
  } catch (_) { status("The service did not return usable information. Please try again later."); }
  finally { lock(false); }
});
byId("clear").addEventListener("click", () => {
  letterVersion++; questionVersion++; byId("extract-form").reset(); byId("question-form").reset();
  resetFacts(); hideResult(); status("Inputs and results cleared.");
});
fetch("/healthz", {cache: "no-store", credentials: "same-origin"}).then(response => {
  if (!response.ok) throw new Error("health"); return response.json();
}).then(data => {
  available = data.status === "ok" && data.source_commit === byId("source-commit").textContent;
  status(available ? "Ready. Choose a letter check or a general question."
    : "The service changed. Reload this page before submitting."); lock(false);
}).catch(() => { status("The service is unavailable. Please try again later."); lock(false); });
</script>
</body></html>"""


def page_html(snapshot_date: date, source_commit: str) -> str:
    return (
        _PAGE.replace("@@DISCLAIMER@@", escape(disclaimer_for(snapshot_date)))
        .replace("@@SNAPSHOT@@", snapshot_date.isoformat())
        .replace("@@SOURCE@@", escape(source_commit))
    )
