"use strict";

(() => {
  const byId = (id) => document.getElementById(id);
  const terminalStates = new Set(["completed", "failed", "cancelled", "uncertain", "recovered"]);
  let connection = null;
  let capabilities = {};
  let generation = 0;
  let selectionGeneration = 0;
  let selected = null;
  let review = null;
  let draft = null;
  let activeJob = null;
  let pollTimer = null;
  let sessionCursor = null;
  let usageCursor = null;
  let operationCursor = null;
  let confirmationPending = false;
  const readable = (value, fallback = "Unknown") => typeof value === "string" && value.length ? value : fallback;
  const notice = (message, error = false) => {
    byId("notice").textContent = message;
    byId("notice").classList.toggle("error", error);
    byId("notice").hidden = false;
  };
  const metadata = (value) => JSON.stringify(value, null, 2);
  const isConnected = () => connection !== null && Date.now() < Date.parse(connection.expires_at);
  const setEnabled = () => {
    const connected = isConnected();
    const busy = confirmationPending || (activeJob !== null && !terminalStates.has(activeJob.state));
    for (const id of ["source-profile", "target-profile", "target-project", "target-title", "title-policy", "refresh-sessions"])
      byId(id).disabled = !connected || busy;
    byId("transfer-mode").disabled = !connected || busy;
    const modes = { summary: capabilities.summary_available, return_new_full: capabilities.return_new_available, return_new_summary: capabilities.return_summary_available };
    for (const option of byId("transfer-mode").options) if (option.value !== "full") option.disabled = !modes[option.value];
    byId("handoff-options").hidden = byId("transfer-mode").value === "full";
    for (const id of ["handoff-model", "handoff-provider", "handoff-version", "handoff-first", "handoff-last", "handoff-parent", "load-handoff-parents"]) byId(id).disabled = !connected || busy;
    byId("accept-handoff").disabled = !connected || !draft || busy;
    byId("discard-handoff").disabled = !connected || busy;
    byId("prepare-transfer").disabled = !connected || !capabilities.publish_enabled || busy || selected === null || !byId("target-profile").value;
    byId("adoption-controls").hidden = !capabilities.adoption_available;
    byId("adopt-target-session").disabled = !connected || busy;
    byId("preview-adoption").disabled = !connected || busy || !byId("adopt-target-session").value;
    byId("prepare-adoption").disabled = !connected || !capabilities.publish_enabled || !capabilities.adoption_available || busy || selected === null || !byId("adopt-target-session").value;
    byId("refresh-usage").disabled = !connected || selected === null;
    byId("refresh-operations").disabled = !connected;
    byId("confirm-transfer").disabled = !connected || review === null || (activeJob !== null && !terminalStates.has(activeJob.state));
    byId("discard-review").disabled = confirmationPending;
    byId("logout").disabled = !connected;
    byId("cancel-job").disabled = !connected || !activeJob || terminalStates.has(activeJob.state) || ["barrier_pubclosed", "publication_closed", "service_commit_entered"].includes(activeJob.state);
  };
  const api = async (path, options = {}) => {
    const current = connection;
    const expectedGeneration = generation;
    if (!isConnected()) throw new Error("connection_unavailable");
    const method = options.method || "GET";
    const headers = { "Authorization": `Bearer ${current.access_token}`, "Accept": "application/json" };
    if (method !== "GET") {
      headers["Content-Type"] = "application/json";
      headers["X-CSRF-Token"] = current.csrf_nonce;
    }
    const response = await fetch(path, { method, headers, body: options.body === undefined ? undefined : JSON.stringify(options.body), credentials: "omit", cache: "no-store", redirect: "error" });
    if (expectedGeneration !== generation || current !== connection) throw new Error("connection_changed");
    const data = await response.json();
    if (expectedGeneration !== generation || current !== connection) throw new Error("connection_changed");
    if (!response.ok) throw new Error("request_failed");
    return data;
  };
  const clearReview = () => {
    if (confirmationPending) return;
    const oldDraft = draft;
    draft = null; byId("handoff-editor").hidden = true; byId("handoff-document").value = "";
    if (oldDraft && isConnected()) api(`/api/handoffs/${encodeURIComponent(oldDraft.draft_id)}/discard`, { method: "POST", body: {} }).catch(() => notice("Discard could not be confirmed; the handoff remains subject to its expiry.", true));
    const discarded = review;
    review = null;
    byId("proposal").hidden = true;
    byId("proposal-details").textContent = "";
    if (discarded?.proposal.preparation_job_id && isConnected()) api(`/api/jobs/${encodeURIComponent(discarded.proposal.preparation_job_id)}/cancel`, { method: "POST", body: {} }).catch(() => notice("The old review could not be discarded on the server; it remains subject to its expiry.", true));
    setEnabled();
  };
  const resetConnection = () => {
    generation += 1;
    selectionGeneration += 1;
    connection = null;
    capabilities = {};
    selected = null;
    draft = null; byId("handoff-editor").hidden = true; byId("handoff-document").value = "";
    review = null;
    activeJob = null;
    confirmationPending = false;
    clearTimeout(pollTimer);
    sessionCursor = usageCursor = operationCursor = null;
    byId("connection-state").textContent = "Disconnected";
    byId("connection-state").classList.remove("connected");
    for (const id of ["sessions", "usage-rows", "usage-context", "operations"]) byId(id).replaceChildren();
    for (const id of ["proposal", "job", "operation-details", "more-sessions", "more-usage", "more-operations"]) byId(id).hidden = true;
    for (const id of ["proposal-details", "job-details", "operation-details"]) byId(id).textContent = "";
    byId("selected-session").textContent = "No session selected";
    byId("usage-selection").textContent = "Select a source session first.";
    byId("preview").textContent = "Select a session to see its preview.";
    byId("target-title").value = "";
    byId("adoption-preview").textContent = ""; byId("adoption-preview").hidden = true;
    optionsFor(byId("adopt-target-session"), [], "Choose an existing session");
    for (const id of ["source-profile", "target-profile", "target-project"]) optionsFor(byId(id), [], "Connect to load profiles");
    setEnabled();
  };
  const optionsFor = (element, values, placeholder) => {
    const first = document.createElement("option"); first.value = ""; first.textContent = placeholder;
    element.replaceChildren(first);
    for (const item of values) {
      const option = document.createElement("option"); option.value = item.id;
      option.textContent = `${readable(item.name, "Unnamed profile")}${item.format ? ` · ${item.format}` : ""}`;
      element.append(option);
    }
  };
  const parseLaunchFile = (text) => {
    let offset = 0;
    const result = Object.create(null);
    const keys = new Set(["schema_version", "run_id", "origin", "expires_at", "access_token", "csrf_nonce"]);
    const skip = () => { while (/\s/u.test(text[offset] || "") && offset < text.length) offset += 1; };
    const symbol = (value) => { skip(); if (text[offset++] !== value) throw new Error("invalid_launch_file"); };
    const string = () => {
      skip(); const start = offset; symbol('"');
      while (offset < text.length) {
        const character = text[offset++];
        if (character === "\\") offset += 1;
        else if (character === '"') return JSON.parse(text.slice(start, offset));
      }
      throw new Error("invalid_launch_file");
    };
    symbol("{");
    while (true) {
      const key = string();
      if (!keys.has(key) || Object.hasOwn(result, key)) throw new Error("invalid_launch_file");
      symbol(":"); skip();
      if (key === "schema_version") { if (text[offset++] !== "1") throw new Error("invalid_launch_file"); result[key] = 1; }
      else result[key] = string();
      skip();
      if (text[offset] === "}") { offset += 1; break; }
      symbol(",");
    }
    skip(); if (offset !== text.length || Object.keys(result).length !== keys.size) throw new Error("invalid_launch_file");
    return result;
  };
  byId("launch-file").addEventListener("change", async (event) => {
    const file = event.target.files[0];
    event.target.value = "";
    if (!file) return;
    if (connection !== null) { notice("Disconnect the current run before connecting another.", true); return; }
    try {
      if (file.size > 4096) throw new Error("invalid_launch_file");
      const data = parseLaunchFile(await file.text());
      const keys = ["schema_version", "run_id", "origin", "expires_at", "access_token", "csrf_nonce"];
      if (data === null || Array.isArray(data) || Object.keys(data).sort().join() !== keys.sort().join() || data.schema_version !== 1) throw new Error("invalid_launch_file");
      if (keys.filter((key) => key !== "schema_version").some((key) => typeof data[key] !== "string" || !data[key] || data[key].length > 256)) throw new Error("invalid_launch_file");
      if (data.origin !== window.location.origin || !Number.isFinite(Date.parse(data.expires_at)) || Date.parse(data.expires_at) <= Date.now()) throw new Error("invalid_launch_file");
      connection = Object.freeze(data);
      generation += 1;
      const profiles = await api("/api/profiles");
      capabilities = await api("/api/capabilities");
      optionsFor(byId("source-profile"), profiles.profiles, "Choose source");
      optionsFor(byId("target-profile"), profiles.profiles, "Choose destination");
      optionsFor(byId("target-project"), profiles.projects || [], "Use session project");
      byId("connection-state").textContent = "Connected";
      byId("connection-state").classList.add("connected");
      notice("Connected. Choose a source profile to load sessions.");
      setEnabled();
    } catch { resetConnection(); notice("The launch file could not establish a connection. Check the selected run and its expiry.", true); }
  });
  byId("logout").addEventListener("click", async () => {
    let revoked = false;
    try { const result = await api("/api/auth/logout", { method: "POST", body: {} }); revoked = result.revoked === true; }
    catch { /* A lost response does not prove revocation. */ }
    resetConnection();
    notice(revoked ? "Disconnected. The server revoked this run; any publication already started continues to completion." : "Local connection cleared. Server revocation could not be confirmed.", !revoked);
  });
  for (const button of document.querySelectorAll("[data-view]")) button.addEventListener("click", () => {
    const titles = { migrate: ["Carry your context forward.", "Select a source, choose a destination, and review before transferring."], usage: ["Understand your session usage.", "Explore recorded counters and what is known about their origin."], operations: ["Follow your transfers.", "Inspect durable records, acknowledgements and continuity."] };
    for (const item of document.querySelectorAll("[data-view]")) { item.classList.toggle("active", item === button); if (item === button) item.setAttribute("aria-current", "page"); else item.removeAttribute("aria-current"); }
    for (const view of document.querySelectorAll(".view")) view.hidden = view.id !== `${button.dataset.view}-view`;
    [byId("view-title").textContent, byId("view-description").textContent] = titles[button.dataset.view];
  });
  const loadSessions = async (append = false) => {
    const profile = byId("source-profile").value;
    if (!profile) return;
    const expectedSelection = selectionGeneration;
    try {
      const params = new URLSearchParams({ profile_id: profile, limit: "50" });
      if (append && sessionCursor) params.set("cursor", sessionCursor);
      const data = await api(`/api/sessions?${params}`);
      if (expectedSelection !== selectionGeneration) return;
      if (!append) byId("sessions").replaceChildren();
      for (const session of data.sessions) {
        const li = document.createElement("li"), button = document.createElement("button"), title = document.createElement("strong"), detail = document.createElement("small");
        button.className = "session-button"; button.type = "button"; button.setAttribute("aria-pressed", String(selected?.id === session.id));
        title.textContent = readable(session.title, "Untitled session");
        detail.textContent = [session.format, session.updated_at].filter((value) => typeof value === "string").join(" · ");
        button.append(title, detail); button.addEventListener("click", () => selectSession(session, button)); li.append(button); byId("sessions").append(li);
      }
      if (!data.sessions.length && !append) { const li = document.createElement("li"); li.className = "empty-state"; li.textContent = "No sessions in this selected profile."; byId("sessions").append(li); }
      sessionCursor = data.next_cursor || null; byId("more-sessions").hidden = sessionCursor === null;
    } catch { notice("Sessions could not be loaded. Refresh after checking the current profile.", true); }
  };
  const selectSession = async (session, button) => {
    if (confirmationPending || (activeJob && !terminalStates.has(activeJob.state))) return;
    selected = session; selectionGeneration += 1; const expectedSelection = selectionGeneration; usageCursor = null;
    clearReview();
    for (const item of document.querySelectorAll(".session-button")) item.setAttribute("aria-pressed", String(item === button));
    byId("selected-session").textContent = readable(session.title, "Untitled session"); byId("usage-selection").textContent = readable(session.title, "Untitled session");
    byId("preview").textContent = "Loading preview…"; byId("usage-rows").replaceChildren(); byId("usage-context").replaceChildren(); byId("more-usage").hidden = true;
    setEnabled();
    try { const data = await api(`/api/sessions/${encodeURIComponent(session.id)}/preview`); if (expectedSelection !== selectionGeneration) return; byId("preview").textContent = readable(data.text, "No preview available."); byId("preview-note").textContent = data.truncated ? "This is a bounded preview. The full session is checked when preparing a transfer." : "Preview content is shown as literal text."; }
    catch { if (expectedSelection === selectionGeneration) byId("preview").textContent = "Preview unavailable. The source may need to be refreshed."; }
  };
  byId("source-profile").addEventListener("change", () => { selected = null; selectionGeneration += 1; clearReview(); loadSessions(); });
  const loadAdoptionSessions = async () => {
    const profile = byId("target-profile").value;
    optionsFor(byId("adopt-target-session"), [], "Choose an existing session");
    byId("adoption-preview").hidden = true; byId("adoption-preview").textContent = "";
    if (!profile || !capabilities.adoption_available) { setEnabled(); return; }
    try {
      const data = await api(`/api/sessions?${new URLSearchParams({ profile_id: profile, limit: "100" })}`);
      if (profile !== byId("target-profile").value) return;
      optionsFor(byId("adopt-target-session"), data.sessions.map((item) => ({ id: item.id, name: readable(item.title, "Untitled session"), format: item.format })), "Choose an existing session");
      setEnabled();
    } catch { notice("Destination sessions could not be loaded. Refresh the catalog through the CLI if needed.", true); }
  };
  byId("target-profile").addEventListener("change", () => { clearReview(); loadAdoptionSessions(); });
  byId("adopt-target-session").addEventListener("change", () => { clearReview(); byId("adoption-preview").hidden = true; setEnabled(); });
  byId("preview-adoption").addEventListener("click", async () => {
    const session = byId("adopt-target-session").value;
    try {
      const data = await api(`/api/sessions/${encodeURIComponent(session)}/preview`);
      if (session !== byId("adopt-target-session").value) return;
      byId("adoption-preview").textContent = readable(data.text, "No preview available."); byId("adoption-preview").hidden = false;
    } catch { notice("Destination preview could not be loaded.", true); }
  });
  for (const id of ["target-project", "target-title", "title-policy"]) byId(id).addEventListener("change", clearReview);
  byId("refresh-sessions").addEventListener("click", () => loadSessions());
  byId("more-sessions").addEventListener("click", () => loadSessions(true));
  byId("discard-review").addEventListener("click", clearReview);
  const showJob = (job) => {
    activeJob = job;
    if (terminalStates.has(job.state)) confirmationPending = false;
    byId("job").hidden = false;
    byId("job-state").textContent = job.state === "service_commit_entered" ? "Service commit entered; effects are possible. Keep observing." : readable(job.state, "Status unavailable").replaceAll("_", " ");
    byId("job-details").textContent = metadata({ operation_id: job.operation_id, subject: job.subject, effects: job.effects, result: job.result });
    setEnabled();
  };
  const pollJob = async (jobId) => {
    const expectedGeneration = generation;
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
      if (expectedGeneration !== generation) return;
      showJob(job);
      if (job.state === "editable" && job.result?.draft_id) {
        const document = await api(`/api/handoffs/${encodeURIComponent(job.result.draft_id)}`);
        if (expectedGeneration !== generation) return;
        draft = document; byId("handoff-document").value = document.document;
        byId("handoff-editor").hidden = false; activeJob = null; byId("job").hidden = true; setEnabled(); return;
      }
      if (job.state === "reviewable" && job.result?.proposal_id) {
        const proposal = await api(`/api/proposals/${encodeURIComponent(job.result.proposal_id)}`);
        if (expectedGeneration !== generation) return;
        const action = proposal.native_action || proposal.service_action || "transfer";
        review = { proposal, key: crypto.randomUUID(), action };
        byId("proposal-heading").textContent = action === "transfer" ? "Review this transfer" : `Review ${action.replaceAll("_", " ")}`;
        byId("confirm-transfer").textContent = action === "transfer" ? "Confirm transfer" : "Confirm reviewed action";
        byId("proposal-details").textContent = metadata(proposal.metadata);
        byId("proposal-expiry").textContent = `Review expires: ${readable(proposal.expires_at)}`;
        byId("proposal").hidden = false;
        activeJob = null;
        byId("job").hidden = true;
        setEnabled();
        return;
      }
      if (!terminalStates.has(job.state)) pollTimer = setTimeout(() => pollJob(jobId), 1200);
      else if (job.state === "completed" || job.state === "recovered") { review = null; byId("proposal").hidden = true; setEnabled(); notice(job.service_action ? "Service result confirmed. Inspect its subject and durable evidence." : "Transfer completed. The durable record is available in Operations."); }
      else if (job.state === "uncertain") notice("The result needs inspection by its operation ID. Do not repeat the import.", true);
    } catch {
      if (expectedGeneration !== generation) return;
      notice("Status could not be refreshed. No success or cancellation has been confirmed.", true);
      byId("job-state").textContent = "Status unavailable";
    }
  };
  byId("transfer-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (confirmationPending || !selected || !byId("target-profile").value || (activeJob && !terminalStates.has(activeJob.state))) return;
    const source = selected, expectedSelection = selectionGeneration;
    clearReview(); byId("prepare-transfer").disabled = true;
    const request = { source_session_id: source.id, target_profile_id: byId("target-profile").value, project_id: byId("target-project").value || null, target_title: byId("target-title").value || null, title_policy: byId("target-title").value ? byId("title-policy").value : null };
    try {
      const mode = byId("transfer-mode").value;
      let route = "/api/proposals";
      if (mode !== "full") {
        if (!request.project_id) throw new Error("project_required");
        Object.assign(request, { model: byId("handoff-model").value, model_provider: byId("handoff-provider").value, target_cli_version: byId("handoff-version").value });
        if (mode !== "summary") request.operation_id = byId("handoff-parent").value;
        if (mode !== "return_new_full") request.selection = [{ first_event: Number(byId("handoff-first").value), last_event: Number(byId("handoff-last").value), block_index: null }];
        route = { summary: "/api/handoffs/capture", return_new_summary: "/api/return-new/capture", return_new_full: "/api/return-new/prepare" }[mode];
      }
      const job = await api(route, { method: "POST", body: request });
      if (expectedSelection !== selectionGeneration) return;
      showJob(job); pollJob(job.job_id);
    } catch { notice("A transfer review could not be prepared. Refresh the source and check the destination.", true); setEnabled(); }
  });
  const prepareService = async (action, request) => {
    if (confirmationPending || (activeJob && !terminalStates.has(activeJob.state))) return;
    clearReview();
    try { const job = await api(`/api/${action}/prepare`, { method: "POST", body: request }); showJob(job); pollJob(job.job_id); }
    catch { notice("This action could not be prepared for the current run and resources.", true); setEnabled(); }
  };
  byId("prepare-adoption").addEventListener("click", () => {
    if (selected && byId("adopt-target-session").value) prepareService("adopt", { source_session_id: selected.id, target_session_id: byId("adopt-target-session").value });
  });
  byId("confirm-transfer").addEventListener("click", async () => {
    const currentReview = review;
    if (!currentReview || (activeJob && !terminalStates.has(activeJob.state))) return;
    confirmationPending = true;
    setEnabled();
    byId("confirm-transfer").disabled = true;
    try {
      const resource = { transfer: "proposals", summary: "handoffs", return_new_summary: "return-new-summary", return_new_full: "return-new-full" }[currentReview.action] || currentReview.action.replaceAll("_", "-");
      const job = await api(`/api/${resource}/${encodeURIComponent(currentReview.proposal.proposal_id)}/confirm`, { method: "POST", body: { body_fingerprint: currentReview.proposal.fingerprint, idempotency_key: currentReview.key } });
      if (review !== currentReview) return;
      showJob(job); pollJob(job.job_id);
    } catch { notice("Confirmation could not be observed. Reusing this review checks the same confirmation; it does not request another import.", true); setEnabled(); }
  });
  for (const id of ["transfer-mode", "handoff-model", "handoff-provider", "handoff-version", "handoff-first", "handoff-last", "handoff-parent"]) byId(id).addEventListener("input", clearReview);
  byId("handoff-document").addEventListener("input", () => { review = null; byId("proposal").hidden = true; });
  byId("load-handoff-parents").addEventListener("click", async () => {
    try {
      const data = await api("/api/lineage?limit=100");
      optionsFor(byId("handoff-parent"), data.operations.filter((item) => item.state === "COMPLETED" || item.state === "completed").map((item) => ({ id: item.id, name: item.operation_id })), "Choose a completed transfer");
    } catch { notice("Parent transfers could not be loaded.", true); }
  });
  byId("discard-handoff").addEventListener("click", clearReview);
  byId("accept-handoff").addEventListener("click", async () => {
    const current = draft, expectedSelection = selectionGeneration, expectedGeneration = generation;
    if (!current || confirmationPending) return;
    const document = byId("handoff-document").value;
    byId("accept-handoff").disabled = true;
    try {
      const bytes = new TextEncoder().encode(document);
      if (!bytes.length || bytes.length > 60000) throw new Error("document_limit");
      const hash = await crypto.subtle.digest("SHA-256", bytes);
      const document_sha256 = Array.from(new Uint8Array(hash), (value) => value.toString(16).padStart(2, "0")).join("");
      if (draft !== current || expectedSelection !== selectionGeneration || expectedGeneration !== generation || document !== byId("handoff-document").value) throw new Error("draft_changed");
      const job = await api(`/api/handoffs/${encodeURIComponent(current.draft_id)}/accept`, { method: "POST", body: { document, document_sha256 } });
      if (draft !== current || expectedSelection !== selectionGeneration) return;
      draft = null; byId("handoff-document").value = ""; byId("handoff-editor").hidden = true; showJob(job); pollJob(job.job_id);
    } catch { notice("The handoff review could not be observed. Keep the exact text to check this same acceptance again, or capture a new handoff.", true); setEnabled(); }
  });
  byId("cancel-job").addEventListener("click", async () => {
    if (!activeJob) return;
    const id = activeJob.job_id;
    try { const job = await api(`/api/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST", body: {} }); showJob(job); pollJob(id); }
    catch { notice("Cancellation could not be confirmed. Inspect the operation before another action.", true); }
  });
  const counterText = (counter) => {
    if (!counter || !Number.isSafeInteger(counter.value) || counter.value < 0) return "Unknown";
    const value = counter.value.toLocaleString("en-US");
    if (counter.quality === "recorded") return value;
    if (counter.quality === "synthetic") return `${value} · synthetic`;
    if (counter.quality === "heuristic") return `~${value} · estimate`;
    return "Unknown";
  };
  const metric = (title, value, detail) => {
    const card = document.createElement("div"), heading = document.createElement("h3"), number = document.createElement("strong"), note = document.createElement("p");
    card.className = "metric"; heading.textContent = title; number.textContent = value; note.textContent = detail; card.append(heading, number, note); return card;
  };
  const loadUsage = async (append = false) => {
    if (!selected) return;
    const expectedSelection = selectionGeneration, sourceId = selected.id;
    try {
      const params = new URLSearchParams({ limit: "100" }); if (append && usageCursor) params.set("cursor", usageCursor);
      const data = await api(`/api/sessions/${encodeURIComponent(sourceId)}/usage?${params}`);
      if (expectedSelection !== selectionGeneration) return;
      if (!append) {
        byId("usage-rows").replaceChildren();
        byId("usage-context").replaceChildren(metric("Context window", "Unknown", "A reliable window is required for a percentage."), metric("Estimated cost", "Unknown", "Recorded usage does not establish billing."), metric("Coverage", readable(data.coverage), "Imported, estimated and unproven counters are distinguished."));
      }
      for (const sample of data.samples_page || []) {
        const row = document.createElement("tr");
        const counters = Array.isArray(sample.counters) ? Object.fromEntries(sample.counters) : sample.counters;
        const values = [readable(sample.scope), readable(sample.model), counterText(counters?.input), counterText(counters?.output), counterText(counters?.cache_read), counterText(counters?.reasoning), readable(sample.origin)];
        for (const value of values) { const cell = document.createElement("td"); cell.textContent = value; row.append(cell); }
        byId("usage-rows").append(row);
      }
      if (!byId("usage-rows").children.length) { const row = document.createElement("tr"), cell = document.createElement("td"); cell.colSpan = 7; cell.className = "empty-state"; cell.textContent = "No proved counter observations are available for this capture."; row.append(cell); byId("usage-rows").append(row); }
      usageCursor = data.next_cursor || null; byId("more-usage").hidden = usageCursor === null;
      byId("usage-note").textContent = "These are observations from this capture. Missing or unsafe-to-display counters are unknown; imported counters are not new consumption. Refresh if the capture changes.";
    } catch { notice("Usage could not be loaded for the current capture. Refresh rather than combining revisions.", true); }
  };
  byId("refresh-usage").addEventListener("click", () => loadUsage());
  byId("more-usage").addEventListener("click", () => loadUsage(true));
  const loadOperations = async (append = false) => {
    const expectedGeneration = generation;
    try {
      capabilities = await api("/api/capabilities");
      if (expectedGeneration !== generation) return;
      const params = new URLSearchParams({ limit: "50" }); if (append && operationCursor) params.set("cursor", operationCursor);
      const data = await api(`/api/lineage?${params}`);
      if (expectedGeneration !== generation) return;
      if (!append) byId("operations").replaceChildren();
      for (const operation of data.operations) {
        const li = document.createElement("li"), button = document.createElement("button");
        button.type = "button"; button.className = "operation-button"; button.textContent = `${readable(operation.operation_id)} · ${readable(operation.state)}`;
        button.addEventListener("click", async () => {
          try { const detail = await api(`/api/lineage/${encodeURIComponent(operation.id)}`); byId("operation-details").textContent = metadata(detail); byId("operation-details").hidden = false; }
          catch { notice("This operation could not be inspected in the current run.", true); }
        });
        li.append(button);
        if (capabilities.publish_enabled && capabilities.cancel_ready_available && operation.state === "ready") {
          const cancel = document.createElement("button"); cancel.type = "button"; cancel.className = "quiet-button"; cancel.textContent = "Review cancellation of READY";
          cancel.addEventListener("click", () => { document.querySelector('[data-view="migrate"]').click(); prepareService("cancel-ready", { operation_id: operation.id }); }); li.append(cancel);
        }
        if (capabilities.publish_enabled && capabilities.reconcile_available && operation.recovery_context_id && operation.state === "needs_reconcile") {
          const reconcile = document.createElement("button"); reconcile.type = "button"; reconcile.className = "quiet-button"; reconcile.textContent = "Review reconciliation";
          reconcile.addEventListener("click", () => { document.querySelector('[data-view="migrate"]').click(); prepareService("reconcile", { operation_id: operation.id, recovery_context_id: operation.recovery_context_id }); }); li.append(reconcile);
        }
        if (operation.state === "needs_reconcile" && !operation.recovery_context_id) { const reason = document.createElement("small"); reason.textContent = "Original context is unavailable in this run; use the CLI with the original files and options."; li.append(reason); }
        byId("operations").append(li);
      }
      if (!data.operations.length && !append) { const li = document.createElement("li"); li.className = "empty-state"; li.textContent = "No durable operations found in the selected state directory."; byId("operations").append(li); }
      operationCursor = data.next_cursor || null; byId("more-operations").hidden = operationCursor === null;
    } catch { notice("Operations could not be inspected. No ledger was created by this read.", true); }
  };
  byId("refresh-operations").addEventListener("click", () => loadOperations());
  byId("more-operations").addEventListener("click", () => loadOperations(true));
  setEnabled();
})();
