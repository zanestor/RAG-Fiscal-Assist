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

function isPublicDemo() {
  return window.location.hostname.endsWith("github.io") || new URLSearchParams(window.location.search).get("demo") === "1";
}

const CONVERSATION_ID_PATTERN = /^[a-f0-9]{32}$/;

function conversationIdFromPath() {
  const id = window.location.pathname.slice(1);
  return CONVERSATION_ID_PATTERN.test(id) ? id : null;
}

function setConversationPath(conversationId) {
  const path = conversationId ? `/${conversationId}` : "/";
  if (window.location.pathname !== path) window.history.pushState({ conversationId }, "", path);
}

document.addEventListener("DOMContentLoaded", () => {
  ["modelBadge", "connectionStatus", "documentCount", "indexMeter", "indexSummary", "sourceFilters",
    "toggleSources", "setupPanel", "setupTitle", "setupMessage", "setupSteps", "welcomePanel", "messages",
    "chatForm", "questionInput", "sendButton", "newChat", "exportConversation", "historySection", "historyList",
    "historyEmpty", "refreshHistory"].forEach((id) => { el[id] = document.getElementById(id); });
  bindEvents();
  loadStatus();
});

window.addEventListener("popstate", () => {
  const conversationId = conversationIdFromPath();
  if (conversationId) openConversation(conversationId, { updatePath: false });
  else resetConversation({ updatePath: false });
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
  el.exportConversation.addEventListener("click", async () => {
    if (el.exportConversation.disabled) return;
    el.exportConversation.disabled = true;
    el.exportConversation.setAttribute("aria-busy", "true");
    try {
      await exportConversationToPDF(state.turns);
    } catch (error) {
      console.error("PDF export failed", error);
      addErrorMessage(`La génération du PDF a échoué (${error.message}).`);
    } finally {
      el.exportConversation.disabled = false;
      el.exportConversation.removeAttribute("aria-busy");
    }
  });
}

function updateExportButton() {
  el.exportConversation.hidden = state.turns.length === 0;
}

async function loadStatus() {
  if (isPublicDemo()) {
    renderPublicDemo();
    return;
  }
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.status = await response.json();
    renderStatus();
    loadHistory();
    const conversationId = conversationIdFromPath();
    if (conversationId) openConversation(conversationId, { updatePath: false });
  } catch (error) {
    el.connectionStatus.className = "connection-status error";
    el.connectionStatus.innerHTML = "<i></i> Serveur indisponible";
    showSetup("Serveur indisponible", `Impossible de lire l’état du serveur (${error.message}).`, []);
  }
}

function renderPublicDemo() {
  state.status = { ready_for_questions: false, searchable: 0, sources: [
    { id: "fiscal", label: "Fiscalité RDC", description: "Impôts, TVA et obligations déclaratives", enabled: true },
    { id: "social", label: "Travail & sécurité sociale", description: "Emploi, paie et protection sociale", enabled: true },
    { id: "public", label: "Administration publique", description: "Marchés publics et réglementation", enabled: true },
    { id: "legal", label: "Textes juridiques", description: "Lois, décrets et ordonnances", enabled: true },
  ] };
  el.modelBadge.textContent = "Aperçu public";
  el.connectionStatus.className = "connection-status ready";
  el.connectionStatus.innerHTML = "<i></i> Démonstration publique";
  el.documentCount.textContent = "RDC";
  el.indexMeter.style.width = "100%";
  el.indexSummary.textContent = "Interface de recherche documentaire";
  renderSources(state.status.sources);
  showSetup(
    "Aperçu public de l’interface",
    "Cette page présente l’expérience de recherche sans exposer de documents, de base de données ou de clés privées.",
    ["Explorez les cas d’usage proposés.", "La recherche en direct reste disponible dans l’installation privée.", "Les sources et citations apparaissent uniquement dans l’espace autorisé."],
  );
  setChatEnabled(false);
}

function renderStatus() {
  const status = state.status;
  const providerLabel = status.active_provider === "openai" ? "Recherche assistée" : "Recherche de secours";
  el.modelBadge.textContent = providerLabel;
  el.documentCount.textContent = "RDC";
  const searchable = status.searchable ?? status.indexed;
  el.indexMeter.style.width = status.ready_for_questions ? "100%" : searchable ? "62%" : "0%";
  el.indexSummary.textContent = status.ready_for_questions
    ? "Recherche documentaire active"
    : searchable
      ? "Référentiel en préparation"
      : "Sources en attente de préparation";
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
    button.innerHTML = `<span class="source-abbr">${escapeHTML(abbreviation(source.label))}</span><span><strong>${escapeHTML(source.label)}</strong><small>Source documentaire</small></span><span class="check">✓</span>`;
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
    if (state.conversationId) setConversationPath(state.conversationId);
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
  meta.innerHTML = `<strong>Référence fiscale</strong><span>·</span><span>Réponse documentée</span>`;
  const text = renderMarkdown(answer);
  wireCitationLinks(text, citations);
  content.append(meta, text);

  if (citations.length) {
    const verification = document.createElement("aside");
    verification.className = "verification-card";
    const institutions = new Set(citations.map((citation) => citation.source_label).filter(Boolean)).size;
    const citationLabel = citations.length === 1 ? "1 source citée" : `${citations.length} sources citées`;
    const institutionLabel = institutions === 1 ? "1 institution" : `${institutions} institutions`;
    verification.innerHTML = `<div class="verification-heading"><span class="verification-icon" aria-hidden="true">🛡️</span><span><strong>Fiche de vérification</strong><small>Réponse appuyée par les références affichées</small></span></div><div class="verification-items"><span>📌 ${citationLabel}</span><span>🏛️ ${institutionLabel}</span><span>⚠️ Vérifier l’entrée en vigueur</span></div>`;
    content.append(verification);

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

const CITATION_MATCH_STOPWORDS = new Set([
  "du", "de", "la", "le", "et", "en", "un", "une", "des", "les", "aux", "sur", "par",
  "ou", "au", "ce", "ces", "que", "qui", "son", "sa", "ses", "dans", "art", "page",
]);

function normalizeForCitationMatch(value) {
  return String(value ?? "")
    .toLowerCase()
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

function citationMatchTokens(value) {
  return normalizeForCitationMatch(value)
    .split(" ")
    .filter((token) => token.length >= 3 && !CITATION_MATCH_STOPWORDS.has(token));
}

// Inline "[Document title, art. N, Page X]" references are free text the model
// composes from the title/locator it was given - not a stable key - so matching
// against the citation list is fuzzy: score each citation by how many of its
// significant title/locator words appear in the inline reference, and only link
// when a clear majority match, to avoid pointing a citation at the wrong document.
function bestCitationMatch(referenceText, citations) {
  const normalizedRef = normalizeForCitationMatch(referenceText);
  let best = null;
  let bestScore = 0;
  citations.forEach((citation) => {
    const candidates = [citation.title, ...(citation.locators || [])].filter(Boolean);
    candidates.forEach((candidate) => {
      const tokens = citationMatchTokens(candidate);
      if (!tokens.length) return;
      const matched = tokens.filter((token) => normalizedRef.includes(token)).length;
      const score = matched / tokens.length;
      if (score > bestScore) {
        bestScore = score;
        best = citation;
      }
    });
  });
  return bestScore >= 0.5 ? best : null;
}

function wireCitationLinks(container, citations) {
  if (!citations || !citations.length) return;
  container.querySelectorAll(".citation-ref").forEach((span) => {
    const referenceText = span.textContent.replace(/^\[|\]$/g, "");
    const citation = bestCitationMatch(referenceText, citations);
    if (!citation || !citation.pdf_url) return;
    const link = document.createElement("a");
    link.className = "citation-ref citation-ref-link";
    link.href = citation.pdf_url;
    link.target = "_blank";
    link.rel = "noopener";
    link.title = `Ouvrir : ${citation.title}`;
    link.textContent = span.textContent;
    span.replaceWith(link);
  });
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

const PDF_FONTS = Object.freeze({
  body: "PublicSans",
  display: "Spectral",
  mono: "IBMPlexMono",
});

const PDF_FONT_ASSETS = Object.freeze([
  { path: "fonts/PublicSans-Regular.ttf", fileName: "PublicSans-Regular.ttf", family: PDF_FONTS.body, style: "normal" },
  { path: "fonts/PublicSans-Bold.ttf", fileName: "PublicSans-Bold.ttf", family: PDF_FONTS.body, style: "bold" },
  { path: "fonts/PublicSans-Italic.ttf", fileName: "PublicSans-Italic.ttf", family: PDF_FONTS.body, style: "italic" },
  { path: "fonts/Spectral-Bold.ttf", fileName: "Spectral-Bold.ttf", family: PDF_FONTS.display, style: "bold" },
  { path: "fonts/IBMPlexMono-Medium.ttf", fileName: "IBMPlexMono-Medium.ttf", family: PDF_FONTS.mono, style: "normal" },
]);

let pdfFontAssetsPromise = null;

function arrayBufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  const chunkSize = 0x8000;
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
  }
  return btoa(binary);
}

function loadPdfFontAssets() {
  if (!pdfFontAssetsPromise) {
    pdfFontAssetsPromise = Promise.all(PDF_FONT_ASSETS.map(async (asset) => {
      const response = await fetch(new URL(asset.path, document.baseURI), { cache: "force-cache" });
      if (!response.ok) throw new Error(`police PDF indisponible : ${asset.fileName} (HTTP ${response.status})`);
      return { ...asset, data: arrayBufferToBase64(await response.arrayBuffer()) };
    })).catch((error) => {
      pdfFontAssetsPromise = null;
      throw error;
    });
  }
  return pdfFontAssetsPromise;
}

function registerPdfFonts(doc, assets) {
  assets.forEach((asset) => {
    doc.addFileToVFS(asset.fileName, asset.data);
    doc.addFont(asset.fileName, asset.family, asset.style);
  });
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
async function exportConversationToPDF(turns) {
  const { jsPDF } = window.jspdf || {};
  if (!jsPDF) {
    addErrorMessage("La génération du PDF a échoué : bibliothèque jsPDF introuvable.");
    return;
  }
  if (!turns.length) return;

  const fontAssets = await loadPdfFontAssets();
  const doc = new jsPDF({ unit: "mm", format: "a4", putOnlyUsedFonts: true, compress: true });
  registerPdfFonts(doc, fontAssets);
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
    const { fontSize, fontFamily = PDF_FONTS.body, fontStyle = "normal", lineHeight, color = [17, 17, 17], link, spacingAfter = 0 } = options;
    doc.setFont(fontFamily, fontStyle);
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

  // Shared by writeRichText() (paragraphs) and the table cell renderer below:
  // splits text into words tagged with whether they fall inside a bare
  // "[...]" citation reference, so both can color citations distinctly from
  // surrounding prose using the same rule the chat view uses.
  const tokenizeRichText = (text) => {
    const segments = [];
    let lastIndex = 0;
    let match;
    CITATION_TOKEN_PATTERN.lastIndex = 0;
    while ((match = CITATION_TOKEN_PATTERN.exec(text)) !== null) {
      if (match.index > lastIndex) segments.push({ text: text.slice(lastIndex, match.index), isCitation: false });
      segments.push({ text: match[0], isCitation: true });
      lastIndex = match.index + match[0].length;
    }
    if (lastIndex < text.length) segments.push({ text: text.slice(lastIndex), isCitation: false });
    const tokens = [];
    segments.forEach((segment) => {
      segment.text.split(/(\s+)/).forEach((word) => {
        if (word.length) tokens.push({ text: word, isCitation: segment.isCitation });
      });
    });
    return tokens;
  };

  const setRichTextTokenFont = (token, fontFamily, fontStyle) => {
    doc.setFont(token.isCitation ? PDF_FONTS.mono : fontFamily, token.isCitation ? "normal" : fontStyle);
  };

  // writeText() colors a whole wrapped line uniformly, which can't set an
  // inline citation apart from the prose around it - this lays text out word
  // by word instead, switching color for tokens inside [...] while still
  // wrapping at maxWidth like a normal paragraph.
  const writeRichText = (text, options = {}) => {
    const { fontSize, fontFamily = PDF_FONTS.body, fontStyle = "normal", lineHeight, color = [17, 17, 17], spacingAfter = 0 } = options;
    doc.setFont(fontFamily, fontStyle);
    doc.setFontSize(fontSize);

    String(text ?? "").split("\n").forEach((paragraph) => {
      if (!paragraph) {
        ensureSpace(lineHeight);
        y += lineHeight;
        return;
      }
      const tokens = tokenizeRichText(paragraph);
      let cursorX = marginX;
      ensureSpace(lineHeight);
      tokens.forEach((token) => {
        // Font MUST be set before measuring, same reasoning as writeText() above:
        // citation glyphs use IBM Plex Mono while prose uses Public Sans, so
        // measuring with whichever font was active for the previous token would
        // make the citation visibly crowd into whatever follows it.
        setRichTextTokenFont(token, fontFamily, fontStyle);
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

  // Wraps citation-aware tokens into lines that each fit boxWidth, given the
  // font size/style already active on doc. Pure layout (no drawing) so the
  // table renderer can measure every cell's row height before committing to
  // a page-break decision.
  const wrapTokensToWidth = (tokens, boxWidth, fontFamily, fontStyle) => {
    const lines = [];
    let current = [];
    let cursorWidth = 0;
    tokens.forEach((token) => {
      setRichTextTokenFont(token, fontFamily, fontStyle);
      const width = doc.getTextWidth(token.text);
      const isSpace = !token.text.trim();
      // Never start a wrapped line with a leading space, but otherwise keep
      // space tokens in the line (with their width) same as writeRichText()
      // does - drawTokenLines() below relies on that gap actually being
      // present to separate words when it draws them.
      if (isSpace && current.length === 0) return;
      if (!isSpace && current.length > 0 && cursorWidth + width > boxWidth) {
        lines.push(current);
        current = [];
        cursorWidth = 0;
      }
      current.push({ ...token, width });
      cursorWidth += width;
    });
    if (current.length) lines.push(current);
    return lines;
  };

  // Draws pre-wrapped citation-aware lines starting at (x, y), honoring cell
  // alignment; returns nothing, just advances the caller's own y bookkeeping
  // (the table renderer tracks row height itself, since every cell in a row
  // must share one row height regardless of which cell is tallest).
  const drawTokenLines = (lines, x, boxWidth, startY, lineHeight, align, fontFamily, fontStyle, color) => {
    lines.forEach((line, lineIndex) => {
      const lineWidth = line.reduce((sum, token) => sum + token.width, 0);
      let cursorX = x;
      if (align === "center") cursorX = x + (boxWidth - lineWidth) / 2;
      else if (align === "right") cursorX = x + boxWidth - lineWidth;
      line.forEach((token) => {
        setRichTextTokenFont(token, fontFamily, fontStyle);
        doc.setTextColor(...(token.isCitation ? CITATION_REF_COLOR : color));
        doc.text(token.text, cursorX, startY + lineIndex * lineHeight);
        cursorX += token.width;
      });
    });
  };

  // Renders a markdown table block as an actual bordered/shaded table,
  // matching the chat view's .markdown-body table styling (dark green header,
  // zebra-striped rows) rather than leaving raw "| a | b |" pipe syntax in
  // the exported text, which is unreadable outside a markdown renderer.
  const writeMarkdownTable = (table, options = {}) => {
    const { fontSize = 8.5, lineHeight = 3.6, spacingAfter = 6 } = options;
    const columns = table.headers.length;
    if (!columns) return;
    const cellPaddingX = 2;
    const cellPaddingY = 1.6;
    const colWidth = maxWidth / columns;
    const HEADER_FILL = [13, 81, 71];
    const HEADER_TEXT = [255, 255, 255];
    const ZEBRA_FILL = [230, 240, 235];
    const BODY_TEXT = [40, 40, 40];
    const BORDER_COLOR = [214, 224, 218];

    doc.setFontSize(fontSize);
    const layoutRow = (cells, fontFamily, fontStyle) =>
      cells.map((cellText, cellIndex) => {
        const plain = markdownToPlainText(cellText || "").replace(/\n/g, " ");
        const tokens = tokenizeRichText(plain);
        const lines = wrapTokensToWidth(tokens, colWidth - cellPaddingX * 2, fontFamily, fontStyle);
        return { lines: lines.length ? lines : [[]], align: table.alignments[cellIndex] || "left" };
      });

    const drawRow = (cells, rowFill, fontFamily, fontStyle, textColor) => {
      const rowHeight = Math.max(...cells.map((cell) => cell.lines.length)) * lineHeight + cellPaddingY * 2;
      ensureSpace(rowHeight);
      if (rowFill) {
        doc.setFillColor(...rowFill);
        doc.rect(marginX, y, maxWidth, rowHeight, "F");
      }
      doc.setDrawColor(...BORDER_COLOR);
      cells.forEach((cell, cellIndex) => {
        const cellX = marginX + cellIndex * colWidth;
        doc.rect(cellX, y, colWidth, rowHeight);
        drawTokenLines(
          cell.lines, cellX + cellPaddingX, colWidth - cellPaddingX * 2,
          y + cellPaddingY + lineHeight * 0.8, lineHeight, cell.align, fontFamily, fontStyle, textColor
        );
      });
      y += rowHeight;
      return rowHeight;
    };

    const headerCells = layoutRow(table.headers, PDF_FONTS.mono, "normal");
    ensureSpace(Math.max(...headerCells.map((cell) => cell.lines.length)) * lineHeight + cellPaddingY * 2 + 4);
    drawRow(headerCells, HEADER_FILL, PDF_FONTS.mono, "normal", HEADER_TEXT);

    table.rows.forEach((row, rowIndex) => {
      const bodyCells = layoutRow(table.headers.map((_, cellIndex) => row[cellIndex] ?? ""), PDF_FONTS.body, "normal");
      const rowHeight = Math.max(...bodyCells.map((cell) => cell.lines.length)) * lineHeight + cellPaddingY * 2;
      // ensureSpace() alone would silently start a new page mid-table with no
      // header - repeat it here so a table split across pages stays readable.
      if (y + rowHeight > bottomLimit) {
        doc.addPage();
        y = 20;
        drawRow(headerCells, HEADER_FILL, PDF_FONTS.mono, "normal", HEADER_TEXT);
      }
      drawRow(bodyCells, rowIndex % 2 === 1 ? ZEBRA_FILL : null, PDF_FONTS.body, "normal", BODY_TEXT);
    });

    y += spacingAfter;
  };

  writeText("Référence Fiscale RDC", { fontSize: 18, fontFamily: PDF_FONTS.display, fontStyle: "bold", lineHeight: 7, color: [13, 81, 71] });
  const generatedAt = new Intl.DateTimeFormat("fr-FR", { dateStyle: "long", timeStyle: "short" }).format(new Date());
  writeText(`Conversation exportée le ${generatedAt} · ${turns.length} échange(s)`, { fontSize: 9, fontFamily: PDF_FONTS.mono, lineHeight: 5, color: [90, 90, 90], spacingAfter: 8 });

  turns.forEach((turn, turnIndex) => {
    if (turnIndex > 0) {
      ensureSpace(14);
      doc.setDrawColor(220, 227, 221);
      doc.line(marginX, y, pageWidth - marginX, y);
      y += 8;
    }

    writeText(`Question ${turnIndex + 1}`, { fontSize: 8, fontFamily: PDF_FONTS.mono, lineHeight: 4, color: [36, 116, 102], spacingAfter: 1 });
    writeText(turn.question || "", { fontSize: 11, fontStyle: "bold", lineHeight: 5.5, spacingAfter: 6 });

    splitMarkdownIntoBlocks(turn.answer).forEach((block) => {
      if (block.type === "table") {
        writeMarkdownTable(block);
      } else if (block.type === "heading") {
        const fontSize = block.level === 2 ? 15 : block.level === 3 ? 13 : 12;
        writeText(markdownToPlainText(block.content), {
          fontSize,
          fontFamily: PDF_FONTS.display,
          fontStyle: "bold",
          lineHeight: fontSize * 0.48,
          color: [13, 81, 71],
          spacingAfter: 2,
        });
      } else if (block.type === "code") {
        writeText(block.content, { fontSize: 9, fontFamily: PDF_FONTS.mono, lineHeight: 4.6, color: [23, 60, 53], spacingAfter: 4 });
      } else {
        writeRichText(markdownToPlainText(block.content), { fontSize: 10.5, lineHeight: 5.2, spacingAfter: 4 });
      }
    });
    y += 4;

    const citations = turn.citations || [];
    if (citations.length) {
      ensureSpace(10);
      writeText("Sources citées", { fontSize: 12, fontFamily: PDF_FONTS.display, fontStyle: "bold", lineHeight: 6, color: [13, 81, 71], spacingAfter: 4 });

      citations.forEach((citation, index) => {
        writeText(`${index + 1}. ${citation.title || ""}`, { fontSize: 10, fontStyle: "bold", lineHeight: 5 });

        const labelText = `${citation.source_label || ""}${citation.category ? ` · ${citation.category}` : ""}`;
        if (labelText.trim()) writeText(labelText, { fontSize: 9, lineHeight: 5, color: [85, 85, 85] });

        // Deliberately the ONLINE source, not the app's local /documents/{id} route:
        // an exported PDF is meant to be read outside this app, so its links must
        // still resolve there. When no online URL exists, say so rather than
        // silently linking to the local route.
        if (citation.source_url) {
          writeText(citation.source_url, { fontSize: 9, fontFamily: PDF_FONTS.mono, lineHeight: 5, color: [13, 81, 71], link: citation.source_url });
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
    doc.setFont(PDF_FONTS.mono, "normal");
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

// Splits the answer into the same typography roles used in the chat view:
// Spectral headings, IBM Plex Mono code/table headers, Public Sans prose, and
// real table structures instead of raw "| a | b |" pipe syntax.
function splitMarkdownIntoBlocks(markdown) {
  const lines = String(markdown ?? "").replace(/\r\n?/g, "\n").split("\n");
  const blocks = [];
  let buffer = [];
  let codeBuffer = null;
  const flush = () => {
    if (buffer.length) {
      blocks.push({ type: "text", content: buffer.join("\n") });
      buffer = [];
    }
  };
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index];
    if (codeBuffer !== null) {
      if (/^\s*```/.test(line)) {
        blocks.push({ type: "code", content: codeBuffer.join("\n") });
        codeBuffer = null;
      } else {
        codeBuffer.push(line);
      }
      continue;
    }
    if (/^\s*```/.test(line)) {
      flush();
      codeBuffer = [];
      continue;
    }
    if (
      line.includes("|") &&
      index + 1 < lines.length &&
      lines[index + 1].includes("|") &&
      TABLE_SEPARATOR_PATTERN.test(lines[index + 1])
    ) {
      flush();
      const headerCells = parseTableRow(line);
      const alignments = parseTableRow(lines[index + 1]).map((cell) => {
        const left = cell.startsWith(":");
        const right = cell.endsWith(":");
        if (left && right) return "center";
        if (right) return "right";
        if (left) return "left";
        return "left";
      });
      let rowIndex = index + 2;
      const bodyRows = [];
      while (rowIndex < lines.length && lines[rowIndex].includes("|") && lines[rowIndex].trim()) {
        bodyRows.push(parseTableRow(lines[rowIndex]));
        rowIndex++;
      }
      blocks.push({ type: "table", headers: headerCells, alignments, rows: bodyRows });
      index = rowIndex - 1;
      continue;
    }
    const heading = line.match(/^\s*(#{1,4})\s+(.+)$/);
    if (heading) {
      flush();
      blocks.push({ type: "heading", level: Math.min(heading[1].length + 1, 4), content: heading[2] });
      continue;
    }
    buffer.push(line);
  }
  if (codeBuffer !== null) blocks.push({ type: "code", content: codeBuffer.join("\n") });
  flush();
  return blocks;
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

function resetConversation({ updatePath = true } = {}) {
  state.conversationId = null;
  state.previousResponseId = null;
  state.turns = [];
  updateExportButton();
  el.messages.replaceChildren();
  el.welcomePanel.hidden = false;
  el.questionInput.value = "";
  autoResize();
  el.questionInput.focus();
  if (updatePath) setConversationPath(null);
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

async function openConversation(conversationId, { updatePath = true } = {}) {
  try {
    const response = await fetch(`/api/history/${conversationId}`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    state.conversationId = conversationId;
    if (updatePath) setConversationPath(conversationId);
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
