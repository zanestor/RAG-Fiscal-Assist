"use strict";

const state = {
  status: null,
  selectedSources: new Set(),
  conversationId: null,
  previousResponseId: null,
  busy: false,
  turns: [],
};

const el = {};

document.addEventListener("DOMContentLoaded", () => {
  ["modelBadge", "connectionStatus", "documentCount", "indexMeter", "indexSummary", "sourceFilters",
    "toggleSources", "setupPanel", "setupTitle", "setupMessage", "setupSteps", "welcomePanel", "messages",
    "chatForm", "questionInput", "sendButton", "newChat", "exportConversation", "historySection", "historyList",
    "historyEmpty", "refreshHistory"].forEach((id) => { el[id] = document.getElementById(id); });
  bindEvents();
  loadStatus();
});

function bindEvents() {
  el.chatForm.addEventListener("submit", (event) => {
    event.preventDefault();
    submitQuestion();
  });
  el.questionInput.addEventListener("input", () => {
    autoResize();
    updateSendButton();
  });
  el.questionInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submitQuestion();
    }
  });
  document.querySelectorAll("[data-question]").forEach((button) => button.addEventListener("click", () => {
    if (button.disabled) return;
    el.questionInput.value = button.dataset.question;
    autoResize();
    submitQuestion();
  }));
  el.toggleSources.addEventListener("click", toggleAllSources);
  el.newChat.addEventListener("click", resetConversation);
  el.refreshHistory.addEventListener("click", loadHistory);
  el.exportConversation.addEventListener("click", () => exportConversationToPDF(state.turns));
}

function updateExportButton() {
  el.exportConversation.hidden = state.turns.length === 0;
}

async function loadStatus() {
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.status = await response.json();
    renderStatus();
    loadHistory();
  } catch (error) {
    el.connectionStatus.className = "connection-status error";
    el.connectionStatus.innerHTML = "<i></i> Serveur indisponible";
    showSetup("Serveur indisponible", `Impossible de lire l’état du serveur (${error.message}).`, []);
  }
}

function renderStatus() {
  const status = state.status;
  const providerLabel = status.active_provider === "openai" ? "OpenAI" : "OpenRouter secours";
  el.modelBadge.textContent = `${providerLabel} · ${status.model}`;
  el.documentCount.textContent = new Intl.NumberFormat("fr-FR").format(status.discovered);
  const searchable = status.searchable ?? status.indexed;
  const ratio = status.discovered ? Math.round((searchable / status.discovered) * 100) : 0;
  el.indexMeter.style.width = `${ratio}%`;
  el.indexSummary.textContent = `${new Intl.NumberFormat("fr-FR").format(searchable)} recherchables · ${status.needs_ocr} à OCRiser · ${status.review_required || 0} à contrôler`;
  renderSources(status.sources || []);

  if (status.ready_for_questions) {
    el.connectionStatus.className = "connection-status ready";
    el.connectionStatus.innerHTML = "<i></i> Bibliothèque prête";
    el.setupPanel.hidden = true;
    setChatEnabled(true);
  } else {
    el.connectionStatus.className = "connection-status error";
    el.connectionStatus.innerHTML = "<i></i> Configuration requise";
    setChatEnabled(false);
    if (!status.api_key_configured) {
      showSetup(
        "Ajoutez une clé API",
        "La clé reste uniquement dans le serveur Python et n’est jamais envoyée au navigateur.",
        ["Copiez <code>.env.example</code> vers <code>.env</code>.", "Insérez <code>OPENAI_API_KEY=...</code> et/ou <code>OPENROUTER_API_KEY=...</code>.", "Lancez <code>python cli.py index --limit 5</code> pour un premier test."],
      );
    } else if (!searchable) {
      showSetup(
        "Indexez les documents",
        "L’inventaire est prêt, mais aucun document n’a encore été ajouté aux index de recherche.",
        ["Testez le secours local avec <code>python cli.py index-local --limit 5</code>.", "Vérifiez le résultat dans cette page.", "Lancez ensuite <code>python cli.py index</code> pour construire les index disponibles."],
      );
    }
  }
}

function renderSources(sources) {
  el.sourceFilters.replaceChildren(...sources.filter((source) => source.enabled).map((source) => {
    state.selectedSources.add(source.id);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "source-filter selected";
    button.dataset.source = source.id;
    button.title = source.description;
    button.innerHTML = `<span class="source-abbr">${escapeHTML(abbreviation(source.label))}</span><span><strong>${escapeHTML(source.label)}</strong><small>${formatNumber(source.document_count)} documents</small></span><span class="check">✓</span>`;
    button.addEventListener("click", () => {
      if (state.selectedSources.has(source.id)) state.selectedSources.delete(source.id);
      else state.selectedSources.add(source.id);
      button.classList.toggle("selected", state.selectedSources.has(source.id));
      updateToggleLabel();
      updateSendButton();
    });
    return button;
  }));
  updateToggleLabel();
}

function toggleAllSources() {
  const buttons = [...el.sourceFilters.querySelectorAll(".source-filter")];
  const allSelected = state.selectedSources.size === buttons.length;
  state.selectedSources.clear();
  if (!allSelected) buttons.forEach((button) => state.selectedSources.add(button.dataset.source));
  buttons.forEach((button) => button.classList.toggle("selected", state.selectedSources.has(button.dataset.source)));
  updateToggleLabel();
  updateSendButton();
}

function updateToggleLabel() {
  const count = el.sourceFilters.querySelectorAll(".source-filter").length;
  el.toggleSources.textContent = state.selectedSources.size === count ? "Tout retirer" : "Tout sélectionner";
}

function showSetup(title, message, steps) {
  el.setupTitle.textContent = title;
  el.setupMessage.textContent = message;
  el.setupSteps.innerHTML = steps.map((step) => `<li>${step}</li>`).join("");
  el.setupPanel.hidden = false;
}

function setChatEnabled(enabled) {
  el.questionInput.disabled = !enabled;
  document.querySelectorAll("[data-question]").forEach((button) => { button.disabled = !enabled; });
  updateSendButton();
}

async function submitQuestion() {
  const question = el.questionInput.value.trim();
  if (!question || state.busy || !state.status?.ready_for_questions || !state.selectedSources.size) return;
  state.busy = true;
  el.welcomePanel.hidden = true;
  el.setupPanel.hidden = true;
  addUserMessage(question);
  el.questionInput.value = "";
  autoResize();
  updateSendButton();
  const thinking = addThinkingMessage();
  scrollToBottom();

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question,
        sources: [...state.selectedSources],
        conversation_id: state.conversationId,
        previous_response_id: state.previousResponseId,
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    thinking.remove();
    state.conversationId = payload.conversation_id || state.conversationId;
    // Keep any existing response id when a turn returns none (OpenRouter path
    // always returns null); only a real id from the OpenAI path should update it,
    // so a mid-conversation fallback never breaks the OpenAI continuation chain.
    state.previousResponseId = payload.response_id || state.previousResponseId;
    addAssistantMessage(payload.answer, payload.citations || [], payload.model, question);
    state.turns.push({ question, answer: payload.answer, citations: payload.citations || [], model: payload.model });
    updateExportButton();
    loadHistory();
  } catch (error) {
    thinking.remove();
    addErrorMessage(error.message);
  } finally {
    state.busy = false;
    updateSendButton();
    el.questionInput.focus();
    scrollToBottom();
  }
}

function addUserMessage(text) {
  const message = document.createElement("div");
  message.className = "message user";
  message.textContent = text;
  el.messages.append(message);
}

function addThinkingMessage() {
  const message = document.createElement("div");
  message.className = "message assistant-message";
  message.innerHTML = '<div class="assistant-avatar">§</div><div class="thinking" aria-label="Recherche dans les sources"><span></span><span></span><span></span></div>';
  el.messages.append(message);
  return message;
}

function addAssistantMessage(answer, citations, model) {
  const message = document.createElement("div");
  message.className = "message assistant-message";
  const content = document.createElement("div");
  content.className = "answer-content";
  const meta = document.createElement("div");
  meta.className = "answer-meta";
  meta.innerHTML = `<strong>Référence fiscale</strong><span>·</span><span>${escapeHTML(model)}</span>`;
  const text = renderMarkdown(answer);
  content.append(meta, text);

  if (citations.length) {
    const list = document.createElement("div");
    list.className = "citation-list";
    citations.forEach((citation, index) => {
      const link = document.createElement("a");
      link.className = "citation-card";
      link.href = citation.pdf_url;
      link.target = "_blank";
      link.rel = "noopener";
      const mirrors = citation.also_available_from || [];
      const mirrorsHTML = mirrors.length
        ? `<small class="citation-mirrors">Également disponible via ${mirrors
            .map((mirror) =>
              mirror.source_url
                ? `<a href="${escapeHTML(mirror.source_url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${escapeHTML(mirror.source_label || mirror.source)}</a>`
                : escapeHTML(mirror.source_label || mirror.source)
            )
            .join(", ")}</small>`
        : "";
      link.innerHTML = `<span class="citation-number">${index + 1}</span><span><strong>${escapeHTML(citation.title)}</strong><small>${escapeHTML(citation.source_label)}${citation.category ? ` · ${escapeHTML(citation.category)}` : ""}</small>${mirrorsHTML}</span>`;
      list.append(link);
    });
    content.append(list);
  }

  const avatar = document.createElement("div");
  avatar.className = "assistant-avatar";
  avatar.textContent = "§";
  message.append(avatar, content);
  el.messages.append(message);
}

function markdownToPlainText(markdown) {
  return String(markdown ?? "")
    .replace(/\r\n?/g, "\n")
    .replace(/```[\s\S]*?```/g, (block) => block.replace(/```/g, ""))
    .replace(/^\s{0,3}#{1,6}\s+/gm, "")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/__([^_]+)__/g, "$1")
    .replace(/\*([^*]+)\*/g, "$1")
    .replace(/_([^_]+)_/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/^\s*[-*–—]\s+/gm, "• ")
    .split("\n")
    .join("\n");
}

// Generates a real PDF client-side (jsPDF) rather than relying on the browser's
// print-to-PDF: that route only preserves clickable hyperlinks when the user
// picks Chrome's own "Save as PDF" destination - picking "Microsoft Print to
// PDF" (the OS printer driver) silently rasterizes the page and drops every
// link. Building the PDF directly sidesteps that choice entirely.
//
// Exports the WHOLE conversation (every question/answer turn so far), not a
// single message: a multi-turn discussion loses its context if only the last
// answer is captured, so this walks state.turns in order and gives every
// turn its own question/answer/sources block in one combined document.
function exportConversationToPDF(turns) {
  const { jsPDF } = window.jspdf || {};
  if (!jsPDF) {
    addErrorMessage("La génération du PDF a échoué : bibliothèque jsPDF introuvable.");
    return;
  }
  if (!turns.length) return;

  const doc = new jsPDF({ unit: "mm", format: "a4" });
  const pageWidth = doc.internal.pageSize.getWidth();
  const pageHeight = doc.internal.pageSize.getHeight();
  const marginX = 20;
  const maxWidth = pageWidth - marginX * 2;
  const bottomLimit = pageHeight - 20;
  let y = 20;

  const ensureSpace = (lineHeight) => {
    if (y + lineHeight > bottomLimit) {
      doc.addPage();
      y = 20;
    }
  };

  // The font MUST be set before splitTextToSize runs: it measures text using
  // whatever font is currently active, so measuring with one font and then
  // drawing in another (e.g. a leftover smaller/narrower font from the
  // previous call) produces lines that are wider than the page once actually
  // drawn - the text runs past the right margin and is visibly cut off.
  const writeText = (text, options = {}) => {
    const { fontSize, fontStyle = "normal", lineHeight, color = [17, 17, 17], link, spacingAfter = 0 } = options;
    doc.setFont("helvetica", fontStyle);
    doc.setFontSize(fontSize);
    doc.setTextColor(...color);
    const lines = doc.splitTextToSize(text, maxWidth);
    lines.forEach((line) => {
      ensureSpace(lineHeight);
      if (link) doc.textWithLink(line, marginX, y, { url: link });
      else doc.text(line, marginX, y);
      y += lineHeight;
    });
    y += spacingAfter;
  };

  // Same green used by .citation-ref in the chat view, so an inline reference
  // like "[Loi n 004/2001, art. 3]" reads as visually distinct from body prose
  // here too, not just on screen.
  const CITATION_REF_COLOR = [13, 81, 71];
  const CITATION_TOKEN_PATTERN = /\[[^\]\n]+\]/g;

  // writeText() colors a whole wrapped line uniformly, which can't set an
  // inline citation apart from the prose around it - this lays text out word
  // by word instead, switching color for tokens inside [...] while still
  // wrapping at maxWidth like a normal paragraph.
  const writeRichText = (text, options = {}) => {
    const { fontSize, fontStyle = "normal", lineHeight, color = [17, 17, 17], spacingAfter = 0 } = options;
    doc.setFont("helvetica", fontStyle);
    doc.setFontSize(fontSize);

    String(text ?? "").split("\n").forEach((paragraph) => {
      if (!paragraph) {
        ensureSpace(lineHeight);
        y += lineHeight;
        return;
      }
      const segments = [];
      let lastIndex = 0;
      let match;
      CITATION_TOKEN_PATTERN.lastIndex = 0;
      while ((match = CITATION_TOKEN_PATTERN.exec(paragraph)) !== null) {
        if (match.index > lastIndex) segments.push({ text: paragraph.slice(lastIndex, match.index), isCitation: false });
        segments.push({ text: match[0], isCitation: true });
        lastIndex = match.index + match[0].length;
      }
      if (lastIndex < paragraph.length) segments.push({ text: paragraph.slice(lastIndex), isCitation: false });

      const tokens = [];
      segments.forEach((segment) => {
        segment.text.split(/(\s+)/).forEach((word) => {
          if (word.length) tokens.push({ text: word, isCitation: segment.isCitation });
        });
      });

      let cursorX = marginX;
      ensureSpace(lineHeight);
      tokens.forEach((token) => {
        // Font MUST be set before measuring, same reasoning as writeText() above:
        // bold glyphs are wider than normal ones, so measuring with whatever font
        // was active from the previous token (rather than the one this token will
        // actually be drawn in) understates a citation token's width and makes it
        // visibly crowd into whatever follows it.
        doc.setFont("helvetica", token.isCitation ? "bold" : fontStyle);
        const width = doc.getTextWidth(token.text);
        if (token.text.trim() && cursorX + width > marginX + maxWidth) {
          cursorX = marginX;
          y += lineHeight;
          ensureSpace(lineHeight);
        }
        if (token.text.trim()) {
          doc.setTextColor(...(token.isCitation ? CITATION_REF_COLOR : color));
          doc.text(token.text, cursorX, y);
        }
        cursorX += width;
      });
      y += lineHeight;
    });
    y += spacingAfter;
  };

  writeText("Référence Fiscale RDC", { fontSize: 18, fontStyle: "bold", lineHeight: 7, color: [13, 81, 71] });
  const generatedAt = new Intl.DateTimeFormat("fr-FR", { dateStyle: "long", timeStyle: "short" }).format(new Date());
  writeText(`Conversation exportée le ${generatedAt} · ${turns.length} échange(s)`, { fontSize: 9, lineHeight: 5, color: [90, 90, 90], spacingAfter: 8 });

  turns.forEach((turn, turnIndex) => {
    if (turnIndex > 0) {
      ensureSpace(14);
      doc.setDrawColor(220, 227, 221);
      doc.line(marginX, y, pageWidth - marginX, y);
      y += 8;
    }

    writeText(`Question ${turnIndex + 1}`, { fontSize: 8, fontStyle: "bold", lineHeight: 4, color: [36, 116, 102], spacingAfter: 1 });
    writeText(turn.question || "", { fontSize: 11, fontStyle: "bold", lineHeight: 5.5, spacingAfter: 6 });

    writeRichText(markdownToPlainText(turn.answer), { fontSize: 10.5, lineHeight: 5.2, spacingAfter: 8 });

    const citations = turn.citations || [];
    if (citations.length) {
      ensureSpace(10);
      writeText("Sources citées", { fontSize: 12, fontStyle: "bold", lineHeight: 6, color: [13, 81, 71], spacingAfter: 4 });

      citations.forEach((citation, index) => {
        writeText(`${index + 1}. ${citation.title || ""}`, { fontSize: 10, fontStyle: "bold", lineHeight: 5 });

        const labelText = `${citation.source_label || ""}${citation.category ? ` · ${citation.category}` : ""}`;
        if (labelText.trim()) writeText(labelText, { fontSize: 9, lineHeight: 5, color: [85, 85, 85] });

        // Deliberately the ONLINE source, not the app's local /documents/{id} route:
        // an exported PDF is meant to be read outside this app, so its links must
        // still resolve there. When no online URL exists, say so rather than
        // silently linking to the local route.
        if (citation.source_url) {
          writeText(citation.source_url, { fontSize: 9, lineHeight: 5, color: [13, 81, 71], link: citation.source_url });
        } else {
          writeText("Aucune URL en ligne disponible pour cette source.", { fontSize: 9, fontStyle: "italic", lineHeight: 5, color: [140, 140, 140] });
        }

        const mirrors = citation.also_available_from || [];
        mirrors.forEach((mirror) => {
          const mirrorLabel = `Également disponible via ${mirror.source_label || mirror.source}`;
          if (mirror.source_url) {
            writeText(mirrorLabel, { fontSize: 8.5, fontStyle: "italic", lineHeight: 4.5, color: [110, 110, 110], link: mirror.source_url });
          } else {
            writeText(mirrorLabel, { fontSize: 8.5, fontStyle: "italic", lineHeight: 4.5, color: [110, 110, 110] });
          }
        });
        y += 3;
      });
    }
  });

  const totalPages = doc.internal.getNumberOfPages();
  const footerY = pageHeight - 10;
  for (let page = 1; page <= totalPages; page++) {
    doc.setPage(page);
    doc.setFont("helvetica", "normal");
    doc.setFontSize(7.5);
    doc.setTextColor(150, 150, 150);
    doc.text("© PivotIQ Solutions · par Nestor Cirhuza Muderhwa · nestor@muderhwa.com", marginX, footerY);
    doc.text(`Page ${page} / ${totalPages}`, pageWidth - marginX, footerY, { align: "right" });
  }

  const safeQuestion = (turns[0].question || "conversation").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/(^-|-$)/g, "").slice(0, 60) || "conversation";
  doc.save(`reference-fiscale-${safeQuestion}.pdf`);
}

function appendInlineMarkdown(parent, value) {
  const text = String(value ?? "");
  const tokenPattern = /(\[[^\]\n]+\]\([^\s)]+\)|\[[^\]\n]+\]|\*\*[^*\n]+\*\*|__[^_\n]+__|`[^`\n]+`|\*[^*\n]+\*|_[^_\n]+_)/g;
  let cursor = 0;

  for (const match of text.matchAll(tokenPattern)) {
    if (match.index > cursor) parent.append(document.createTextNode(text.slice(cursor, match.index)));
    const token = match[0];

    if (token.startsWith("**") || token.startsWith("__")) {
      const strong = document.createElement("strong");
      appendInlineMarkdown(strong, token.slice(2, -2));
      parent.append(strong);
    } else if (token.startsWith("`")) {
      const code = document.createElement("code");
      code.textContent = token.slice(1, -1);
      parent.append(code);
    } else if (token.startsWith("[")) {
      const parts = token.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
      const href = parts?.[2] || "";
      if (parts && (/^https?:\/\//i.test(href) || href.startsWith("/"))) {
        const link = document.createElement("a");
        link.href = href;
        link.target = "_blank";
        link.rel = "noopener";
        appendInlineMarkdown(link, parts[1]);
        parent.append(link);
      } else if (parts) {
        parent.append(document.createTextNode(parts[1]));
      } else {
        // A bare "[Document title, art. N, p. X]" citation reference (not a
        // markdown link) - styled distinctly from body prose so a reader can
        // tell an inline source reference apart from the assistant's own text
        // at a glance, not just by parsing brackets.
        const cite = document.createElement("span");
        cite.className = "citation-ref";
        cite.textContent = token;
        parent.append(cite);
      }
    } else {
      const emphasis = document.createElement("em");
      appendInlineMarkdown(emphasis, token.slice(1, -1));
      parent.append(emphasis);
    }
    cursor = match.index + token.length;
  }

  if (cursor < text.length) parent.append(document.createTextNode(text.slice(cursor)));
}

const TABLE_SEPARATOR_PATTERN = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;

function parseTableRow(line) {
  let trimmed = line.trim();
  if (trimmed.startsWith("|")) trimmed = trimmed.slice(1);
  if (trimmed.endsWith("|")) trimmed = trimmed.slice(0, -1);
  return trimmed.split("|").map((cell) => cell.trim());
}

function renderMarkdown(markdown) {
  const root = document.createElement("div");
  root.className = "answer-text markdown-body";
  const lines = String(markdown ?? "").replace(/\r\n?/g, "\n").split("\n");
  let paragraph = [];
  let activeList = null;
  let activeListTag = "";
  let lastListItem = null;
  let codeLines = null;

  const flushParagraph = () => {
    if (!paragraph.length) return;
    const block = document.createElement("p");
    paragraph.forEach((line, index) => {
      if (index) block.append(document.createElement("br"));
      appendInlineMarkdown(block, line);
    });
    root.append(block);
    paragraph = [];
  };

  const closeList = () => {
    activeList = null;
    activeListTag = "";
    lastListItem = null;
  };

  for (let index = 0; index < lines.length; index++) {
    const line = lines[index];
    if (codeLines !== null) {
      if (/^\s*```/.test(line)) {
        const pre = document.createElement("pre");
        const code = document.createElement("code");
        code.textContent = codeLines.join("\n");
        pre.append(code);
        root.append(pre);
        codeLines = null;
      } else {
        codeLines.push(line);
      }
      continue;
    }

    if (/^\s*```/.test(line)) {
      flushParagraph();
      closeList();
      codeLines = [];
      continue;
    }

    if (!line.trim()) {
      flushParagraph();
      closeList();
      continue;
    }

    if (
      line.includes("|") &&
      index + 1 < lines.length &&
      lines[index + 1].includes("|") &&
      TABLE_SEPARATOR_PATTERN.test(lines[index + 1])
    ) {
      flushParagraph();
      closeList();
      const headerCells = parseTableRow(line);
      const alignments = parseTableRow(lines[index + 1]).map((cell) => {
        const left = cell.startsWith(":");
        const right = cell.endsWith(":");
        if (left && right) return "center";
        if (right) return "right";
        if (left) return "left";
        return "";
      });
      let rowIndex = index + 2;
      const bodyRows = [];
      while (rowIndex < lines.length && lines[rowIndex].includes("|") && lines[rowIndex].trim()) {
        bodyRows.push(parseTableRow(lines[rowIndex]));
        rowIndex++;
      }

      const wrapper = document.createElement("div");
      wrapper.className = "markdown-table-wrap";
      const table = document.createElement("table");
      const thead = document.createElement("thead");
      const headRow = document.createElement("tr");
      headerCells.forEach((cellText, cellIndex) => {
        const th = document.createElement("th");
        if (alignments[cellIndex]) th.style.textAlign = alignments[cellIndex];
        appendInlineMarkdown(th, cellText);
        headRow.append(th);
      });
      thead.append(headRow);
      const tbody = document.createElement("tbody");
      bodyRows.forEach((row) => {
        const tr = document.createElement("tr");
        headerCells.forEach((_, cellIndex) => {
          const td = document.createElement("td");
          if (alignments[cellIndex]) td.style.textAlign = alignments[cellIndex];
          appendInlineMarkdown(td, row[cellIndex] ?? "");
          tr.append(td);
        });
        tbody.append(tr);
      });
      table.append(thead, tbody);
      wrapper.append(table);
      root.append(wrapper);
      index = rowIndex - 1;
      continue;
    }

    const heading = line.match(/^\s*(#{1,4})\s+(.+)$/);
    if (heading) {
      flushParagraph();
      closeList();
      const level = Math.min(heading[1].length + 1, 4);
      const element = document.createElement(`h${level}`);
      appendInlineMarkdown(element, heading[2]);
      root.append(element);
      continue;
    }

    const orderedItem = line.match(/^\s*\d+[.)]\s+(.+)$/);
    const unorderedItem = line.match(/^\s*[-*–—]\s+(.+)$/);
    const listMatch = orderedItem || unorderedItem;
    if (listMatch) {
      flushParagraph();
      const listTag = orderedItem ? "OL" : "UL";
      if (!activeList || activeListTag !== listTag) {
        activeList = document.createElement(listTag.toLowerCase());
        activeListTag = listTag;
        root.append(activeList);
      }
      lastListItem = document.createElement("li");
      appendInlineMarkdown(lastListItem, listMatch[1]);
      activeList.append(lastListItem);
      continue;
    }

    if (activeList && lastListItem && /^\s{2,}\S/.test(line)) {
      lastListItem.append(document.createElement("br"));
      appendInlineMarkdown(lastListItem, line.trim());
      continue;
    }

    closeList();
    paragraph.push(line.trim());
  }

  if (codeLines !== null) {
    const pre = document.createElement("pre");
    const code = document.createElement("code");
    code.textContent = codeLines.join("\n");
    pre.append(code);
    root.append(pre);
  }
  flushParagraph();
  return root;
}

function addErrorMessage(error) {
  const message = document.createElement("div");
  message.className = "message message-error";
  message.textContent = `La recherche a échoué : ${error}`;
  el.messages.append(message);
}

function resetConversation() {
  state.conversationId = null;
  state.previousResponseId = null;
  state.turns = [];
  updateExportButton();
  el.messages.replaceChildren();
  el.welcomePanel.hidden = false;
  el.questionInput.value = "";
  autoResize();
  el.questionInput.focus();
}

async function loadHistory() {
  if (!state.status?.history_enabled) {
    el.historySection.hidden = true;
    return;
  }
  el.historySection.hidden = false;
  try {
    const response = await fetch("/api/history", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    renderHistory(payload.conversations || []);
  } catch (error) {
    el.historyEmpty.hidden = false;
    el.historyEmpty.textContent = `Historique indisponible (${error.message}).`;
  }
}

function renderHistory(conversations) {
  el.historyEmpty.hidden = conversations.length > 0;
  el.historyEmpty.textContent = "Aucune conversation sauvegardée.";
  el.historyList.replaceChildren(...conversations.map((conversation) => {
    const row = document.createElement("div");
    row.className = `history-item${conversation.conversation_id === state.conversationId ? " active" : ""}`;

    const openButton = document.createElement("button");
    openButton.type = "button";
    openButton.className = "history-open";
    const title = document.createElement("strong");
    title.textContent = conversation.title;
    const metadata = document.createElement("small");
    metadata.textContent = `${formatHistoryDate(conversation.updated_at)} · ${Math.ceil((conversation.message_count || 0) / 2)} échange(s)`;
    openButton.append(title, metadata);
    openButton.addEventListener("click", () => openConversation(conversation.conversation_id));

    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "history-delete";
    deleteButton.title = "Supprimer cette conversation";
    deleteButton.setAttribute("aria-label", `Supprimer ${conversation.title}`);
    deleteButton.textContent = "×";
    deleteButton.addEventListener("click", () => deleteConversation(conversation.conversation_id, conversation.title));

    row.append(openButton, deleteButton);
    return row;
  }));
}

async function openConversation(conversationId) {
  try {
    const response = await fetch(`/api/history/${conversationId}`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    state.conversationId = conversationId;
    state.previousResponseId = null;
    el.messages.replaceChildren();
    state.turns = [];
    let lastQuestion = "";
    (payload.messages || []).forEach((message) => {
      if (message.role === "user") {
        addUserMessage(message.content);
        lastQuestion = message.content;
      } else {
        addAssistantMessage(message.content, message.citations || [], message.model || payload.model);
        state.turns.push({
          question: lastQuestion,
          answer: message.content,
          citations: message.citations || [],
          model: message.model || payload.model,
        });
        if (message.response_id) state.previousResponseId = message.response_id;
      }
    });
    updateExportButton();
    el.welcomePanel.hidden = (payload.messages || []).length > 0;
    el.setupPanel.hidden = true;
    renderHistory((await (await fetch("/api/history", { cache: "no-store" })).json()).conversations || []);
    scrollToBottom();
  } catch (error) {
    addErrorMessage(`Impossible d’ouvrir la conversation : ${error.message}`);
  }
}

async function deleteConversation(conversationId, title) {
  if (!window.confirm(`Supprimer définitivement « ${title} » de cet ordinateur ?`)) return;
  try {
    const response = await fetch(`/api/history/${conversationId}`, { method: "DELETE" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    if (state.conversationId === conversationId) resetConversation();
    loadHistory();
  } catch (error) {
    addErrorMessage(`Suppression impossible : ${error.message}`);
  }
}

function formatHistoryDate(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Date inconnue";
  return new Intl.DateTimeFormat("fr-FR", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }).format(date);
}

function updateSendButton() {
  el.sendButton.disabled = state.busy || !el.questionInput.value.trim() || !state.status?.ready_for_questions || !state.selectedSources.size;
}

function autoResize() {
  el.questionInput.style.height = "auto";
  el.questionInput.style.height = `${Math.min(el.questionInput.scrollHeight, 150)}px`;
}

function scrollToBottom() {
  window.requestAnimationFrame(() => window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" }));
}

function abbreviation(label) {
  return label.split(/[\s-]+/).filter(Boolean).map((word) => word[0]).join("").slice(0, 3).toLocaleUpperCase("fr");
}

function formatNumber(value) {
  return new Intl.NumberFormat("fr-FR").format(value || 0);
}

function escapeHTML(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" })[char]);
}
