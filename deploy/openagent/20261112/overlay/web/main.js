"use strict";

const byId = (id) => document.getElementById(id);
const select = byId("profile");
const submit = byId("submit");
const message = byId("message");
const result = byId("result");
const taskInput = byId("task-id");
const labels = new Map();
let pendingRegistration;
let pendingOpinion;
let navigation = 0;
let publicProfile;
let activeTask;
let editorRevision;
let pendingEdit;
const sourceRoles = ["body", "table", "reference"];
const revisionStates = {
  received: "原件已读取，待生成",
  drafting: "Codex 正在定位改稿",
  reviewing: "候选已通过程序检查，待采用",
  ready_for_review: "可审阅 · 待业务核稿",
  needs_input: "需确认 · 父版已保留",
  failed: "改稿未完成 · 父版已保留",
};

function announce(text, error = false) {
  message.textContent = text;
  message.dataset.error = String(error);
}

async function api(url, options) {
  const response = await fetch(url.startsWith("/") ? url.slice(1) : url, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body.summary ?? "服务暂不可用，请稍后再试。");
    error.code = body.error?.code;
    throw error;
  }
  return body;
}

function textNode(tag, value) {
  const node = document.createElement(tag);
  node.textContent = value;
  return node;
}

function showChanges(id, changes) {
  byId(id).replaceChildren(...changes.map((change) => {
    const section = document.createElement("section");
    section.append(textNode("h4", change.location),
                   textNode("p", `改前 · ${change.before}`),
                   textNode("p", `改后 · ${change.after}`));
    return section;
  }));
}

function loadEditParagraph() {
  const index = Number(byId("manual-paragraph").value);
  byId("manual-text").value = activeTask.revision_work.current.view.body[index] ?? "";
  byId("manual-message").textContent = "";
}

function showCurrent(task, adopted) {
  const current = task.revision_work.current;
  const manual = current?.manual;
  const view = current?.view;
  byId("current-preview").hidden = !view || adopted;
  byId("manual-editor").hidden = !view?.editable_indices.length;
  if (view) {
    byId("current-body").replaceChildren(...view.body.map((value) => textNode("p", value)));
    byId("current-locked-text").replaceChildren(...view.lock_indices.map((i) => textNode("p", view.body[i])));
    byId("current-lock").hidden = !view.lock_indices.length;
    byId("current-table").hidden = !view.table_row.length;
    byId("current-table-head").replaceChildren(...view.table_header.map((value) => textNode("th", value)));
    byId("current-table-row").replaceChildren(...view.table_row.map((value) => textNode("td", value)));
    byId("manual-changes").hidden = !manual;
    showChanges("manual-change-list", manual ? view.changes : []);
    if (editorRevision !== task.current_revision) {
      editorRevision = task.current_revision;
      byId("manual-paragraph").replaceChildren(...view.editable_indices.map((i) =>
        new Option(`第 ${i + 1} 段 · ${view.body[i].slice(0, 35)}`, i)));
      loadEditParagraph();
    }
  }
  byId("downloads").hidden = !manual && !adopted;
  byId("download-word").href = `api/tasks/${task.task_id}/revisions/${task.current_revision}/word${manual ? "?allow_draft=true" : ""}`;
  byId("download-reference").href = `api/tasks/${task.task_id}/reference`;
  byId("export-receipt").textContent = manual ? "下载当前手改稿，待核稿。" : "Word 按当前稿版本下载。";
}

function showRevision(task) {
  activeTask = task;
  const work = task.revision_work;
  byId("revision").hidden = !work;
  const runningOnCurrent = work?.run?.state === "running" && work.run.base_revision === task.current_revision;
  byId("task-state").textContent = runningOnCurrent ? revisionStates.drafting
    : work?.current?.manual ? "手改稿 · 待核稿"
    : work ? revisionStates[task.task_state] : "已登记，后续处理待接入";
  if (!work) return;
  byId("saved-sources").replaceChildren(...work.sources.map((source) => textNode("li", source.name)));
  byId("current-revision").textContent = task.current_revision;
  const error = work.run?.error;
  byId("revision-error").textContent = error ? `${error.code} · ${error.message}` : "";
  byId("generate").hidden = !work.can_generate || task.profile_id !== publicProfile;
  byId("generate").textContent = work.current?.manual ? "从当前稿重新生成候选" : "生成改稿候选";
  const candidate = work.candidate;
  const adopted = candidate?.revision_id === task.current_revision;
  showCurrent(task, adopted);
  byId("candidate").hidden = !candidate;
  if (!candidate) return;
  const { revision_id, parent_revision, view } = candidate;
  const stale = !adopted && parent_revision !== task.current_revision;
  byId("candidate-label").textContent = adopted ? "当前稿" : "候选稿";
  byId("candidate-id").textContent = revision_id;
  byId("parent-id").textContent = parent_revision;
  byId("candidate-conflict").hidden = !stale;
  byId("date-basis").textContent = `日期依据 · ${view.date.decision_source} · ${view.date.before} → ${view.date.after}`;
  showChanges("change-list", view.changes);
  byId("locked-text").replaceChildren(...view.lock_indices.map((index) => textNode("p", view.body[index])));
  byId("body-preview").replaceChildren(...view.body.map((text) => textNode("p", text)));
  byId("table-head").replaceChildren(...view.table_header.map((text) => textNode("th", text)));
  byId("table-row").replaceChildren(...view.table_row.map((text) => textNode("td", text)));
  byId("commit").hidden = adopted || stale;
}

function showOpinion(task) {
  const review = task.opinion_review;
  byId("opinion-result").hidden = !review;
  if (!review) return;
  const state = {running: "处理中 · 可用任务 ID 重开", failed: "本轮未交件",
    revision: "可审阅 · 待业务核稿", review_only: "已核对 · 原稿未改",
    held: "父版保留 · 待业务核稿", triage: "已归档意见 · 待补原稿"};
  byId("task-state").textContent = state[review.state];
  byId("opinion-state").textContent = review.error
    ? `${review.error.code} · ${review.error.message}`
    : review.native
      ? `${review.native.message ?? (review.native.applied ? `正文已改，已接受 ${review.native.applied} 处正文文字修订。` : "正文未改。")}批注定位 ${review.native.comments_located}/${review.native.comments} 条；保留其他标记 ${review.native.body_pending_marks + review.native.outside_body_marks} 处。${review.native.template_note ?? ""}`
      : review.files ? `子项 ${review.children} 条` : "稍后重开任务查看结果。";
  byId("opinion-downloads").replaceChildren(...(review.files ?? []).map((name) => {
    const link = textNode("a", name);
    link.href = `api/tasks/${task.task_id}/opinion/${encodeURIComponent(name)}`;
    const item = document.createElement("li");
    item.append(link);
    return item;
  }));
}

function showFormal(task) {
  const formal = task.formal_notice;
  byId("formal-result").hidden = !formal;
  if (!formal) return;
  byId("task-state").textContent = formal.files.length ? "已套版 · 待签发核稿" : "套版文件未交付";
  byId("formal-state").textContent = formal.files.length
    ? "请核对文号与成文日期。用印、版记和实际附件由经办人按单位签发流程补齐。"
    : "请用原样张和原材料重试。";
  byId("formal-downloads").replaceChildren(...formal.files.map((name) => {
    const link = textNode("a", name);
    link.href = `api/tasks/${task.task_id}/formal/${encodeURIComponent(name)}`;
    const item = document.createElement("li");
    item.append(link);
    return item;
  }));
}

function showDraft(task) {
  const draft = task.natural_draft;
  byId("draft-result").hidden = !draft;
  if (!draft) return;
  byId("task-state").textContent = {
    running: "等待起草结果", draft: "初稿已生成 · 待核稿", failed: "起草未完成",
    needs_input: "待补充材料", needs_review: "未通过成稿检查", revision_identified: "已识别改稿需求",
  }[draft.state];
  byId("draft-state").textContent = draft.elapsed_seconds == null
    ? draft.message : `${draft.message} 服务处理耗时 ${draft.elapsed_seconds} 秒。`;
  byId("draft-downloads").replaceChildren(...draft.files.map((name) => {
    const link = textNode("a", name);
    link.href = `api/tasks/${task.task_id}/draft/${encodeURIComponent(name)}`;
    const item = document.createElement("li");
    item.append(link);
    return item;
  }));
}

async function openTask(id, pushUrl = true) {
  const currentNavigation = ++navigation;
  result.hidden = true;
  announce("正在读取任务…");
  if (pushUrl) {
    const url = new URL(window.location.href);
    url.searchParams.set("task", id);
    if (url.href !== window.location.href) history.pushState(null, "", url);
  }
  taskInput.value = id;
  try {
    const { data: { task } } = await api(`/api/tasks/${encodeURIComponent(id)}`);
    if (navigation !== currentNavigation) return;
    byId("saved-id").textContent = task.task_id;
    byId("saved-profile").textContent = labels.get(task.profile_id) ?? task.profile_id;
    byId("saved-request").textContent = task.original_request;
    showRevision(task);
    showOpinion(task);
    showFormal(task);
    showDraft(task);
    result.hidden = false;
    announce("已读取任务记录。");
    if (task.natural_draft?.state === "running") {
      setTimeout(() => {
        if (navigation === currentNavigation) openTask(id, false);
      }, 3000);
    }
  } catch (error) {
    if (navigation === currentNavigation) announce(error.message, true);
  }
}

function restoreUrl() {
  const taskId = new URL(window.location.href).searchParams.get("task");
  if (taskId) return openTask(taskId, false);
  ++navigation;
  activeTask = undefined;
  result.hidden = true;
  taskInput.value = "";
  announce("");
}

function readFile(file, role) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("读取附件失败，请重新选择。"));
    reader.onload = () => resolve({
      role, name: file.name, content_base64: reader.result.split(",")[1],
    });
    reader.readAsDataURL(file);
  });
}

let pendingDraft;
byId("draft-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = byId("draft-submit");
  button.disabled = true;
  const mode = byId("draft-mode").value;
  announce(mode === "team" ? "专家团正在整理材料、起草并核稿，可能需要几分钟…" : "正在读取材料并起草…");
  try {
    const file = byId("draft-file").files[0];
    if (file?.size > 100_000) throw new Error("TXT 附件须在 100 KB 以内。");
    const payload = {original_request: byId("draft-request").value, public_trial: byId("draft-consent").checked, expert_mode: mode,
      attachment: file ? await readFile(file) : null};
    const identity = JSON.stringify(payload);
    if (pendingDraft?.identity !== identity) {
      pendingDraft = {identity, body: {...payload, request_id: crypto.randomUUID()}};
    }
    const {task_id, state} = await api("/api/natural-drafts", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify(pendingDraft.body)});
    if (state !== "running") pendingDraft = undefined;
    await openTask(task_id);
  } catch (error) {
    announce(error.message, true);
  } finally {
    button.disabled = false;
  }
});

function updateOpinionMode() {
  const native = byId("opinion-mode").value === "native";
  byId("opinion-files").multiple = !native;
  byId("opinion-files").accept = native ? ".docx" : ".docx,.txt,.md,.csv";
  byId("opinion-file-label").textContent = native ? "一份带原生审阅标记的 Word" : "待改 Word、意见及答复表、参考依据";
  byId("opinion-request").placeholder = native ? "只核对批注，不改正文。也可填写：请接受全部正文文字修订，批注保留。"
    : "在这份手改稿上按会签答复改，序言和责任单位锁定。";
  byId("opinion-consent-label").textContent = native ? "材料公开或已获授权，可在本机处理。"
    : "材料公开或已获授权，允许由本机 Codex 处理。";
}

byId("opinion-mode").addEventListener("change", updateOpinionMode);
byId("opinion-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = byId("opinion-submit");
  const files = Array.from(byId("opinion-files").files);
  const template = byId("opinion-template").files[0];
  const native = byId("opinion-mode").value === "native";
  button.disabled = true;
  announce(native ? "正在定位 Word 原生批注和修订…" : "正在逐组拆分意见并核对 Word…");
  try {
    if (native && (files.length !== 1 || !files[0].name.toLowerCase().endsWith(".docx"))) {
      throw new Error("Word 原生审阅需选择一份 DOCX 原稿。");
    }
    if (!files.length || files.length > 8 || [...files, ...(template ? [template] : [])].some((file) => file.size > 1_500_000)) {
      throw new Error("请选择 1 至 8 份文件，每份不超过 1.5 MB。");
    }
    const payload = {original_request: byId("opinion-request").value,
      public_trial: byId("opinion-consent").checked, native_review: native,
      template: template ? await readFile(template) : null, files: await Promise.all(files.map((file) => readFile(file)))};
    const identity = JSON.stringify(payload);
    if (pendingOpinion?.identity !== identity) {
      pendingOpinion = {identity, body: {...payload, request_id: crypto.randomUUID()}};
    }
    const {task_id, state} = await api("/api/opinion-tasks", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify(pendingOpinion.body)});
    if (state !== "running") pendingOpinion = undefined;
    await openTask(task_id);
  } catch (error) {
    announce(error.message, true);
  } finally {
    button.disabled = false;
  }
});

let pendingFormal;
byId("formal-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = byId("formal-submit");
  button.disabled = true;
  announce("正在套版并核对正文…");
  try {
    const {name, content_base64} = await readFile(byId("formal-template").files[0], "template");
    const fields = ["title", "number", "addressee", "body", "attachments", "issuer", "date"];
    const payload = Object.fromEntries(fields.map((key) => [key, byId(`formal-${key}`).value]));
    payload.template = {name, content_base64};
    payload.public_trial = byId("formal-consent").checked;
    const identity = JSON.stringify(payload);
    if (pendingFormal?.identity !== identity) pendingFormal = {identity, requestId: crypto.randomUUID()};
    const {task_id} = await api("/api/formal-tasks", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({...payload, request_id: pendingFormal.requestId})});
    pendingFormal = undefined;
    await openTask(task_id);
    if (!result.hidden) result.scrollIntoView();
  } catch (error) {
    announce(error.message, true);
  } finally {
    button.disabled = false;
  }
});

byId("register").addEventListener("submit", async (event) => {
  event.preventDefault();
  const profile_id = select.value;
  const original_request = byId("request").value;
  if (!original_request.trim()) {
    announce("请填写写作或改稿需求。", true);
    byId("request").focus();
    return;
  }
  submit.disabled = true;
  announce("正在登记任务…");
  try {
    let inputs = [];
    if (profile_id === publicProfile) {
      const files = sourceRoles.map((role) => byId(`${role}-file`).files[0]);
      if (files.some(Boolean)) {
        if (files.some((file) => !file) || !byId("public-consent").checked) {
          throw new Error("请选齐三个公开文件并确认 Codex 处理范围。");
        }
        if (files.some((file) => file.size > 1_500_000)) throw new Error("试跑文件须小于 1.5 MB。");
        inputs = await Promise.all(files.map((file, i) => readFile(file, sourceRoles[i])));
      }
    }
    const payload = {profile_id, original_request, inputs, public_trial: Boolean(inputs.length)};
    const identity = JSON.stringify(payload);
    if (pendingRegistration?.identity !== identity) {
      pendingRegistration = {identity, body: {...payload, request_id: crypto.randomUUID()}};
    }
    const { task_id } = await api("/api/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(pendingRegistration.body),
    });
    pendingRegistration = undefined;
    await openTask(task_id);
  } catch (error) {
    announce(error.message, true);
  } finally {
    submit.disabled = false;
  }
});

async function revisionAction(action, button) {
  const task = activeTask;
  const currentNavigation = navigation;
  const body = {
    request_id: crypto.randomUUID(), expected_revision: task.current_revision,
    source_set_version: task.source_set_version,
  };
  if (action === "commit_candidate") {
    const candidate = task.revision_work.candidate;
    body.candidate_id = candidate.revision_id;
    body.review_id = candidate.review_id;
    body.basis_key = task.basis_key;
  }
  button.disabled = true;
  announce(action === "generate_candidate" ? "Codex 正在定位改动，可刷新后查看本轮记录…" : "正在采用候选…");
  try {
    const result = await api(`/api/tasks/${task.task_id}/${action}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (currentNavigation === navigation) {
      if (activeTask.current_revision === task.current_revision ||
          result.data.task.current_revision === activeTask.current_revision) showRevision(result.data.task);
      else await openTask(task.task_id, false);
      announce(result.summary);
    }
  } catch (error) {
    if (currentNavigation === navigation) {
      await openTask(task.task_id, false);
      announce(error.message, true);
    }
  } finally {
    button.disabled = false;
  }
}

byId("generate").addEventListener("click", (event) => revisionAction("generate_candidate", event.currentTarget));
byId("commit").addEventListener("click", (event) => revisionAction("commit_candidate", event.currentTarget));
byId("manual-paragraph").addEventListener("change", loadEditParagraph);
byId("manual-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const task = activeTask;
  const currentNavigation = navigation;
  const selectParagraph = byId("manual-paragraph");
  const field = byId("manual-text");
  const button = byId("save-edit");
  const status = byId("manual-message");
  const payload = {expected_revision: task.current_revision,
                   block_edits: [{paragraph: Number(selectParagraph.value) + 1, text: field.value}]};
  const identity = JSON.stringify([task.task_id, payload]);
  if (pendingEdit?.identity !== identity) {
    pendingEdit = {identity, body: {...payload, request_id: crypto.randomUUID()}};
  }
  selectParagraph.disabled = button.disabled = true;
  field.readOnly = true;
  status.textContent = "正在保存手改…";
  try {
    const saved = await api(`/api/tasks/${task.task_id}/apply_user_edit`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(pendingEdit.body),
    });
    pendingEdit = undefined;
    if (navigation === currentNavigation) {
      showRevision(saved.data.task);
      status.textContent = saved.summary;
    }
  } catch (error) {
    if (navigation === currentNavigation) {
      status.textContent = error.code === "REVISION_CONFLICT"
        ? "当前稿已更新。请先复制未保存的文字，重开任务后再核对并编辑。" : error.message;
    }
  } finally {
    selectParagraph.disabled = button.disabled = false;
    field.readOnly = false;
  }
});
byId("download-word").addEventListener("click", async (event) => {
  event.preventDefault();
  const link = event.currentTarget;
  const currentNavigation = navigation;
  try {
    const response = await fetch(link.href);
    if (!response.ok) {
      const result = await response.json();
      throw new Error(result.summary);
    }
    const revision = response.headers.get("X-Revision-Id");
    const quality = response.headers.get("X-Quality-Label");
    const template = response.headers.get("X-Template-Version");
    const hash = response.headers.get("X-Content-Sha256");
    const blob = await response.blob();
    if (currentNavigation !== navigation) return;
    if (revision !== activeTask.current_revision) throw new Error("当前稿已变化，请重新下载。");
    const url = URL.createObjectURL(blob);
    const file = document.createElement("a");
    file.href = url;
    file.download = `work-plan-${revision}.docx`;
    file.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    byId("export-receipt").textContent = `${quality === "unreviewed" ? "待核稿 · " : ""}版本 ${revision} · 模板 ${template} · SHA-256 ${hash}`;
  } catch (error) {
    if (currentNavigation === navigation) announce(error.message, true);
  }
});

byId("lookup").addEventListener("submit", (event) => {
  event.preventDefault();
  const id = taskInput.value.trim();
  if (id) openTask(id);
});

async function initialize() {
  try {
    const capabilities = await api("/api/capabilities");
    publicProfile = capabilities.public_revision_profile;
    byId("opinion-intake").hidden = !capabilities.opinion_review;
    byId("opinion-mode").querySelector('[value="external"]').disabled = !capabilities.opinion_external;
    updateOpinionMode();
    labels.set("opinion-review", "按意见逐条核稿");
    byId("formal-intake").hidden = !capabilities.formal_notice;
    labels.set("formal-notice", "通知红头正文套版");
    byId("draft-intake").hidden = !capabilities.natural_draft;
    labels.set("natural-draft", "自然收件起草");
    byId("capability-note").textContent = publicProfile
      ? "四条路径可登记和重开。工作方案改稿已开放南阳公开材料试跑，支持同版 Word。"
      : "文本任务登记与重开。启动公开试跑后可处理指定工作方案。";
    select.addEventListener("change", () => {
      byId("public-inputs").hidden = select.value !== publicProfile;
    });
    for (const profile of capabilities.profiles) {
      labels.set(profile.id, profile.label);
      select.add(new Option(profile.label, profile.id));
    }
    select.disabled = false;
    submit.disabled = false;
    window.addEventListener("popstate", restoreUrl);
    await restoreUrl();
  } catch (error) {
    announce(error.message, true);
  }
}
initialize();
