/* Publisher — клиентская логика (без фреймворков) */
(() => {
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
    if (!html) return;
    const style = "<style>body{font-family:Georgia,'Times New Roman',serif;font-size:17px;line-height:1.65;color:#222;max-width:760px;margin:24px auto;padding:0 20px}img{max-width:100%;height:auto}h1,h2,h3{font-family:Arial,sans-serif;line-height:1.25}table{border-collapse:collapse}td,th{border:1px solid #ddd;padding:6px 10px}</style>";
    // Блок title/meta из ответа нейросети показываем отдельной плашкой — читатель его не видит, но проверить SEO полезно
    const doc = new DOMParser().parseFromString(html, "text/html");
    const title = doc.querySelector("title")?.textContent?.trim();
    const desc = doc.querySelector('meta[name="description"]')?.getAttribute("content")?.trim();
    const seo = title || desc
      ? `<div style="font-family:Arial,sans-serif;font-size:13px;background:#fff6e0;border:1px dashed #e0b44a;border-radius:10px;padding:10px 14px;margin-bottom:22px;color:#5b4500"><b>SEO</b>${title ? `<br><b>title:</b> ${escapeHtml(title)}` : ""}${desc ? `<br><b>description:</b> ${escapeHtml(desc)}` : ""}</div>`
      : "";
    frame.srcdoc = `<!doctype html><html><head><meta charset="utf-8">${style}</head><body>${seo}${doc.body.innerHTML}</body></html>`;
  }

  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  if (frame) {
    frame.addEventListener("load", () => {
      try {
        const h = frame.contentDocument.documentElement.scrollHeight;
        frame.style.height = Math.max(420, h + 20) + "px";
      } catch (_) { /* нет доступа — оставляем высоту по умолчанию */ }
    });
    renderPreview();
    let t;
    codeArea.addEventListener("input", () => { clearTimeout(t); t = setTimeout(renderPreview, 400); });
  }

  // Вкладки «Как увидит читатель» / «HTML-код»
  $$("[data-tab]").forEach((btn) => btn.addEventListener("click", () => {
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
          const btn = $("[data-run]", card);
          btn.disabled = st.auto || s.running;
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
      toast("Парсинг запущен. Новые статьи появятся в реестре по мере загрузки.");
      startStatusPolling();
    } catch (err) {
      btn.disabled = false;
      toast(err.message, "warn");
    }
  }));
  if ($(".dot.running")) startStatusPolling();

  // ---------- Журнал: обновление идущего запуска ----------
  if ($("[data-autorefresh]")) setTimeout(() => location.reload(), 3000);
})();
