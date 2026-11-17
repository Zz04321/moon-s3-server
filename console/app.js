"use strict";

const element = (id) => document.getElementById(id);
const state = { csrf: "", bucket: "", page: null, offset: 0, generation: 0, busy: false };
let modalSubmit = null;

function node(tag, text = "", className = "") {
  const result = document.createElement(tag);
  result.textContent = text;
  if (className) result.className = className;
  return result;
}

function notice(message, error = false) {
  element("notice").textContent = message;
  element("notice").className = error ? "notice error" : "notice";
  element("notice").hidden = !message;
}

function signedOut() {
  state.csrf = "";
  state.bucket = "";
  state.page = null;
  modalSubmit = null;
  state.generation += 1;
  element("modal").close();
  element("modal-body").replaceChildren();
  element("workspace").hidden = true;
  element("login-view").hidden = false;
  element("buckets").replaceChildren();
  element("objects").replaceChildren();
  element("password").value = "";
  notice("");
}

async function api(path, options = {}) {
  const headers = { ...options.headers };
  if (options.method === "POST") headers["X-Console-CSRF"] = state.csrf;
  const response = await fetch(path, { ...options, headers, credentials: "same-origin", redirect: "error" });
  const result = await response.json();
  if (!response.ok || !result.ok) {
    if (response.status === 401 && path !== "/api/login") {
      signedOut();
      element("login-error").textContent = "Your session expired. Sign in again to continue.";
    }
    const error = new Error(result.error || "The request could not be completed.");
    error.code = result.code;
    throw error;
  }
  return result;
}

function action(command, operation, fields = {}) {
  return api("/api/action", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ command, action: operation, ...fields }) });
}

function run(callback) {
  return async (...args) => {
    try { await callback(...args); } catch (error) { notice(error.message, true); }
  };
}

function field(label, value = "", type = "text") {
  const id = `field-${Math.random().toString(36).slice(2)}`;
  const wrapper = node("div");
  const caption = node("label", label);
  caption.htmlFor = id;
  const input = node(type === "textarea" ? "textarea" : "input");
  input.id = id;
  if (type !== "textarea") input.type = type;
  if (type !== "file") input.value = value;
  wrapper.append(caption, input);
  return { wrapper, input };
}

function openModal(title, children, submit = null, button = "Save", danger = false) {
  element("modal-title").textContent = title;
  element("modal-body").replaceChildren(...children);
  element("modal-error").textContent = "";
  element("modal-submit").hidden = !submit;
  element("modal-submit").textContent = button;
  element("modal-submit").className = danger ? "primary danger-button" : "primary";
  element("modal-submit").disabled = false;
  element("modal-cancel").disabled = false;
  element("modal-close").disabled = false;
  modalSubmit = submit;
  element("modal").showModal();
}

function confirmAction(title, message, callback, button = "Confirm") {
  openModal(title, [node("p", message)], callback, button, true);
}

function size(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function rowButton(label, callback, danger = false) {
  const button = node("button", label, danger ? "danger" : "");
  button.type = "button";
  button.addEventListener("click", run(callback));
  return button;
}

async function loadBuckets() {
  const result = await api("/api/buckets");
  const navigation = element("buckets");
  navigation.replaceChildren();
  for (const bucket of result.buckets) {
    const button = node("button");
    button.append(node("span", "◫"), node("span", bucket.name));
    button.dataset.bucket = bucket.name;
    button.setAttribute("aria-label", bucket.name);
    button.className = state.bucket === bucket.name ? "active" : "";
    button.addEventListener("click", run(async () => {
      state.bucket = bucket.name;
      element("prefix").value = "";
      element("show-history").checked = false;
      await refresh();
    }));
    navigation.append(button);
  }
  if (!result.buckets.some((bucket) => bucket.name === state.bucket)) state.bucket = "";
  element("breadcrumb").textContent = state.bucket || "Overview";
  element("bucket-title").textContent = state.bucket || "Your buckets";
  element("bucket-description").textContent = state.bucket ? "Browse, share, and keep track of every version." : "Select a bucket to explore its files, or create a new one.";
  element("empty-workspace").hidden = !!state.bucket;
  element("bucket-workspace").hidden = !state.bucket;
}

async function loadObjects(next = false) {
  if (!state.bucket) return;
  const generation = ++state.generation;
  const bucket = state.bucket;
  const history = element("show-history").checked;
  const query = new URLSearchParams({ bucket, prefix: element("prefix").value });
  if (next && state.page) {
    if (history) {
      query.set("key_marker", state.page.key_marker);
      query.set("version_marker", state.page.version_marker);
    } else query.set("token", state.page.token);
  }
  const previousCount = next ? state.offset + (state.page?.entries.length || 0) : 0;
  element("next-page").disabled = true;
  try {
    const result = await api(`/api/${history ? "versions" : "objects"}?${query}`);
    if (generation !== state.generation || bucket !== state.bucket) return;
    state.page = result;
    state.offset = previousCount;
    const rows = element("objects");
    rows.replaceChildren();
    for (const item of result.entries) {
      const row = node("tr");
      const keyCell = node("td", "", "object-key");
      keyCell.append(node("span", item.delete_marker ? "⊘" : "▤", "file-symbol"), document.createTextNode(item.key));
      row.append(keyCell, node("td", item.delete_marker ? "—" : size(item.size), "muted"),
        node("td", item.modified ? new Date(item.modified).toLocaleString() : "—", "muted"));
      const version = node("td");
      version.append(node("span", item.delete_marker ? "Delete marker" : item.latest ? "Latest" : history ? "Historical" : "Current", "version-badge"));
      if (item.version_id) {
        const id = node("span", item.version_id, "version-badge");
        id.title = item.version_id;
        version.append(id);
      }
      row.append(version);
      const operations = node("div", "", "row-actions");
      const selection = { bucket, key: item.key };
      if (history && item.version_id) selection.version_id = item.version_id;
      if (!item.delete_marker) {
        const download = node("a", "Download");
        download.href = `/api/download?${new URLSearchParams(selection)}`;
        operations.append(download);
        operations.append(rowButton("Info", async () => {
          const info = await action("object", "head", selection);
          openModal("Object details", [node("p", item.key), node("pre", JSON.stringify(info.headers, null, 2))]);
        }));
        if (history) {
          operations.append(rowButton("Restore", () => confirmAction("Restore this version?", `Create a new current version of “${item.key}” using this content.`, async () => {
            await action("versions", "restore", selection);
            await loadObjects();
            notice("Version restored.");
          }, "Restore version")));
        } else {
          operations.append(rowButton("Link", async () => {
            const result = await action("presign", "", { ...selection, expires: 900 });
            const link = field("Download link · expires in 15 minutes", result.url, "textarea");
            link.input.readOnly = true;
            openModal("Share a temporary link", [node("p", "Anyone with this link can download the object until it expires."), link.wrapper], async () => {
              await navigator.clipboard.writeText(result.url);
              notice("Temporary link copied.");
            }, "Copy link");
            link.input.select();
          }));
          operations.append(rowButton("Copy", () => {
            const destinationBucket = field("Destination bucket", bucket);
            const destinationKey = field("Destination key", item.key + ".copy");
            destinationBucket.input.required = destinationKey.input.required = true;
            openModal("Copy object", [destinationBucket.wrapper, destinationKey.wrapper], async () => {
              await action("object", "copy", { source_bucket: bucket, source_key: item.key,
                bucket: destinationBucket.input.value, key: destinationKey.input.value });
              await loadObjects();
              notice("Object copied.");
            }, "Copy object");
          }));
        }
      }
      operations.append(rowButton("Delete", () => confirmAction("Delete this object?",
        history ? `Permanently remove version ${item.version_id} of “${item.key}”. This cannot be undone.` : `Delete “${item.key}”. Versioned buckets retain earlier versions; unversioned data is permanently removed.`,
        async () => {
          await action("object", "delete", selection);
          await loadObjects();
          notice(history ? "Version permanently removed." : "Object deleted.");
        }, history ? "Delete permanently" : "Delete object"), true));
      const actionsCell = node("td");
      actionsCell.append(operations);
      row.append(actionsCell);
      rows.append(row);
    }
    element("file-empty").hidden = result.entries.length > 0;
    element("result-count").textContent = result.entries.length ? `${state.offset + 1}–${state.offset + result.entries.length} ${history ? "versions" : "objects"}${result.truncated ? " · more available" : ""}` : "0 objects";
    element("next-page").hidden = !result.truncated;
  } finally {
    if (generation === state.generation) element("next-page").disabled = false;
  }
}

async function refresh() {
  await loadBuckets();
  if (state.bucket) await loadObjects();
}

function createBucket() {
  const name = field("Bucket name");
  name.input.required = true;
  openModal("Create a bucket", [node("p", "Use 3–63 lowercase letters, digits, dots, or hyphens."), name.wrapper], async () => {
    await action("bucket", "create", { bucket: name.input.value });
    state.bucket = name.input.value;
    element("prefix").value = "";
    element("show-history").checked = false;
    await refresh();
    notice("Bucket created.");
  }, "Create bucket");
}

element("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  element("login-error").textContent = "";
  try {
    const result = await api("/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password: element("password").value }) });
    state.csrf = result.csrf;
    element("password").value = "";
    element("login-view").hidden = true;
    element("workspace").hidden = false;
    await refresh();
  } catch (error) {
    if (state.csrf) notice(error.message, true);
    else element("login-error").textContent = error.message;
  } finally { button.disabled = false; }
});

element("modal-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!modalSubmit || state.busy) return;
  state.busy = true;
  for (const id of ["modal-submit", "modal-cancel", "modal-close"]) element(id).disabled = true;
  element("modal-error").textContent = "";
  try { await modalSubmit(); element("modal").close(); }
  catch (error) { element("modal-error").textContent = error.message; }
  finally {
    state.busy = false;
    for (const id of ["modal-submit", "modal-cancel", "modal-close"]) element(id).disabled = false;
  }
});
for (const id of ["modal-close", "modal-cancel"]) element(id).addEventListener("click", () => element("modal").close());
element("modal").addEventListener("cancel", (event) => { if (state.busy) event.preventDefault(); });
element("logout").addEventListener("click", run(async () => { await api("/api/logout", { method: "POST" }); signedOut(); }));
element("new-bucket").addEventListener("click", createBucket);
element("empty-create").addEventListener("click", createBucket);
element("refresh").addEventListener("click", run(refresh));
element("search-form").addEventListener("submit", run(async (event) => { event.preventDefault(); await loadObjects(); }));
element("show-history").addEventListener("change", run(() => loadObjects()));
element("next-page").addEventListener("click", run(() => loadObjects(true)));
element("delete-bucket").addEventListener("click", () => {
  const bucket = state.bucket;
  confirmAction("Delete this bucket?", `Remove “${bucket}”. All objects, historical versions, delete markers and unfinished uploads must be removed first.`, async () => {
    await action("bucket", "delete", { bucket });
    state.bucket = "";
    await refresh();
    notice("Bucket deleted.");
  }, "Delete bucket");
});
element("upload").addEventListener("click", () => {
  const bucket = state.bucket;
  const file = field("Choose a file · maximum 64 MiB", "", "file");
  const key = field("Object key");
  file.input.required = key.input.required = true;
  file.input.addEventListener("change", () => { if (file.input.files[0]) key.input.value = element("prefix").value + file.input.files[0].name; });
  const progress = node("progress", "", "upload-progress");
  progress.max = 100;
  progress.value = 0;
  openModal("Upload a file", [file.wrapper, key.wrapper, node("p", "Uploading to an existing key replaces its current content. Enable versioning first to keep earlier versions."), progress], async () => {
    const selected = file.input.files[0];
    if (!selected) throw new Error("Choose a file first.");
    if (selected.size > 64 * 1024 * 1024) throw new Error("Use the management CLI for files larger than 64 MiB.");
    await new Promise((resolve, reject) => {
      const request = new XMLHttpRequest();
      request.open("POST", `/api/upload?${new URLSearchParams({ bucket, key: key.input.value })}`);
      request.setRequestHeader("X-Console-CSRF", state.csrf);
      request.setRequestHeader("Content-Type", selected.type || "application/octet-stream");
      request.upload.addEventListener("progress", (event) => { if (event.lengthComputable) progress.value = event.loaded / event.total * 100; });
      request.addEventListener("load", () => {
        try {
          const result = JSON.parse(request.responseText);
          if (request.status === 401) signedOut();
          if (!result.ok) reject(new Error(result.error || "Upload failed."));
          else resolve();
        } catch { reject(new Error("Upload failed. Check the service connection.")); }
      });
      request.addEventListener("error", () => reject(new Error("Upload connection failed.")));
      request.send(selected);
    });
    if (!key.input.value.startsWith(element("prefix").value)) element("prefix").value = "";
    await loadObjects();
    notice("File uploaded.");
  }, "Upload file");
});

element("versioning").addEventListener("click", run(async () => {
  const bucket = state.bucket;
  const result = await action("versioning", "get", { bucket });
  const choice = field("Versioning", "", "text");
  const select = node("select");
  select.id = choice.input.id;
  for (const [value, label] of [["enable", "Enabled · keep new versions"], ["suspend", "Suspended · replace null versions"]]) {
    const option = node("option", label);
    option.value = value;
    select.append(option);
  }
  select.value = result.status === "Suspended" ? "suspend" : "enable";
  choice.input.replaceWith(select);
  openModal("Bucket versioning", [node("p", `Current status: ${result.status}. Enabling versioning preserves overwritten content; it increases disk usage.`), choice.wrapper], async () => {
    await action("versioning", select.value, { bucket });
    notice("Versioning updated.");
  });
}));
element("access").addEventListener("click", run(async () => {
  const bucket = state.bucket;
  const result = await action("access", "get", { bucket });
  const select = node("select");
  select.setAttribute("aria-label", "Access mode");
  for (const [value, label] of [["private", "Private · owner only"], ["public", "Public · current object reads"], ["versions", "Public · current and historical reads"]]) {
    const option = node("option", label); option.value = value; select.append(option);
  }
  const statements = result.policy ? (Array.isArray(result.policy.Statement) ? result.policy.Statement : [result.policy.Statement]) : [];
  const grants = new Set(statements.flatMap((statement) => Array.isArray(statement.Action) ? statement.Action : [statement.Action]));
  select.value = grants.has("s3:GetObjectVersion") && grants.has("s3:GetObject") ? "versions" : grants.has("s3:GetObject") ? "public" : "private";
  openModal("Bucket access", [node("p", "Choose a new access mode. Saving replaces the current public-read policy. Public access allows anyone with an object URL to read its content."),
    node("pre", JSON.stringify(result.policy || { policy: "No public-read policy" }, null, 2)), select], async () => {
    await action("access", select.value === "private" ? "private" : "public", { bucket, ...(select.value === "private" ? {} : { include_versions: select.value === "versions" }) });
    notice("Bucket access updated.");
  }, "Apply access mode");
}));
element("cors").addEventListener("click", run(async () => {
  const bucket = state.bucket;
  let xml = "";
  try { xml = (await action("cors", "get", { bucket })).xml; }
  catch (error) { if (error.code !== "NoSuchCORSConfiguration") throw error; }
  const rules = field("CORS XML · leave empty to remove rules", xml, "textarea");
  openModal("Browser access rules", [node("p", "CORS controls browser origins, not object permissions."), rules.wrapper], async () => {
    await action("cors", rules.input.value.trim() ? "put" : "delete", { bucket, ...(rules.input.value.trim() ? { xml: rules.input.value } : {}) });
    notice("CORS rules updated.");
  });
}));
element("uploads").addEventListener("click", run(async () => {
  const bucket = state.bucket;
  const result = await action("multipart", "list", { bucket, prefix: "" });
  const container = node("div");
  for (const upload of result.uploads) {
    const item = node("div", "", "upload-item");
    item.append(node("span", `${upload.key}\n${upload.upload_id}`), rowButton("Abort", async () => {
      if (!window.confirm(`Abort unfinished upload for “${upload.key}”? Uploaded parts will be removed.`)) return;
      await action("multipart", "abort", { bucket, key: upload.key, upload_id: upload.upload_id });
      item.remove();
    }, true));
    container.append(item);
  }
  openModal("Unfinished uploads", [node("p", result.uploads.length ? "Remove abandoned uploads to release their stored parts." : "No unfinished uploads."), container]);
}));

(async () => {
  try {
    const result = await api("/api/session");
    state.csrf = result.csrf;
    element("login-view").hidden = true;
    element("workspace").hidden = false;
    await refresh();
  } catch (error) {
    if (state.csrf) notice(error.message, true);
  }
})();
