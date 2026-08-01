/**
 * AI Automation Connector — Content Script  (v5)
 * ==============================================
 * Runs inside ChatGPT / Claude / Gemini / Google Flow / ElevenLabs pages.
 * Receives EXECUTE_TASK from background.js, performs the DOM automation,
 * reports back with TASK_RESULT.
 *
 * v5 CHANGES (Google Flow overhaul):
 *  1. MULTI-VIDEO FIX — every downloaded video is remembered by its unique
 *     media src. After Back, the NEXT un-downloaded thumbnail is clicked
 *     (never the same one twice). Player src is verified before download.
 *  2. APPROVE WATCHER — the Approve popup is checked continuously during
 *     the ENTIRE generation wait (non-blocking: clicked if present,
 *     silently skipped if not). No more blind 20s block.
 *  3. Prompt text now travels to Python with FORCE_PASTE_SUBMIT so Python
 *     sets the OS clipboard itself (page-side clipboard needs focus, which
 *     is exactly what was broken).
 *  4. Grid re-load after Back is waited-for (selector), not a blind sleep.
 */

(function () {
  'use strict';

  // Guard against double-injection (manifest + manual executeScript).
  if (window.__aiConnectorLoaded) return;
  window.__aiConnectorLoaded = true;

  /* ===================================================================== *
   *  CONFIG + DEFAULT SELECTORS (remote JSON from background overrides)
   * ===================================================================== */

  const CONFIG = {
    GENERATION_TIMEOUT_MS: 10 * 60 * 1000,
    ELEVENLABS_GEN_TIMEOUT_MS: 3 * 60 * 1000,     // audio generation cap
    ELEVENLABS_VOICE_WAIT_MS: 60 * 1000,          // hard 1 min wait after picking the voice
    COOLDOWN_AFTER_FOUND_MS: 5000,
    TYPE_DELAY_MS: 15,
    // Flow: once >=1 thumb exists, keep waiting this long for the REST of
    // the wanted videos before proceeding with what we have.
    FLOW_EXTRA_THUMB_WAIT_MS: 90 * 1000,
    FLOW_APPROVE_POLL_MS: 2500,
  };

  const DEFAULT_SELECTORS = {
    chatgpt: {
      input: [
        '#prompt-textarea',
        'div.ProseMirror[contenteditable="true"]',
        'p[data-placeholder="Ask anything"]',
        'xpath://div[@contenteditable="true"]',
      ],
      send: [
        'button[data-testid="send-button"]',
        'button[aria-label="Send prompt"]',
      ],
      stopButton: [
        'button[data-testid="stop-button"]',
        'button[aria-label="Stop streaming"]',
      ],
      generatedImage: [
        'img[alt^="Generated image"]',
        'img[src*="backend-api/estuary/content"]',
      ],
    },
    claude: {
      input: [
        'div.ProseMirror[contenteditable="true"]',
        'div[contenteditable="true"][enterkeyhint]',
        'p[data-placeholder="How can I help you today?"]',
      ],
      send: [
        'button[aria-label="Send message"]',
        'xpath://fieldset//button[last()]',
      ],
      streaming: [
        'button[aria-label="Stop response"]',
        '[data-is-streaming="true"]',
      ],
      downloadButton: [
        'xpath://button[.//span[normalize-space(text())="Download"]]',
        'xpath://a[.//span[normalize-space(text())="Download"]]',
        'a[download][href]',
      ],
      // "Skip" pops up at random moments while Claude works — dismiss it.
      skipButton: [
        'xpath://button[.//span[normalize-space(text())="Skip"]]',
        'xpath://*[self::button or self::a][normalize-space(.)="Skip"]',
      ],
    },
    gemini: {
      input: [
        'div.ql-editor[contenteditable="true"]',
        'rich-textarea div[contenteditable="true"]',
        'div[aria-label="Enter a prompt for Gemini"]',
      ],
      send: [
        'xpath://button[.//mat-icon[@fonticon="arrow_upward"]]',
        'button[aria-label="Send message"]',
      ],
      responseText: [
        'xpath:(//message-content//div[contains(@class,"markdown")])[last()]',
        'message-content .markdown',
      ],
      generating: [
        'xpath://button[.//mat-icon[@fonticon="stop"]]',
        'progress-indicator',
      ],
      // Chip shown once a pasted file has actually attached/uploaded.
      attachment: [
        'uploader-file-preview',
        '[data-test-id="file-preview"]',
        'xpath://div[contains(@class,"file-preview")]',
        'xpath://*[@aria-label="Remove file"]/ancestor::*[2]',
      ],
    },
    flow: {
      // Landing page: "New project" (also matches the Start button variants).
      newProject: [
        'xpath://button[contains(., "New project")]',
        'xpath://i[normalize-space(text())="add_2"]/ancestor::button[1]',
        'xpath://button[contains(., "Start")]',
      ],
      input: [
        'div[data-slate-editor="true"][contenteditable="true"]',
        'div[role="textbox"][data-slate-editor]',
        'xpath://div[@role="textbox" and @contenteditable="true"]',
      ],
      // IMPORTANT: there are TWO buttons whose sr-only label says "Create".
      // One has the add_2 icon (attach media), the other has arrow_forward
      // (actual submit). We must target arrow_forward ONLY.
      create: [
        'xpath://button[.//i[normalize-space(text())="arrow_forward"]]',
        'xpath://i[normalize-space(text())="arrow_forward"]/ancestor::button[1]',
      ],
      approve: [
        'xpath://div[.//i[contains(text(),"check")] and contains(., "Approve")]',
        'xpath://div[text()="Approve"]/parent::div',
        'xpath://button[contains(., "Approve")]',
        'xpath://div[text()="Approve"]',
      ],
      videoThumbs: [
        'video[src*="media.getMediaUrlRedirect"]',
        '[data-testid="virtuoso-item-list"] video',
        'img[src*="MEDIA_URL_TYPE_THUMBNAIL"]',
      ],
      download: [
        'xpath://button[.//i[contains(text(),"download")]]',
        'xpath://button[.//span[contains(text(),"Download")]]',
      ],
      back: [
        'xpath://button[.//i[contains(text(),"arrow_back")]]',
        'xpath://button[.//span[contains(text(),"Back to projects")]]',
        'xpath://button[contains(@aria-label,"Back")]',
      ],
      // The video element inside the OPEN player (largest video on screen).
      playerVideo: [
        'video[src]',
        'video source[src]',
      ],
    },
    // ---- ElevenLabs Text-to-Speech ------------------------------------
    elevenlabs: {
      // Chevron that expands the voice selector panel.
      voicePanelToggle: [
        'xpath://button[.//svg[contains(@class,"lucide-chevron-right")]]',
        'xpath://*[contains(@class,"lucide-chevron-right")]/ancestor::button[1]',
        'xpath://*[contains(@class,"lucide-chevron-right")]/ancestor::*[@role="button"][1]',
      ],
      exploreTab: [
        '[data-testid="tabbed-voice-selector-explore-tab"]',
        'xpath://button[@role="tab" and normalize-space(.)="Explore"]',
      ],
      voiceSearch: [
        'input[placeholder="Start typing to search..."]',
        'xpath://input[contains(@data-agent-id,"search-bar")]',
        'input[aria-label="Start typing to search..."]',
      ],
      // First row in the search results list.
      voiceResult: [
        'xpath:(//*[@role="option"])[1]',
        'xpath:(//*[@data-testid="voice-card"])[1]',
        'xpath:(//div[contains(@class,"w-5") and contains(@class,"h-5")]/ancestor::*[@role="button"])[1]',
        'xpath:(//div[contains(@class,"w-5") and contains(@class,"h-5") and contains(@class,"mx-3")])[1]',
      ],
      modelSelector: [
        '[data-testid="tts-model-selector"]',
        'xpath://button[contains(@aria-label,"Select model")]',
      ],
      // "The most expressive model..." = the Eleven v3 row in the dropdown.
      modelV3: [
        'xpath://p[contains(text(),"most expressive model")]/ancestor::*[@role="menuitem"][1]',
        'xpath://p[contains(text(),"most expressive model")]/ancestor::*[@role="option"][1]',
        'xpath://p[contains(text(),"most expressive model")]/ancestor::div[3]',
        'xpath://*[contains(text(),"Eleven v3")][last()]',
      ],
      input: [
        'div[data-node-view-content-react] div.ProseMirror',
        'div.ProseMirror[contenteditable="true"]',
        'div[contenteditable="true"][role="textbox"]',
        'xpath://div[@contenteditable="true"]',
      ],
      generate: [
        '[data-testid="tts-generate"]',
        'xpath://button[contains(@aria-label,"Generate speech")]',
        'xpath://button[normalize-space(.)="Generate speech"]',
      ],
      // Download arrow (the svg path with the tray + down-arrow shape).
      download: [
        'xpath://button[.//svg[.//path[contains(@d,"M15.1875 11.0625V12.9375")]]]',
        'xpath://path[contains(@d,"M15.1875 11.0625V12.9375")]/ancestor::button[1]',
        'xpath://button[contains(@aria-label,"Download")]',
        'xpath://*[@data-testid="download-button"]',
      ],
      // Present while audio is still being generated.
      generating: [
        'xpath://button[@data-testid="tts-generate" and @data-loading="true"]',
        'xpath://button[contains(@aria-label,"Generate speech") and @data-loading="true"]',
      ],
    },
  };

  let SEL = DEFAULT_SELECTORS;

  function mergeSelectors(remote) {
    if (!remote) return;
    const out = JSON.parse(JSON.stringify(DEFAULT_SELECTORS));
    for (const platform of Object.keys(remote)) {
      if (platform.startsWith('_')) continue;
      out[platform] = out[platform] || {};
      for (const key of Object.keys(remote[platform])) out[platform][key] = remote[platform][key];
    }
    SEL = out;
  }

  /* ===================================================================== *
   *  UTILITIES
   * ===================================================================== */

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const log = (...a) => console.log('%c[AI-Connector]', 'color:#7dd3fc', ...a);
  const logErr = (...a) => console.error('%c[AI-Connector]', 'color:#f87171', ...a);

  function queryOne(candidate) {
    if (!candidate || typeof candidate !== 'string') return null;
    try {
      if (candidate.startsWith('xpath:') || candidate.startsWith('//')) {
        const xp = candidate.replace(/^xpath:/, '');
        return document.evaluate(xp, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
      }
      return document.querySelector(candidate.replace(/^css:/, ''));
    } catch (e) {
      logErr(`queryOne failed on selector: "${candidate}"`, e.message);
      return null;
    }
  }

  function queryAll(candidate) {
    if (!candidate || typeof candidate !== 'string') return [];
    try {
      if (candidate.startsWith('xpath:') || candidate.startsWith('//')) {
        const xp = candidate.replace(/^xpath:/, '');
        const res = document.evaluate(xp, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
        return Array.from({ length: res.snapshotLength }, (_, i) => res.snapshotItem(i));
      }
      return Array.from(document.querySelectorAll(candidate.replace(/^css:/, '')));
    } catch (e) {
      logErr(`queryAll failed on selector: "${candidate}"`, e.message);
      return [];
    }
  }

  function findFirst(candidates) {
    for (const c of candidates || []) { const el = queryOne(c); if (el) return el; }
    return null;
  }

  function findAll(candidates) {
    const seen = new Set(); const out = [];
    for (const c of candidates || []) for (const el of queryAll(c)) if (!seen.has(el)) { seen.add(el); out.push(el); }
    return out;
  }

  /** Visible = attached, has a box, not display:none/visibility:hidden. */
  function isVisible(el) {
    if (!el || !el.isConnected) return false;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    const st = window.getComputedStyle(el);
    return st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0';
  }

  async function waitFor(candidates, { timeout = 30000, interval = 500, label = 'element' } = {}) {
    const start = Date.now();
    while (Date.now() - start < timeout) {
      const el = findFirst(candidates);
      if (el) return el;
      await sleep(interval);
    }
    throw new Error(`Timed out waiting for ${label} (${timeout}ms)`);
  }

  async function waitForGone(candidates, { timeout = 60000, interval = 750, label = 'element' } = {}) {
    const start = Date.now();
    while (Date.now() - start < timeout) {
      if (!findFirst(candidates)) return true;
      await sleep(interval);
    }
    throw new Error(`Timed out waiting for ${label} to disappear`);
  }

  function robustClick(el) {
    if (!el) throw new Error('robustClick: element is null');
    const target = el.closest('button, a, [role="button"]') || el;
    target.scrollIntoView({ block: 'center', behavior: 'instant' });

    const opts = { bubbles: true, cancelable: true, view: window };
    for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
      target.dispatchEvent(new MouseEvent(type, opts));
    }
    // Force the browser's native click as a backup to bypass React pooling.
    try { target.click(); } catch (e) {}
  }

  /** True when a control is genuinely clickable (not disabled/aria-disabled). */
  function isEnabled(el) {
    if (!el) return false;
    const btn = el.closest('button, a, [role="button"]') || el;
    if (btn.disabled) return false;
    if (btn.getAttribute('aria-disabled') === 'true') return false;
    if (btn.getAttribute('data-disabled') === 'true') return false;
    return true;
  }

  /**
   * Wait until a selector resolves AND the element is enabled, then return it.
   * Critical for Google Flow: the arrow_forward submit button ships with
   * aria-disabled="true" and only flips once the editor has real content —
   * clicking it early silently does nothing.
   */
  async function waitEnabled(candidates, { timeout = 30000, interval = 500, label = 'control' } = {}) {
    const start = Date.now();
    let last = null;
    while (Date.now() - start < timeout) {
      const el = findFirst(candidates);
      if (el) {
        last = el;
        if (isEnabled(el)) return el;
      }
      await sleep(interval);
    }
    throw new Error(last
      ? `${label} found but stayed disabled (${timeout}ms)`
      : `Timed out waiting for ${label} (${timeout}ms)`);
  }

  /* ===================================================================== *
   *  TEXT INJECTION (ProseMirror / Quill / Slate safe, emoji-proof)
   * ===================================================================== */

  async function typeIntoEditor(editor, text) {
    editor.focus();
    await sleep(150);

    if (editor.tagName === 'TEXTAREA' || editor.tagName === 'INPUT') {
      const proto = editor.tagName === 'TEXTAREA'
        ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, 'value').set.call(editor, text);
      editor.dispatchEvent(new Event('input', { bubbles: true }));
      editor.dispatchEvent(new Event('change', { bubbles: true }));
      return;
    }

    const lines = String(text).split('\n');
    for (let li = 0; li < lines.length; li++) {
      const words = lines[li].split(' ');
      for (let wi = 0; wi < words.length; wi++) {
        const chunk = wi < words.length - 1 ? words[wi] + ' ' : words[wi];
        if (chunk.length) {
          if (!document.execCommand('insertText', false, chunk)) insertViaInputEvent(editor, chunk);
          await sleep(CONFIG.TYPE_DELAY_MS);
        }
      }
      if (li < lines.length - 1) {
        if (!document.execCommand('insertLineBreak')) insertViaInputEvent(editor, '\n', 'insertLineBreak');
        await sleep(CONFIG.TYPE_DELAY_MS);
      }
    }
    editor.dispatchEvent(new Event('input', { bubbles: true }));
    editor.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function insertViaInputEvent(editor, data, inputType = 'insertText') {
    const before = new InputEvent('beforeinput', { bubbles: true, cancelable: true, inputType, data });
    const after = new InputEvent('input', { bubbles: true, inputType, data });
    editor.dispatchEvent(before);
    if (!before.defaultPrevented && inputType === 'insertText') editor.appendChild(document.createTextNode(data));
    editor.dispatchEvent(after);
  }

  function pressEnter(editor, mods = {}) {
    const opts = {
      bubbles: true, cancelable: true, key: 'Enter', code: 'Enter',
      keyCode: 13, which: 13, ...mods,
    };
    editor.dispatchEvent(new KeyboardEvent('keydown', opts));
    editor.dispatchEvent(new KeyboardEvent('keypress', opts));
    editor.dispatchEvent(new KeyboardEvent('keyup', opts));
  }

  async function submitPrompt(editor, sendSelectors) {
    await sleep(400);
    const btn = findFirst(sendSelectors);
    if (btn && !btn.disabled && btn.getAttribute('aria-disabled') !== 'true') {
      robustClick(btn);
      log('send button clicked');
    } else {
      log('send button not found/disabled → Enter key');
      pressEnter(editor);
    }
  }

  /* ===================================================================== *
   *  STATUS WIDGET (glassmorphism, top-right)
   * ===================================================================== */

  const Widget = (() => {
    let box, dot, label, sub;
    const STATES = {
      connected: { color: '#34d399', text: 'Connected' },
      idle:      { color: '#94a3b8', text: 'Idle' },
      executing: { color: '#38bdf8', text: 'Executing Prompt…' },
      error:     { color: '#f87171', text: 'Error' },
    };
    function mount() {
      if (box && document.contains(box)) return;
      box = document.createElement('div');
      box.style.cssText = `position:fixed;right:16px;top:16px;z-index:2147483647;display:flex;align-items:center;gap:10px;padding:10px 16px;border-radius:14px;background:rgba(17,24,39,.55);backdrop-filter:blur(14px) saturate(160%);-webkit-backdrop-filter:blur(14px) saturate(160%);border:1px solid rgba(255,255,255,.14);box-shadow:0 8px 32px rgba(0,0,0,.35);font:500 13px/1.3 -apple-system,'Segoe UI',Roboto,sans-serif;color:#e5e7eb;pointer-events:none;user-select:none;`;
      dot = document.createElement('span');
      dot.style.cssText = 'width:9px;height:9px;border-radius:50%;flex:none;box-shadow:0 0 8px currentColor;';
      const col = document.createElement('div');
      label = document.createElement('div');
      sub = document.createElement('div');
      sub.style.cssText = 'font-size:11px;opacity:.65;font-weight:400;max-width:240px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;';
      col.append(label, sub);
      box.append(dot, col);
      (document.body || document.documentElement).appendChild(box);
    }
    function set(state, detail = '') {
      try {
        mount();
        const s = STATES[state] || STATES.idle;
        dot.style.background = s.color; dot.style.color = s.color;
        label.textContent = `[${s.text}]`;
        sub.textContent = detail; sub.style.display = detail ? 'block' : 'none';
        chrome.runtime.sendMessage({ type: 'STATUS', text: s.text + (detail ? ' — ' + detail : '') }).catch(() => {});
      } catch (e) { /* never let UI kill automation */ }
    }
    return { set };
  })();

  /* ===================================================================== *
   *  TASK HANDLERS
   * ===================================================================== */

  // ---- ChatGPT image ---------------------------------------------------
  async function handleChatGPTImage(task) {
    Widget.set('executing', 'ChatGPT: typing prompt');
    const existing = new Set(findAll(SEL.chatgpt.generatedImage).map((i) => i.src));

    const editor = await waitFor(SEL.chatgpt.input, { timeout: 30000, label: 'ChatGPT input box' });
    await typeIntoEditor(editor, task.prompt);
    await submitPrompt(editor, SEL.chatgpt.send);

    Widget.set('executing', 'ChatGPT: waiting for image…');
    const deadline = Date.now() + CONFIG.GENERATION_TIMEOUT_MS;
    let img = null;
    while (Date.now() < deadline) {
      img = findAll(SEL.chatgpt.generatedImage).find((i) => i.src && !existing.has(i.src));
      if (img && !findFirst(SEL.chatgpt.stopButton)) break;
      img = null;
      await sleep(2000);
    }
    if (!img) throw new Error('Image not generated within 10 minutes');

    await sleep(CONFIG.COOLDOWN_AFTER_FOUND_MS); // let the asset settle

    Widget.set('executing', 'ChatGPT: downloading image');
    const resp = await fetch(img.src, { credentials: 'include' });
    if (!resp.ok) throw new Error(`Image fetch failed: HTTP ${resp.status}`);
    const blob = await resp.blob();
    const b64 = await blobToBase64(blob);

    const m = location.pathname.match(/\/c\/([a-z0-9-]+)/i);
    return { image_b64: b64, mime: blob.type || 'image/png', src: img.src, alt: img.alt || '', path: task.path, chat_code: m ? m[1] : null, chat_url: location.href };
  }

  function blobToBase64(blob) {
    return new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(String(r.result).split(',')[1]);
      r.onerror = () => reject(new Error('FileReader failed'));
      r.readAsDataURL(blob);
    });
  }

  // ---- Claude PDF ------------------------------------------------------
  async function handleClaudePdf(task) {
    Widget.set('executing', 'Claude: typing prompt');
    const editor = await waitFor(SEL.claude.input, { timeout: 30000, label: 'Claude input box' });
    await typeIntoEditor(editor, task.prompt);
    await submitPrompt(editor, SEL.claude.send);

    Widget.set('executing', 'Claude: waiting for PDF…');
    const deadline = Date.now() + CONFIG.GENERATION_TIMEOUT_MS;
    let dlBtn = null;
    while (Date.now() < deadline) {
      // "Skip" appears at unpredictable moments — click it whenever it shows.
      const skip = findFirst(SEL.claude.skipButton);
      if (skip && isVisible(skip)) {
        log('Skip button detected -> clicking');
        Widget.set('executing', 'Claude: clicking Skip');
        await sleep(1000);           // 1s settle before clicking
        robustClick(skip);
        await sleep(1500);
        continue;
      }
      dlBtn = findFirst(SEL.claude.downloadButton);
      if (dlBtn) break;
      await sleep(2000);
    }
    if (!dlBtn) throw new Error('Claude Download button not found within 10 minutes');

    await sleep(CONFIG.COOLDOWN_AFTER_FOUND_MS); // cool-down before clicking
    try { await waitForGone(SEL.claude.streaming, { timeout: 60000, label: 'Claude streaming' }); }
    catch (e) { log('streaming indicator still present, proceeding'); }

    Widget.set('executing', 'Claude: downloading PDF');
    robustClick(dlBtn);
    await sleep(1200);
    const second = findFirst(SEL.claude.downloadButton);
    if (second && second !== dlBtn) robustClick(second); // menu-style download

    await sleep(3000);
    // background.js tracked the download → exact path goes to Python.
    return { move_from_downloads: true, expect_ext: '.pdf', path: task.path, chat_url: location.href };
  }

  // ---- Gemini ----------------------------------------------------------
  async function handleGemini(task) {
    // background.js already navigated us to /app/<chat_code> if needed.
    Widget.set('executing', 'Gemini: typing prompt');
    const editor = await waitFor(SEL.gemini.input, { timeout: 30000, label: 'Gemini input box' });
    await typeIntoEditor(editor, task.prompt);

    // File attachments: Python copies file → OS clipboard, fires Ctrl+V.
    const paths = (task.paths || []).filter(Boolean).slice(0, 3);
    for (const p of paths) {
      const fileName = p.split(/[\\/]/).pop();
      Widget.set('executing', 'Gemini: pasting ' + fileName);

      const before = findAll(SEL.gemini.attachment).length;
      let attached = false;

      // Up to 3 attempts — clipboard/focus races are common here.
      for (let attempt = 1; attempt <= 3 && !attached; attempt++) {
        editor.focus();
        await sleep(400);                    // focus must settle BEFORE Ctrl+V

        // Python now FORCES the automation Chrome window into the OS
        // foreground itself before pressing Ctrl+V (v5 focus fix).
        const res = await chrome.runtime.sendMessage({ type: 'REQUEST_PASTE', file: p });
        if (!res || !res.ok) throw new Error('File paste failed: ' + (res && res.error));

        await sleep(3000);                   // 3s wait after paste, as specified

        // VERIFY the attachment chip really appeared (upload can be slow).
        const upDeadline = Date.now() + 30000;
        while (Date.now() < upDeadline) {
          if (findAll(SEL.gemini.attachment).length > before) { attached = true; break; }
          await sleep(1000);
        }
        if (!attached) log(`attach attempt ${attempt} failed for ${fileName}, retrying`);
      }

      if (!attached) throw new Error('File never attached in Gemini: ' + fileName);
      log('attached:', fileName);
      await sleep(1500);                     // let the upload finalise
    }

    // Fallback if copyButton is missing in older selectors
    if (!SEL.gemini.copyButton) {
        SEL.gemini.copyButton = ['mat-icon[data-mat-icon-name="copy"]', 'button[aria-label="Copy"]'];
    }

    const beforeCount = findAll(SEL.gemini.responseText).length;
    const beforeCopyCount = findAll(SEL.gemini.copyButton).length;
    
    await submitPrompt(editor, SEL.gemini.send);

    Widget.set('executing', 'Gemini: waiting for response…');
    const deadline = Date.now() + CONFIG.GENERATION_TIMEOUT_MS;
    let textResult = "";

    while (Date.now() < deadline) {
      const allCopyBtns = findAll(SEL.gemini.copyButton);
      const generating = !!findFirst(SEL.gemini.generating);
      
      if (allCopyBtns.length > beforeCopyCount && !generating) {
        const copyBtn = allCopyBtns[allCopyBtns.length - 1];
        
        Widget.set('executing', 'Gemini: clicking Copy button');
        robustClick(copyBtn);
        await sleep(1000); // give it time to hit the OS clipboard
        
        // Route via the service worker: a direct fetch from a content script
        // is cross-origin (page origin -> localhost) and Chrome blocks it.
        let j;
        try {
            j = await chrome.runtime.sendMessage({ type: 'GET_CLIPBOARD' });
        } catch (e) {
            throw new Error('Clipboard bridge unreachable: ' + (e && e.message || e));
        }
        if (j && j.ok) {
            textResult = j.text;
            break;
        }
        throw new Error('Clipboard read failed: ' + ((j && j.error) || 'no response'));
      }
      await sleep(1500);
    }
    
    if (!textResult) throw new Error('Gemini response (copy button) not detected within timeout');

    await sleep(CONFIG.COOLDOWN_AFTER_FOUND_MS);

    const m = location.pathname.match(/\/app\/([a-z0-9]+)/i);
    return {
      response: textResult.trim(),
      chat_code: m ? m[1] : null,
      chat_url: location.href,
      n: task.n ?? null,
      files_pasted: paths.length,
    };
  }

  /* ===================================================================== *
   *  GOOGLE FLOW — v5 helpers
   * ===================================================================== */

  /** Stable identity for a video thumbnail element (survives DOM rebuilds). */
  function thumbKey(el) {
    if (!el) return '';
    const raw = el.currentSrc || el.src || el.getAttribute('src')
             || el.getAttribute('poster') || (el.querySelector && (el.querySelector('source[src]') || {}).src)
             || '';
    try {
      const u = new URL(String(raw), location.href);
      if (u.protocol === 'blob:') return u.href;
      return u.pathname + (u.searchParams.has('name') ? '?name=' + u.searchParams.get('name') : '');
    } catch(e) {
      return String(raw).split('?')[0];
    }
  }

  /** src of the video currently open in the PLAYER (largest visible video). */
  function currentPlayerKey() {
    let best = null, bestArea = 0;
    for (const v of document.querySelectorAll('video')) {
      if (!isVisible(v)) continue;
      const r = v.getBoundingClientRect();
      const area = r.width * r.height;
      if (area > bestArea) { bestArea = area; best = v; }
    }
    return best ? thumbKey(best) : '';
  }

  /** Non-blocking Approve check: click it if visible, return true if clicked.
   *  NEVER waits — this is polled inside the generation loop instead. */
  function clickApproveIfPresent() {
    const approve = findFirst(SEL.flow.approve);
    if (approve && isVisible(approve)) {
      log('Approve popup detected → clicking');
      Widget.set('executing', 'Flow: Approve popup → clicking');
      try { robustClick(approve); return true; } catch (e) { logErr('approve click failed', e); }
    }
    return false;
  }

  /** Click Back (UI button preferred, history fallback), then WAIT for the
   *  thumbnail grid to actually re-render before returning. */
  async function goBackToGrid() {
    const backBtn = findFirst(SEL.flow.back);
    if (backBtn && isVisible(backBtn)) {
      robustClick(backBtn);
      log('clicked UI Back button');
    } else {
      log('UI Back button not found → history.back()');
      history.back();
    }
    // Don't blind-sleep: wait for the grid thumbs to exist again.
    try {
      await waitFor(SEL.flow.videoThumbs, { timeout: 20000, label: 'Flow grid after Back' });
    } catch (e) {
      log('grid did not reappear after Back — continuing anyway');
    }
    await sleep(2000); // settle: virtualised list finishes mounting
  }

  // ---- Google Flow video (v5 REWRITE) ---------------------------------
  async function handleFlowVideo(task) {
    Widget.set('executing', 'Flow: opening project');
    const np = findFirst(SEL.flow.newProject);
    if (np) {
      robustClick(np);
      await sleep(5000);
    }

    // 1. Local clipboard copy is now only a FALLBACK — the authoritative
    //    copy happens in Python (works even when this window has no focus).
    try { await navigator.clipboard.writeText(task.prompt); }
    catch (e) {
      try {
        const textArea = document.createElement('textarea');
        textArea.value = task.prompt;
        textArea.style.position = 'fixed';
        textArea.style.opacity = '0';
        document.body.appendChild(textArea);
        textArea.focus();
        textArea.select();
        document.execCommand('copy');
        document.body.removeChild(textArea);
      } catch (e2) { log('local clipboard fallback failed (Python will set it)'); }
    }
    await sleep(500);

    const editor = await waitFor(SEL.flow.input, { timeout: 60000, label: 'Flow prompt box' });
    robustClick(editor);
    editor.focus();

    Widget.set('executing', 'Flow: OS-level paste (Python forces window focus)');

    // 2. Keep caret in the editor while Python re-focuses the OS window.
    const keepFocusInterval = setInterval(() => {
      try { editor.focus(); } catch (e) {}
    }, 100);

    // 3. v5: send the PROMPT TEXT along — Python sets the OS clipboard
    //    itself, forces this Chrome window into the foreground, THEN
    //    presses Ctrl+V (+Enter). Fixes the "window not focused" bug.
    const res = await chrome.runtime.sendMessage({
      type: 'FORCE_PASTE_SUBMIT',
      text: task.prompt,
    });
    clearInterval(keepFocusInterval);
    if (!res || !res.ok) throw new Error('Python hotkey failed: ' + (res && res.error));

    await sleep(3000);

    Widget.set('executing', 'Flow: confirming submit');

    // 4. Click the Create button via JS (Enter from Python may already have
    //    submitted — clicking a disabled/absent Create is harmless).
    try {
      const createBtn = await waitEnabled(SEL.flow.create, { timeout: 15000, label: 'Flow Create button' });
      robustClick(createBtn);
      log('Create button clicked');
    } catch (e) {
      log('Create button not found/disabled (Enter probably submitted already)');
    }

    // 5. v5 GENERATION WAIT + CONTINUOUS APPROVE WATCHER
    //    The Approve popup can appear at ANY moment during generation.
    //    We never block waiting for it — every poll cycle checks:
    //      a) Approve visible? → click it.
    //      b) Enough thumbnails? → done.
    Widget.set('executing', 'Flow: generating video (watching for Approve)…');

    const wanted = Math.max(1, parseInt(task.n, 10) || 1);
    const deadline = Date.now() + CONFIG.GENERATION_TIMEOUT_MS;
    let thumbs = [];
    let firstThumbAt = 0;
    let approveClicks = 0;
    let ignoreApprove = false;

    while (Date.now() < deadline) {
      if (!ignoreApprove && clickApproveIfPresent()) {
        approveClicks++;
        if (approveClicks > 3) {
          log('Approve loop detected - ignoring future approve popups');
          ignoreApprove = true;
        } else {
          await sleep(2500);          // let the dialog dismiss + generation resume
          continue;
        }
      }

      thumbs = findAll(SEL.flow.videoThumbs);

      if (thumbs.length >= wanted) break;                 // all videos ready

      if (thumbs.length >= 1) {
        // Some (not all) videos are ready — give the rest a bounded grace
        // window instead of bailing with just one.
        if (!firstThumbAt) firstThumbAt = Date.now();
        if (Date.now() - firstThumbAt > CONFIG.FLOW_EXTRA_THUMB_WAIT_MS) {
          log(`only ${thumbs.length}/${wanted} videos after grace window → proceeding`);
          break;
        }
      }
      await sleep(CONFIG.FLOW_APPROVE_POLL_MS);
    }
    if (!thumbs.length) throw new Error('No generated videos appeared within 10 minutes');
    log(`generation done: ${thumbs.length} thumb(s), wanted ${wanted}, approve clicked ${approveClicks}x`);

    await sleep(CONFIG.COOLDOWN_AFTER_FOUND_MS);

    // 6. v5 DIRECT VIDEO DOWNLOAD (NO MORE FLAKY UI CLICKING)
    //    We extract the direct URL (`src`) right off the `<video>` elements
    //    and ping the background script to invoke `chrome.downloads`.
    // Deduplicate thumbs to avoid React virtual DOM dupes
    const uniqueThumbs = [];
    const seenKeys = new Set();
    for (const t of thumbs) {
      const k = thumbKey(t);
      if (k && !seenKeys.has(k)) {
        seenKeys.add(k);
        uniqueThumbs.push(t);
      }
    }
    
    const count = Math.min(wanted, uniqueThumbs.length);
    let downloaded = 0;
    const uploadedFilenames = []; // <--- 1. ADD THIS ARRAY TO TRACK UPLOADS

    for (let i = 0; i < count; i++) {
      const thumb = uniqueThumbs[i];
      let src = thumb.currentSrc || thumb.src || thumb.getAttribute('src');
      if (!src && thumb.querySelector) {
        const s = thumb.querySelector('source[src]');
        if (s) src = s.src;
      }
      
      if (src) {
        try {
          const u = new URL(src, location.href);
          src = u.href;
        } catch(e) {}
        
        Widget.set('executing', `Flow: downloading video ${downloaded + 1}/${count}`);
        log(`Directly downloading: ${src.slice(-60)}`);
        
        try {
          const response = await fetch(src);
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          const blob = await response.blob();
          
          const reader = new FileReader();
          reader.readAsDataURL(blob);
          await new Promise(resolve => {
              reader.onloadend = async () => {
                  const b64 = reader.result;
                  const filename = `flow_video_${Date.now()}_${downloaded}.mp4`;
                  
                  const res = await new Promise(r => chrome.runtime.sendMessage({ 
                    type: 'UPLOAD_TO_BRIDGE', 
                    b64: b64, 
                    filename: filename,
                    target_path: task.path,
                    is_multiple: count > 1,
                    download_index: downloaded + 1
                  }, r));
                  
                  if (!res || !res.ok) {
                    logErr(`Bridge upload failed: ${(res && res.error) || 'unknown error'}`);
                  } else {
                    // THE BULLETPROOF FIX:
                    // Extract the exact absolute path Python saved the file to.
                    const exactSavedPath = res.path || res.filepath || res.saved_to || res.full_path || res.file || filename;
                    
                    uploadedFilenames.push(exactSavedPath); 
                    log(`Tracked uploaded file at: ${exactSavedPath}`);
                  }
                  resolve();
              };
          });
          
          downloaded++;
          await sleep(1000);
        } catch (err) {
          logErr(`Direct background download failed for ${src}`, err);
        }
      } else {
        logErr(`Could not find src for thumbnail ${i + 1}`);
      }
    }

    if (!downloaded) throw new Error('Could not download any generated video');
    if (downloaded < wanted) log(`WARNING: downloaded ${downloaded}/${wanted} videos`);

    return {
      downloaded_count: downloaded,
      wanted_count: wanted,
      approve_clicks: approveClicks,
      move_from_downloads: false,
      download_files: uploadedFilenames, // <--- 3. PASS ARRAY TO PYTHON
      saved_to: uploadedFilenames,       // <--- 4. SATISFY PYTHON'S 'saved_to' CHECK
      expect_ext: '.mp4',
      path: task.path,
      project_url: location.href,
    };
  }

  // ---- ElevenLabs ------------------------------------------------------
  async function handleElevenLabs(task) {
    const voice = (task.voice || 'bunty').trim();

    Widget.set('executing', '11Labs: opening voice panel');
    const chevron = findFirst(SEL.elevenlabs.voicePanelToggle);
    if (chevron) {
      robustClick(chevron);
      await sleep(1500);
    }

    try {
      const explore = await waitFor(SEL.elevenlabs.exploreTab, { timeout: 15000, label: 'Explore tab' });
      robustClick(explore);
      await sleep(1500);
    } catch (e) { }

    Widget.set('executing', '11Labs: searching voice "' + voice + '"');
    const search = await waitFor(SEL.elevenlabs.voiceSearch, { timeout: 20000, label: 'voice search box' });
    robustClick(search);
    await sleep(300);
    await typeIntoEditor(search, voice);
    await sleep(3000);

    try {
      const result = await waitFor(SEL.elevenlabs.voiceResult, { timeout: 20000, label: 'voice result' });
      robustClick(result);
    } catch (e) {
      throw new Error(`Voice "${voice}" not found in Explore results`);
    }

    Widget.set('executing', '11Labs: Voice selected, waiting 10s...');
    await sleep(10000);

    Widget.set('executing', '11Labs: selecting Eleven v3');
    try {
      const modelBtn = await waitFor(SEL.elevenlabs.modelSelector, { timeout: 15000, label: 'model selector' });
      robustClick(modelBtn);
      await sleep(1500);

      const v3 = await waitFor(SEL.elevenlabs.modelV3, { timeout: 10000, label: 'Eleven v3 option' });
      robustClick(v3);

      Widget.set('executing', '11Labs: Model selected, waiting 5s...');
      await sleep(5000);
    } catch (e) { }

    Widget.set('executing', '11Labs: clearing & typing text');
    const editor = await waitFor(SEL.elevenlabs.input, { timeout: 20000, label: 'TTS text editor' });
    robustClick(editor);
    await sleep(400);

    editor.focus();
    // Dispatch Ctrl+A for Prosemirror explicitly
    editor.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', ctrlKey: true, bubbles: true }));
    await sleep(100);
    document.execCommand('selectAll', false, null);
    document.execCommand('delete', false, null);
    await sleep(500);

    await typeIntoEditor(editor, task.prompt);
    await sleep(1000);

    Widget.set('executing', '11Labs: generating speech');
    try {
      const gen = await waitFor(SEL.elevenlabs.generate, { timeout: 20000, label: 'Generate speech button' });
      robustClick(gen);
    } catch (e) {
      editor.focus();
      pressEnter(editor, { ctrlKey: true });
    }

    Widget.set('executing', '11Labs: waiting for audio...');
    const deadline = Date.now() + CONFIG.ELEVENLABS_GEN_TIMEOUT_MS;
    let dl = null;
    while (Date.now() < deadline) {
      if (!findFirst(SEL.elevenlabs.generating)) {
        dl = findFirst(SEL.elevenlabs.download);
        if (dl) break;
      }
      await sleep(2000);
    }
    if (!dl) throw new Error('Audio not generated within 3 minutes');

    await sleep(CONFIG.COOLDOWN_AFTER_FOUND_MS);

    Widget.set('executing', '11Labs: downloading audio');
    robustClick(dl);
    await sleep(4000);

    return {
      move_from_downloads: true,
      expect_ext: '.mp3',
      path: task.path,
      voice,
      page_url: location.href,
    };
  }

  /* ===================================================================== *
   *  MESSAGE ROUTER
   * ===================================================================== */

  const HANDLERS = {
    img: handleChatGPTImage,
    pdf: handleClaudePdf,
    gemini: handleGemini,
    video: handleFlowVideo,
    tts: handleElevenLabs,
  };
  let busy = false;

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg.type === 'PING') { sendResponse({ ok: true }); return; }

    if (msg.type === 'EXECUTE_TASK') {
      sendResponse({ ok: true, accepted: !busy }); // ack immediately
      if (busy) return;
      busy = true;
      (async () => {
        const task = msg.task;
        mergeSelectors(msg.selectors);
        try {
          const handler = HANDLERS[task.type];
          if (!handler) throw new Error('Unknown task type: ' + task.type);
          const data = await handler(task);
          await chrome.runtime.sendMessage({ type: 'TASK_RESULT', id: task.id, ok: true, taskType: task.type, data });
          Widget.set('idle', 'Task ' + task.id + ' done ✔');
        } catch (err) {
          logErr('task failed:', err);
          Widget.set('error', err.message);
          // ALWAYS report the error back — never swallow it.
          try {
            await chrome.runtime.sendMessage({ type: 'TASK_RESULT', id: task.id, ok: false, taskType: task.type, error: String(err.message || err) });
          } catch (e2) { logErr('could not report error:', e2); }
        } finally {
          busy = false;
        }
      })();
      return;
    }
  });

  Widget.set('connected', location.hostname);
  log('content script ready on', location.hostname);
})();
