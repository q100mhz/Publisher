/* Publisher — клиентская логика (без фреймворков) */
(() => {
  window.BASE = document.body.dataset.base || "";  // префикс адресов (/publisher)
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  async function post(url, data) {
    const body = new FormData();
    Object.entries(data || {}).forEach(([k, v]) => body.append(k, v));
    const resp = await fetch(url, { method: "POST", body, headers: { "X-Requested-With": "fetch" } });
    let json = {};
    try { json = await resp.json(); } catch (_) { /* не JSON */ }
    if (!resp.ok) throw new Error(json.error || json.detail || `Ошибка ${resp.status}`);
    return json;
  }

  async function getJSON(url) {
    const resp = await fetch(url, { headers: { "X-Requested-With": "fetch" } });
    if (!resp.ok) throw new Error(`Ошибка ${resp.status}`);
    return resp.json();
  }

  function toast(text, kind = "ok") {
    let box = $("[data-flashes]");
    if (!box) {
      box = document.createElement("div");
      box.className = "flashes";
      box.dataset.flashes = "";
      document.body.appendChild(box);
    }
    const el = document.createElement("div");
    el.className = `flash ${kind}`;
    el.textContent = text;
    box.appendChild(el);
    setTimeout(() => el.remove(), 5000);
  }

  // ---------- Общее ----------
  $$("[data-flashes] .flash").forEach((el) => setTimeout(() => el.remove(), 5000));

  const menuBtn = $("[data-menu-toggle]");
  if (menuBtn) menuBtn.addEventListener("click", () => $(".nav").classList.toggle("open"));

  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-confirm]");
    if (btn && !confirm(btn.dataset.confirm)) {
      e.preventDefault();
      e.stopImmediatePropagation();
    }
  }, true);

  $$("[data-autosubmit]").forEach((el) => el.addEventListener("change", () => el.form.submit()));

  // ---------- Реестр: массовые действия ----------
  const bulkForm = $("[data-bulk-form]");
  if (bulkForm) {
    const boxes = $$("input[name=ids]", bulkForm);
    const all = $("[data-check-all]", bulkForm);
    const bar = $("[data-bulkbar]");
    const count = $("[data-bulk-count]");
    const sync = () => {
      const n = boxes.filter((b) => b.checked).length;
      count.textContent = n;
      bar.classList.toggle("hidden", n === 0);
      all.checked = n > 0 && n === boxes.length;
    };
    boxes.forEach((b) => b.addEventListener("change", sync));
    all.addEventListener("change", () => { boxes.forEach((b) => (b.checked = all.checked)); sync(); });
  }

  // ---------- Реестр: статус и плановая дата прямо в строке ----------
  async function quickUpdate(row, data, control) {
    control.classList.add("saving");
    control.disabled = true;
    try {
      const res = await post(row.dataset.quickUrl, data);
      const status = $("[data-quick-status]", row);
      status.className = `status-select s-${res.status}`;
      status.value = res.status;
      // Запись ушла в архив (или вернулась из него) — в текущем списке её больше нет
      const inArchiveView = new URLSearchParams(location.search).get("archive") === "1";
      if (res.archived !== inArchiveView) {
        row.classList.add("row-gone");
        setTimeout(() => row.remove(), 300);
        toast(res.archived ? "Отправлено в архив" : "Возвращено из архива");
        return;
      }
      row.classList.remove("flag-today", "flag-overdue");
      if (res.flag) row.classList.add(`flag-${res.flag}`);
      const published = $("[data-published]", row);
      published.textContent = res.published_at ? `✓ ${res.published_at}` : "";
      published.classList.toggle("hidden", !res.published_at);
      $("[data-planned-wrap]", row).classList.toggle("hidden", !!res.published_at);
      $("[data-quick-date]", row).value = res.planned;
      const chip = $("[data-flag-chip]", row);
      chip.className = `chip ${res.flag}${res.flag ? "" : " hidden"}`;
      chip.textContent = res.flag === "today" ? "Сегодня" : res.flag === "overdue" ? "Просрочено" : "";
    } catch (err) {
      toast(err.message, "warn");
    } finally {
      control.classList.remove("saving");
      control.disabled = false;
    }
  }

  $$("[data-quick-status]").forEach((sel) => {
    let previous = sel.value;
    sel.addEventListener("change", async () => {
      if (sel.value === "archive" && !confirm("Отправить новость в архив?")) { sel.value = previous; return; }
      await quickUpdate(sel.closest("tr"), { status: sel.value }, sel);
      previous = sel.value;
    });
  });
  $$("[data-quick-date]").forEach((input) => input.addEventListener("change", () =>
    quickUpdate(input.closest("tr"), { planned_publish_date: input.value }, input)));

  // ---------- Карточка новости ----------
  const newsForm = $("#news-form");
  const codeArea = $("[data-html-code]");
  const frame = $("[data-preview]");
  let dirty = false;

  if (newsForm) {
    newsForm.addEventListener("input", () => {
      dirty = true;
      const note = $("[data-dirty-note]");
      if (note) note.innerHTML = "<b style='color:var(--red)'>Есть несохранённые изменения</b>";
    });
    newsForm.addEventListener("submit", () => { dirty = false; });
    window.addEventListener("beforeunload", (e) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });
  }

  // Предпросмотр: HTML показывается в изолированном iframe (скрипты внутри не выполняются)
  function renderPreview() {
    if (!frame || !codeArea) return;
    const html = codeArea.value.trim();
    $("[data-preview-empty]").classList.toggle("hidden", !!html);
    frame.classList.toggle("hidden", !html);
    $("[data-edit-bar]")?.classList.toggle("hidden", !html);
    if (!html) return;
    const style = "<style>body{font-family:Georgia,'Times New Roman',serif;font-size:17px;line-height:1.65;color:#222;max-width:760px;margin:24px auto;padding:0 20px}img{max-width:100%;height:auto}h1,h2,h3{font-family:Arial,sans-serif;line-height:1.25}table{border-collapse:collapse}td,th{border:1px solid #ddd;padding:6px 10px}</style>";
    // Блок title/meta из ответа нейросети показываем отдельной плашкой — читатель его не видит, но проверить SEO полезно
    const doc = new DOMParser().parseFromString(html, "text/html");
    const metaDoc = new DOMParser().parseFromString(($("[data-meta]")?.value || "") + html, "text/html");
    const title = metaDoc.querySelector("title")?.textContent?.trim();
    const desc = metaDoc.querySelector('meta[name="description"]')?.getAttribute("content")?.trim();
    const seo = title || desc
      ? `<div style="font-family:Arial,sans-serif;font-size:13px;background:#fff6e0;border:1px dashed #e0b44a;border-radius:10px;padding:10px 14px;margin-bottom:22px;color:#5b4500"><b>SEO</b>${title ? `<br><b>title:</b> ${escapeHtml(title)}` : ""}${desc ? `<br><b>description:</b> ${escapeHtml(desc)}` : ""}</div>`
      : "";
    // Стили и прочее, что браузер вынес в <head>, сохраняем — и для вида, и чтобы не потерять их при правке
    headExtra = [...doc.head.querySelectorAll("style, link")].map((n) => n.outerHTML).join("\n");
    frame.srcdoc = `<!doctype html><html><head><meta charset="utf-8">${style}${editStyle}${headExtra}</head><body>${seo}<div id="pub-edit-root">${doc.body.innerHTML}</div></body></html>`;
  }

  // ---------- Правка текста прямо в предпросмотре ----------
  let headExtra = "";
  let editing = false;
  const editStyle = "<style>#pub-edit-root[contenteditable=true]{outline:2px dashed #f08a24;outline-offset:10px;border-radius:4px;cursor:text}#pub-edit-root[contenteditable=true]:focus{outline-style:solid}</style>";
  const editToggle = $("[data-edit-toggle]");

  function editRoot() {
    try { return frame.contentDocument.getElementById("pub-edit-root"); } catch (_) { return null; }
  }

  // Переносит правки из предпросмотра в HTML-код статьи
  function syncFromPreview() {
    const root = editRoot();
    if (!editing || !root || !codeArea) return;
    const html = (headExtra ? headExtra + "\n" : "") + root.innerHTML.trim();
    if (html !== codeArea.value.trim()) {
      codeArea.value = html;
      markDirty();
    }
  }

  function markDirty() {
    dirty = true;
    const note = $("[data-dirty-note]");
    if (note) note.innerHTML = "<b style='color:var(--red)'>Есть несохранённые изменения</b>";
  }

  function fitFrame() {
    try { frame.style.height = Math.max(420, frame.contentDocument.documentElement.scrollHeight + 20) + "px"; } catch (_) { /* нет доступа */ }
  }

  function enableEditing() {
    const root = editRoot();
    if (!root) return;
    const doc = frame.contentDocument;
    root.contentEditable = "true";
    if (root.dataset.bound) return;
    root.dataset.bound = "1";
    let t;
    root.addEventListener("input", () => { clearTimeout(t); t = setTimeout(() => { syncFromPreview(); fitFrame(); }, 300); });
    // Вставка из Word и других сайтов — только текстом, без чужих стилей и разметки
    root.addEventListener("paste", (e) => {
      e.preventDefault();
      const text = (e.clipboardData || window.clipboardData).getData("text/plain");
      doc.execCommand("insertText", false, text);
    });
  }

  function setEditing(on) {
    if (!on) syncFromPreview();
    editing = on;
    const root = editRoot();
    if (root) {
      if (on) enableEditing(); else root.contentEditable = "false";
    }
    if (editToggle) {
      editToggle.textContent = on ? "✓ Готово" : "✎ Править текст";
      editToggle.classList.toggle("btn-dark", on);
    }
    const hint = $("[data-edit-hint]");
    if (hint) hint.textContent = on
      ? "Кликните в текст и правьте. Ctrl+B — жирный, Ctrl+I — курсив, Ctrl+Z — отменить. После правок нажмите «Сохранить»."
      : "Можно исправить слово, предложение или переписать абзац прямо в статье.";
    if (on) { try { editRoot()?.focus(); } catch (_) { /* ничего */ } }
  }

  editToggle?.addEventListener("click", () => setEditing(!editing));
  newsForm?.addEventListener("submit", syncFromPreview, true);

  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  if (frame) {
    frame.addEventListener("load", () => {
      fitFrame();
      if (editing) enableEditing();
    });
    renderPreview();
    let t;
    codeArea.addEventListener("input", () => { clearTimeout(t); t = setTimeout(renderPreview, 400); });
    $("[data-meta]")?.addEventListener("input", () => { clearTimeout(t); t = setTimeout(renderPreview, 400); });
  }

  // Вкладки «Как увидит читатель» / «HTML-код»
  $$("[data-tab]").forEach((btn) => btn.addEventListener("click", () => {
    if (btn.dataset.tab !== "preview" && editing) setEditing(false);
    $$("[data-tab]").forEach((b) => b.classList.toggle("on", b === btn));
    $$("[data-pane]").forEach((p) => p.classList.toggle("hidden", p.dataset.pane !== btn.dataset.tab));
    if (btn.dataset.tab === "preview") renderPreview();
  }));

  // Копирование кода в буфер обмена
  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      try {
        await navigator.clipboard.writeText(text);
        return;
      } catch (_) { /* браузер запретил доступ — пробуем старый способ */ }
    }
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    if (!ok) throw new Error("Браузер не дал доступ к буферу обмена");
  }

  // Копирование содержимого любого поля (META, изменённый заголовок)
  $$("[data-copy-from]").forEach((btn) => btn.addEventListener("click", async () => {
    const field = $(btn.dataset.copyFrom);
    if (!field || !field.value.trim()) { toast("Поле пустое — копировать нечего", "warn"); return; }
    try {
      await copyText(field.value);
      const old = btn.innerHTML;
      btn.innerHTML = "✓ Скопировано";
      setTimeout(() => (btn.innerHTML = old), 2000);
    } catch (err) { toast(err.message, "warn"); }
  }));

  // Новый заголовок отдельным запросом к нейросети
  const titleBtn = $("[data-generate-title]");
  if (titleBtn) titleBtn.addEventListener("click", async () => {
    const privacy = $("[data-generate]")?.dataset.privacy;
    if (privacy && !confirm(`В статье найдены персональные данные (${privacy}). Перед отправкой в нейросеть они будут удалены. Продолжить?`)) return;
    const old = titleBtn.innerHTML;
    titleBtn.disabled = true;
    titleBtn.innerHTML = '<span class="blob-loader" style="width:14px;height:14px"></span> Придумываю…';
    try {
      const res = await post(titleBtn.dataset.url, { prompt_id: $("[data-title-prompt]").value });
      $("[data-new-title]").value = res.title;
      toast("Новый заголовок сохранён");
    } catch (err) {
      toast(err.message, "warn");
    } finally {
      titleBtn.disabled = false;
      titleBtn.innerHTML = old;
    }
  });

  const copyBtn = $("[data-copy]");
  if (copyBtn) copyBtn.addEventListener("click", async () => {
    const text = codeArea.value;
    if (!text.trim()) { toast("HTML-код пока пустой — сначала сгенерируйте статью", "warn"); return; }
    try {
      await copyText(text);
      const old = copyBtn.innerHTML;
      copyBtn.innerHTML = "✓ Скопировано";
      setTimeout(() => (copyBtn.innerHTML = old), 2000);
    } catch (err) {
      toast(err.message, "warn");
    }
  });

  // Генерация
  const genBtn = $("[data-generate]");
  function pollGeneration() {
    const running = $("[data-gen-running]");
    const errBox = $("[data-gen-error]");
    running.classList.remove("hidden");
    errBox.classList.add("hidden");
    genBtn.disabled = true;
    const timer = setInterval(async () => {
      try {
        const st = await getJSON(genBtn.dataset.stateUrl);
        if (st.state === "running") return;
        clearInterval(timer);
        if (st.state === "error") {
          running.classList.add("hidden");
          errBox.textContent = st.error || "Ошибка генерации";
          errBox.classList.remove("hidden");
          genBtn.disabled = false;
        } else {
          dirty = false;
          location.reload();
        }
      } catch (_) { /* временная ошибка сети — пробуем ещё */ }
    }, 3000);
  }

  if (genBtn) {
    genBtn.addEventListener("click", async () => {
      if (dirty && !confirm("В карточке есть несохранённые изменения. После генерации страница обновится и они пропадут. Продолжить?")) return;
      if (genBtn.dataset.privacy && !confirm(`В статье найдены персональные данные (${genBtn.dataset.privacy}). Перед отправкой в нейросеть они будут заменены метками вроде [ФИО] и [телефон] и в ИИ не попадут. Продолжить?`)) return;
      if (codeArea.value.trim() && !confirm("Текущий HTML-код будет заменён новой версией (старая останется в «Версиях генерации»). Продолжить?")) return;
      genBtn.disabled = true;
      try {
        await post(genBtn.dataset.url, { prompt_id: $("[data-prompt]").value });
        pollGeneration();
      } catch (err) {
        genBtn.disabled = false;
        toast(err.message, "warn");
      }
    });
    if (!$("[data-gen-running]").classList.contains("hidden")) pollGeneration();
  }

  // Генерация картинки
  const imgBtn = $("[data-generate-image]");
  function pollImage() {
    const running = $("[data-img-running]");
    const errBox = $("[data-img-error]");
    running.classList.remove("hidden");
    errBox.classList.add("hidden");
    imgBtn.disabled = true;
    const timer = setInterval(async () => {
      try {
        const st = await getJSON(imgBtn.dataset.stateUrl);
        if (st.state === "running") return;
        clearInterval(timer);
        if (st.state === "error") {
          running.classList.add("hidden");
          errBox.textContent = st.error || "Ошибка генерации картинки";
          errBox.classList.remove("hidden");
          imgBtn.disabled = false;
        } else {
          dirty = false;
          location.hash = "images";
          location.reload();
        }
      } catch (_) { /* временная ошибка сети — пробуем ещё */ }
    }, 3000);
  }
  if (imgBtn) {
    imgBtn.addEventListener("click", async () => {
      if (dirty && !confirm("В карточке есть несохранённые изменения. После создания картинки страница обновится и они пропадут. Продолжить?")) return;
      const privacy = $("[data-generate]")?.dataset.privacy;
      if (privacy && !confirm(`В статье найдены персональные данные (${privacy}). В описание для картинки они не попадут — будут удалены. Продолжить?`)) return;
      imgBtn.disabled = true;
      try {
        await post(imgBtn.dataset.url, { prompt_id: $("[data-img-prompt]").value });
        pollImage();
      } catch (err) {
        imgBtn.disabled = false;
        toast(err.message, "warn");
      }
    });
    if (!$("[data-img-running]").classList.contains("hidden")) pollImage();
  }

  // Картинки: drag & drop
  $$("[data-dropzone]").forEach((zone) => {
    const input = $("input[type=file]", zone);
    ["dragenter", "dragover"].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add("drag"); }));
    ["dragleave", "drop"].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove("drag"); }));
    zone.addEventListener("drop", (e) => {
      input.files = e.dataTransfer.files;
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
  });
  $$("[data-autosubmit-files]").forEach((input) => input.addEventListener("change", () => {
    if (!input.files.length) return;
    if (dirty && !confirm("В карточке есть несохранённые изменения — после загрузки картинок они пропадут. Продолжить?")) return;
    dirty = false;
    input.form.submit();
  }));
  $$("[data-file-list]").forEach((input) => input.addEventListener("change", () => {
    const label = $("[data-file-label]", input.closest("[data-dropzone]"));
    label.textContent = input.files.length ? `Выбрано файлов: ${input.files.length}` : "Перетащите картинки сюда или нажмите, чтобы выбрать";
  }));

  // ---------- Промпты: вставка подстановок ----------
  const promptText = $("[data-prompt-text]");
  $$("[data-insert]").forEach((btn) => btn.addEventListener("click", () => {
    const value = btn.dataset.insert;
    const { selectionStart: s, selectionEnd: e } = promptText;
    promptText.setRangeText(value, s, e, "end");
    promptText.focus();
  }));

  // ---------- Источники: ручной запуск и статус ----------
  const statusHost = $("[data-status-url]");
  let statusTimer = null;

  function startStatusPolling() {
    if (statusTimer || !statusHost) return;
    statusTimer = setInterval(async () => {
      try {
        const st = await getJSON(statusHost.dataset.statusUrl);
        let anyRunning = false;
        Object.entries(st.sources).forEach(([id, s]) => {
          const card = $(`[data-source="${id}"]`);
          if (!card) return;
          anyRunning ||= s.running;
          $("[data-dot]", card).className = `dot ${s.running ? "running" : "st-" + s.status}`;
          $("[data-message]", card).textContent = s.running ? "Парсится…" : (s.last_run ? `${s.last_run} — ${s.message}` : "Ещё не запускался");
          $$("[data-run]", card).forEach((btn) => { btn.disabled = (st.auto && !btn.dataset.allowAuto) || s.running; });
        });
        const next = $("[data-next-run]");
        if (next && st.next_run) next.textContent = st.next_run;
        if (!anyRunning) { clearInterval(statusTimer); statusTimer = null; }
      } catch (_) { /* пробуем на следующем тике */ }
    }, 3000);
  }

  $$("[data-run]").forEach((btn) => btn.addEventListener("click", async () => {
    btn.disabled = true;
    const card = btn.closest("[data-source]");
    try {
      await post(btn.dataset.run);
      $("[data-dot]", card).className = "dot running";
      $("[data-message]", card).textContent = "Парсится…";
      $$("[data-run]", card).forEach((b) => { b.disabled = true; });
      toast(btn.dataset.started || "Парсинг запущен. Новые статьи появятся в реестре по мере загрузки.");
      startStatusPolling();
    } catch (err) {
      btn.disabled = false;
      toast(err.message, "warn");
    }
  }));
  if ($(".dot.running")) startStatusPolling();

  // ---------- Настройки: выбор нейросети ----------
  const providerSelect = $("[data-provider]");
  if (providerSelect) {
    const modelInput = $("[data-model]");
    const syncProvider = () => {
      const opt = providerSelect.selectedOptions[0];
      const models = opt.dataset.models ? opt.dataset.models.split("|") : [];
      $("[data-model-list]").innerHTML = models.map((m) => `<option value="${m}">`).join("");
      modelInput.placeholder = opt.dataset.defaultModel || "название модели у этого сервиса";
      $("[data-model-hint]").textContent = models.length
        ? `Варианты: ${models.join(", ")}. Пусто — ${opt.dataset.defaultModel || models[0]}.`
        : "Впишите название модели так, как оно указано в документации сервиса.";
      $("[data-base-url-field]").classList.toggle("hidden", opt.dataset.editable !== "1");
      $("[data-base-url]").placeholder = opt.dataset.base || "https://example.com/v1";
      $("[data-base-hint]").textContent = providerSelect.value === "qwen"
        ? "Пусто — международный адрес Model Studio. Если в консоли Alibaba Cloud указан адрес рабочего пространства (…maas.aliyuncs.com/compatible-mode/v1), вставьте его."
        : "Для любого сервиса с OpenAI-совместимым API: российские модели через шлюзы, локальные модели и т. п.";
      $("[data-temperature-field]").classList.toggle("hidden", opt.dataset.temperature !== "1");
      const keyMask = opt.dataset.keyMask;
      $("[data-key-input]").placeholder = keyMask ? `сохранён: ${keyMask} — оставьте пустым, чтобы не менять` : "вставьте ключ";
      $("[data-remove-key]").classList.toggle("hidden", !keyMask);
      $("[data-base-note]")?.classList.toggle("hidden", providerSelect.value !== "deepseek");
      const link = $("[data-key-link]");
      link.innerHTML = opt.dataset.keyUrl
        ? `Где взять ключ: <a href="${opt.dataset.keyUrl}" target="_blank" rel="noopener noreferrer">${opt.dataset.keyUrl}</a>`
        : "";
    };
    providerSelect.addEventListener("change", () => {
      // Модель от другого сервиса не подойдёт — очищаем, если она не из списка нового
      const models = (providerSelect.selectedOptions[0].dataset.models || "").split("|");
      if (modelInput.value && !models.includes(modelInput.value)) modelInput.value = "";
      syncProvider();
    });
    syncProvider();
  }

  const imgProvider = $("[data-img-provider]");
  if (imgProvider) {
    const syncImg = () => {
      const opt = imgProvider.selectedOptions[0];
      const models = opt.dataset.models ? opt.dataset.models.split("|") : [];
      $("[data-img-model-list]").innerHTML = models.map((m) => `<option value="${m}">`).join("");
      $("[data-img-base-url]").classList.toggle("hidden", !["custom", "qwen"].includes(imgProvider.value));
      $("[data-img-base-hint]").textContent = imgProvider.value === "qwen"
        ? "Пусто — международный адрес Model Studio. Можно вставить адрес своего рабочего пространства (…maas.aliyuncs.com/compatible-mode/v1)."
        : "Сервис должен принимать запросы в формате OpenAI Images (/images/generations).";
      $("[data-img-key]").classList.toggle("hidden", imgProvider.value === "");
      const imgMask = opt.dataset.keyMask;
      $("[data-img-key-input]").placeholder = imgMask ? `сохранён: ${imgMask} — оставьте пустым, чтобы не менять` : "вставьте ключ";
      $("[data-img-remove-key]").classList.toggle("hidden", !imgMask);
    };
    imgProvider.addEventListener("change", syncImg);
    syncImg();
  }

  const aiTest = $("[data-ai-test]");
  if (aiTest) aiTest.addEventListener("click", async () => {
    const box = $("[data-ai-status]");
    aiTest.disabled = true;
    box.className = "gen-status running";
    box.innerHTML = '<span class="blob-loader"></span> Отправляю пробный запрос…';
    try {
      const res = await post(aiTest.dataset.aiTest);
      box.className = `gen-status ${res.ok ? "running" : "error"}`;
      box.textContent = res.ok ? `✓ ${res.message}` : res.error;
    } catch (err) {
      box.className = "gen-status error";
      box.textContent = err.message;
    } finally {
      aiTest.disabled = false;
    }
  });

  // ---------- Битрикс24: настройки ----------
  const bx = $("[data-bitrix]");
  if (bx) {
    const status = $("[data-bx-status]");
    const show = (text, ok) => { status.className = `gen-status ${ok ? "running" : "error"}`; status.textContent = text; };
    const siteSel = $("[data-bx-site]"), tplSel = $("[data-bx-template]");
    const loadPages = async () => {
      if (!siteSel.value) return;
      $("[data-bx-site-title]").value = siteSel.selectedOptions[0].textContent;
      tplSel.innerHTML = "<option value=''>загружаю…</option>";
      try {
        const res = await post(bx.dataset.pagesUrl, { site_id: siteSel.value, webhook: $("[data-bx-webhook]").value });
        tplSel.innerHTML = res.pages.map((p) => `<option value="${p.id}">${escapeHtml(p.title)} (ID ${p.id})</option>`).join("")
          || "<option value=''>на сайте нет страниц</option>";
        $("[data-bx-template-title]").value = tplSel.selectedOptions[0]?.textContent || "";
        show("Выберите страницу-шаблон и нажмите «Сохранить».", true);
      } catch (err) { show(err.message, false); }
    };
    $("[data-bx-load]").addEventListener("click", async () => {
      show("Подключаюсь к порталу…", true);
      try {
        const res = await post(bx.dataset.sitesUrl, { webhook: $("[data-bx-webhook]").value });
        siteSel.innerHTML = res.sites.map((s) => `<option value="${s.id}">${escapeHtml(s.title)} (ID ${s.id})</option>`).join("");
        show(`Подключение работает: сайтов — ${res.sites.length}.`, true);
        await loadPages();
      } catch (err) { show(err.message, false); }
    });
    siteSel.addEventListener("change", loadPages);
    const checkBtn = $("[data-bx-check]");
    if (checkBtn) checkBtn.addEventListener("click", async () => {
      const report = $("[data-bx-report]");
      show("Проверяю шаблон на портале…", true);
      checkBtn.disabled = true;
      try {
        const res = await post(checkBtn.dataset.bxCheck);
        const rows = res.blocks.map((b) => `<tr><td>№${b.n}</td><td>${escapeHtml(b.code || "блок")}</td>`
          + `<td class="small">${b.html_fields.length ? escapeHtml(b.html_fields.join(", ")) : "—"}</td>`
          + `<td>${b.article ? "✅ сюда встанет статья" : (b.marker ? `метка найдена (${escapeHtml(b.marker)})` : "")}`
          + `${b.title ? " %%TITLE%%" : ""}${b.image ? " %%IMAGE%%" : ""}</td></tr>`).join("");
        report.innerHTML = `<table class="mini-table wide"><tr><th>Блок</th><th>Код</th><th>HTML-поле</th><th>Метки</th></tr>${rows}</table>`
          + `<details class="fold" ${res.ok ? "" : "open"} style="margin-top:10px"><summary class="small">Подробный отчёт для разработчика</summary>`
          + `<textarea class="code" readonly data-bx-report-text style="min-height:220px;margin-top:8px"></textarea>`
          + `<button type="button" class="btn-sm btn-dark" data-copy-from="[data-bx-report-text]" style="margin-top:8px">⧉ Скопировать отчёт</button></details>`;
        $("[data-bx-report-text]").value = res.report || "";
        $("[data-bx-report] [data-copy-from]").addEventListener("click", async (e) => {
          await copyText($("[data-bx-report-text]").value);
          e.target.textContent = "✓ Скопировано";
        });
        report.classList.remove("hidden");
        show(res.ok ? "Шаблон в порядке: блок для статьи найден." : "Блок с меткой %%ARTICLE%% не найден — проверьте шаблон.", res.ok);
      } catch (err) { show(err.message, false); } finally { checkBtn.disabled = false; }
    });
    tplSel.addEventListener("change", () => { $("[data-bx-template-title]").value = tplSel.selectedOptions[0].textContent; });
  }

  // ---------- Битрикс24: публикация из карточки ----------
  const bxCard = $("[data-bx-card]");
  const bxBtn = $("[data-bx-publish]");
  function pollBitrix() {
    $("[data-bx-running]").classList.remove("hidden");
    $("[data-bx-error]").classList.add("hidden");
    if (bxBtn) bxBtn.disabled = true;
    const timer = setInterval(async () => {
      try {
        const st = await getJSON(bxCard.dataset.stateUrl);
        if (st.state === "running") return;
        clearInterval(timer);
        if (st.state === "error") {
          $("[data-bx-running]").classList.add("hidden");
          const box = $("[data-bx-error]");
          box.textContent = st.error;
          box.classList.remove("hidden");
          if (bxBtn) bxBtn.disabled = false;
        } else {
          dirty = false;
          location.reload();
        }
      } catch (_) { /* пробуем ещё */ }
    }, 3000);
  }
  if (bxBtn) bxBtn.addEventListener("click", async () => {
    if (dirty) { toast("Сначала сохраните изменения в карточке — на сайт уйдёт сохранённая версия", "warn"); return; }
    bxBtn.disabled = true;
    try {
      await post(bxCard.dataset.url);
      pollBitrix();
    } catch (err) {
      bxBtn.disabled = false;
      toast(err.message, "warn");
    }
  });
  if (bxCard && !$("[data-bx-running]").classList.contains("hidden")) pollBitrix();

  // ---------- Журнал: обновление идущего запуска ----------
  if ($("[data-autorefresh]")) setTimeout(() => location.reload(), 3000);
})();
