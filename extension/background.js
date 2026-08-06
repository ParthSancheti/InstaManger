/**
 * AI Automation Connector — Background Service Worker (MV3)  (v5)
 * ===============================================================
 * The "brain" of the extension. Opens/manages tabs, long-polls the Python
 * bridge, hands tasks to content.js, tracks downloads, reports results.
 *
 * v5 CHANGES:
 *  - FORCE_PASTE_SUBMIT now forwards the PROMPT TEXT to Python, so Python
 *    sets the OS clipboard itself (page-side clipboard fails when the
 *    window has no focus — the exact bug being fixed).
 *  - Both paste handshakes un-minimize the window (state:'normal') before
 *    focusing; Python then FORCES OS-level foreground before Ctrl+V.
 *
 * Robust against MV3 service-worker suspension: state lives in
 * chrome.storage.session, an alarm watchdog restarts the poll loop, and
 * results are handled purely event-driven (a message wakes the worker).
 */

'use strict';

/* ======================================================================= *
 *  CONFIG
 * ======================================================================= */

const SERVER = 'http://localhost:5000';
// Self-healing selectors. PINNED to an immutable commit SHA — never 'main'.
// 'main' would let anyone with repo push access steer clicks/typing on your
// logged-in ChatGPT / Claude / Gemini sessions. Bump this SHA deliberately.
const SELECTOR_SHA = '767ec7a8d62b726523dc5914d3843e435bdaeb79';
const SELECTOR_URL =
  `https://raw.githubusercontent.com/ParthSancheti/InstaManger/${SELECTOR_SHA}/selectors.json`;
const SELECTOR_KEYS = ['chatgpt', 'claude', 'gemini', 'flow', 'elevenlabs'];

const TASK_TIMEOUT_MIN = 16;          // hard kill-switch per task (must be > Python's 15m max timeout)
const LONG_POLL_S = 25;               // server holds /get-task this long

const PLATFORMS = {
  chatgpt: { url: 'https://chatgpt.com/',                 match: ['*://chatgpt.com/*', '*://chat.openai.com/*'] },
  claude:  { url: 'https://claude.ai/new',                match: ['*://claude.ai/*'] },
  gemini:  { url: 'https://gemini.google.com/app',        match: ['*://gemini.google.com/*'] },
  flow:    { url: 'https://labs.google/fx/tools/flow',    match: ['*://labs.google/*'] },
  elevenlabs: {
    url: 'https://elevenlabs.io/app/speech-synthesis/text-to-speech',
    match: ['*://elevenlabs.io/*'],
  },
};

const TYPE_TO_PLATFORM = {
  img: 'chatgpt',
  pdf: 'claude',
  gemini: 'gemini',
  video: 'flow',
  tts: 'elevenlabs',
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => console.log('[AI-Connector/bg]', ...a);

/* ======================================================================= *
 *  SESSION STATE (survives service-worker restarts)
 * ======================================================================= */

async function getState() {
  const s = await chrome.storage.session.get({ busy: false, task: null, tabId: null, downloads: [] });
  return s;
}
async function setState(patch) { await chrome.storage.session.set(patch); }

/* ======================================================================= *
 *  BADGE (visual status on the extension icon)
 * ======================================================================= */

function badge(text, color) {
  try {
    chrome.action.setBadgeText({ text });
    chrome.action.setBadgeBackgroundColor({ color });
  } catch (e) { /* cosmetic only */ }
}

/* ======================================================================= *
 *  POLL LOOP (long-poll keeps the worker alive while waiting)
 * ======================================================================= */

let polling = false;

async function pollLoop() {
  if (polling) return;
  polling = true;
  log('poll loop started');
  while (true) {
    try {
      const { busy } = await getState();
      if (busy) { await sleep(3000); continue; }

      const res = await fetch(`${SERVER}/get-task?wait=${LONG_POLL_S}`);
      const json = await res.json();
      badge('•', '#34d399'); // connected

      if (json && json.task) {
        await startTask(json.task);
      }
    } catch (e) {
      badge('!', '#f87171'); // bridge offline
      await sleep(3000);
    }
  }
}

// Watchdog: if Chrome suspended the worker, this alarm revives the loop.
chrome.alarms.create('watchdog', { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === 'watchdog') pollLoop();
  if (alarm.name === 'task-timeout') await failCurrentTask('Task timed out (' + TASK_TIMEOUT_MIN + ' min hard limit)');
});
chrome.runtime.onStartup.addListener(pollLoop);
chrome.runtime.onInstalled.addListener(pollLoop);
pollLoop(); // also on every worker wake

/* ======================================================================= *
 *  TAB MANAGEMENT
 * ======================================================================= */

/** Find an existing tab for the platform, or create one. Focus it.
 *  Also DEDUPES: if the same site got opened twice (Python launch + extension
 *  create racing each other), the extras are closed. And when reusing a tab
 *  we reset it to the platform's clean URL so a new task never continues
 *  inside the previous conversation. */
async function ensureTab(platform, targetUrl) {
  const spec = PLATFORMS[platform];
  let tabs = await chrome.tabs.query({ url: spec.match });

  // DEDUPE — keep the active tab (the one Python just opened to steal focus), close the rest.
  if (tabs.length > 1) {
    let target = tabs.find(t => t.active) || tabs[tabs.length - 1];
    log(`found ${tabs.length} ${platform} tabs -> keeping active tab ${target.id}`);
    for (const extra of tabs) {
      if (extra.id !== target.id) {
        try { await chrome.tabs.remove(extra.id); } catch (e) {}
      }
    }
    tabs = [target];
  }

  let tab = tabs[0];
  if (!tab) {
    log(`no ${platform} tab -> creating`);
    tab = await chrome.tabs.create({ url: targetUrl || spec.url, active: true });
    await waitTabComplete(tab.id);
  } else {
    // Always (re)navigate to a clean starting URL
    const dest = targetUrl || spec.url;
    // If the tab is already at the destination, avoid a hard reload 
    // which can cause infinite 'loading' status in SPAs like ElevenLabs.
    if (tab.url === dest || tab.url.startsWith(dest + '#')) {
      log(`reusing ${platform} tab -> already at dest, activating only`);
      await chrome.tabs.update(tab.id, { active: true });
      await sleep(1500);
    } else {
      log(`reusing ${platform} tab -> resetting to ${dest}`);
      await chrome.tabs.update(tab.id, { url: dest, active: true });
      await waitTabComplete(tab.id);
    }
  }

  // Bring the whole window to front — un-minimize FIRST, then focus.
  try {
    await chrome.windows.update(tab.windowId, { state: 'normal' });
    await chrome.windows.update(tab.windowId, { focused: true });
  } catch (e) {}
  return tab;
}

/** After a task finishes: park the tab on google.com, then close it, so
 *  nothing is left holding the old conversation open. */
async function cleanupTab(tabId) {
  if (!tabId) return;
  try {
    const tab = await chrome.tabs.get(tabId);                                  // still exists?
    
    // If this is the only tab left in the window, open a spare one
    // so that removing it doesn't cause Chrome to exit entirely.
    const allTabs = await chrome.tabs.query({ windowId: tab.windowId });
    if (allTabs.length <= 1) {
      await chrome.tabs.create({ windowId: tab.windowId, url: 'chrome://newtab/', active: false });
    }
    
    try {
      await chrome.scripting.executeScript({
        target: { tabId: tabId },
        func: () => { window.onbeforeunload = null; }
      });
    } catch(e) {}
    
    await chrome.tabs.remove(tabId);
    log('task tab closed cleanly');
  } catch (e) {
    log('cleanupTab skipped:', e.message);
  }
}

/** Resolve when the tab finishes loading (status: complete) + settle time. */
function waitTabComplete(tabId, timeoutMs = 60000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { cleanup(); reject(new Error('Tab load timeout')); }, timeoutMs);
    function listener(id, info) {
      if (id === tabId && info.status === 'complete') { cleanup(); setTimeout(resolve, 3000); }
    }
    function cleanup() { clearTimeout(timer); chrome.tabs.onUpdated.removeListener(listener); }
    chrome.tabs.onUpdated.addListener(listener);
    // Already complete? (race guard)
    chrome.tabs.get(tabId).then((t) => {
      if (t.status === 'complete') { cleanup(); setTimeout(resolve, 1500); }
    }).catch(() => {});
  });
}

/** Ping the content script; inject it manually if it's not there yet. */
async function ensureContentScript(tabId) {
  try {
    await chrome.tabs.sendMessage(tabId, { type: 'PING' });
  } catch (e) {
    log('content script missing → injecting');
    await chrome.scripting.executeScript({ target: { tabId }, files: ['content.js'] });
    await sleep(1000);
  }
}

/* ======================================================================= *
 *  SELECTORS (remote fetch with cache fallback — self-healing)
 * ======================================================================= */

function validSelectors(j) {
  if (!j || typeof j !== 'object' || Array.isArray(j)) return false;
  return SELECTOR_KEYS.every((k) => j[k] && typeof j[k] === 'object');
}

async function bundledSelectors() {
  try {
    const r = await fetch(chrome.runtime.getURL('selectors.json'));
    const j = await r.json();
    return validSelectors(j) ? j : null;
  } catch (e) {
    return null;
  }
}

async function loadSelectors() {
  try {
    const res = await fetch(SELECTOR_URL);          // SHA-pinned => immutable
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const json = await res.json();
    if (!validSelectors(json)) throw new Error('shape validation failed');
    await chrome.storage.local.set({ selectors: json });
    log('remote selectors loaded \u2714');
    return json;
  } catch (e) {
    log('selector fetch rejected (' + e.message + ') \u2192 cache \u2192 bundled');
    const { selectors } = await chrome.storage.local.get('selectors');
    if (validSelectors(selectors)) return selectors;
    return await bundledSelectors();                // never returns partial data
  }
}

/* ======================================================================= *
 *  TASK LIFECYCLE
 * ======================================================================= */

async function startTask(task) {
  try {
    if (task.type === 'close_browser') {
      log('received close_browser task -> closing all tabs to gracefully exit Chrome');
      const tabs = await chrome.tabs.query({});
      for (const t of tabs) {
        await chrome.tabs.remove(t.id).catch(() => {});
      }
      return; // Chrome dies, worker dies
    }

    await setState({ busy: true, task, downloads: [] });
    chrome.alarms.create('task-timeout', { delayInMinutes: TASK_TIMEOUT_MIN });
    badge('▶', '#38bdf8');

    const platform = TYPE_TO_PLATFORM[task.type];
    if (!platform) throw new Error('Unknown task type: ' + task.type);

    // Gemini and ChatGPT chat_code resume → background handles the navigation itself.
    let targetUrl = null;
    if (task.type === 'gemini' && task.chat_code) {
      targetUrl = `https://gemini.google.com/app/${task.chat_code}`;
    } else if (task.type === 'img' && task.chat_code) {
      targetUrl = `https://chatgpt.com/c/${task.chat_code}`;
    }

    const tab = await ensureTab(platform, targetUrl);
    await setState({ tabId: tab.id });
    await ensureContentScript(tab.id);

    const selectors = await loadSelectors();
    // Fire-and-forget: content.js reports back via TASK_RESULT message.
    await chrome.tabs.sendMessage(tab.id, { type: 'EXECUTE_TASK', task, selectors, platform });
  } catch (e) {
    await failCurrentTask('startTask: ' + (e.message || e));
  }
}

async function failCurrentTask(errorMsg) {
  const { busy, task } = await getState();
  if (!busy || !task) return;
  log('task failed:', errorMsg);
  await finishTask({ id: task.id, ok: false, type: task.type, error: errorMsg });
}

/** Common exit path: attach exact download paths, POST to Python, reset. */
async function finishTask(result) {
  try {
    if (result.ok && result.data && result.data.move_from_downloads) {
      result.data.download_files = await collectDownloads();
    }
  } catch (e) {
    result.ok = false;
    result.error = 'download tracking failed: ' + e.message;
  }

  try {
    await fetch(SERVER + '/submit-result', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(result),
    });
  } catch (e) {
    log('could not reach bridge to submit result:', e.message);
  }

  // Park + close the task tab so the next task starts fresh.
  try {
    const { tabId } = await getState();
    await cleanupTab(tabId);
  } catch (e) { log('cleanup failed:', e.message); }

  chrome.alarms.clear('task-timeout');
  await setState({ busy: false, task: null, tabId: null, downloads: [] });
  badge('•', '#34d399');
}

/* ======================================================================= *
 *  DOWNLOAD TRACKING (exact absolute file paths — no more guessing)
 * ======================================================================= */

chrome.downloads.onCreated.addListener(async (item) => {
  const { busy, downloads } = await getState();
  if (!busy) return;
  log('download started during task:', item.id);
  downloads.push(item.id);
  await setState({ downloads });
});

/** Wait for every tracked download to complete; return absolute paths. */
async function collectDownloads(timeoutMs = 3 * 60 * 1000) {
  const { downloads } = await getState();
  if (!downloads.length) return [];
  const deadline = Date.now() + timeoutMs;
  const files = [];
  for (const id of downloads) {
    while (Date.now() < deadline) {
      const [item] = await chrome.downloads.search({ id });
      if (item && item.state === 'complete') { files.push(item.filename); break; }
      if (item && item.state === 'interrupted') throw new Error('Download interrupted: ' + (item.error || ''));
      await sleep(1500);
    }
  }
  if (!files.length) throw new Error('Downloads did not complete in time');
  return files;
}

/* ======================================================================= *
 *  WINDOW FOCUS HELPER (paste handshakes)
 * ======================================================================= */

async function bringTabToFront(sender) {
  if (!sender.tab) return;
  try {
    await chrome.windows.update(sender.tab.windowId, { state: 'normal' }); // un-minimize
    await chrome.tabs.update(sender.tab.id, { active: true });
    await chrome.windows.update(sender.tab.windowId, { focused: true, drawAttention: true });
    await sleep(500);
  } catch (e) { log('bringTabToFront:', e.message); }
}

/* ======================================================================= *
 *  MESSAGES FROM content.js
 * ======================================================================= */

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    try {
      switch (msg.type) {
        case 'GET_TABS_STATUS': {
          let status = {};
          for (const [pf, spec] of Object.entries(PLATFORMS)) {
            let tabs = await chrome.tabs.query({ url: spec.match });
            status[pf] = tabs.length > 0;
          }
          sendResponse(status);
          return; // return so sendResponse can work asynchronously if needed, but since we await, we must return true from the listener! Wait, the listener itself doesn't return true unless we return true at the end of the outer block.
        }
        case 'GET_CLIPBOARD': {
          // Proxied through the service worker on purpose. Content scripts run
          // in the page's origin and are subject to CORS; the worker has
          // host_permissions and is not. Never fetch the bridge from content.js.
          try {
            const r = await fetch(SERVER + '/get-clipboard');
            if (!r.ok) throw new Error('HTTP ' + r.status);
            const j = await r.json();
            sendResponse(j);
          } catch (e) {
            sendResponse({ ok: false, error: String(e && e.message || e) });
          }
          return;
        }
        case 'TASK_RESULT': {
          // Purely event-driven: works even if the worker restarted mid-task.
          await finishTask({ id: msg.id, ok: msg.ok, type: msg.taskType, data: msg.data, error: msg.error });
          sendResponse({ ok: true });
          break;
        }
        case 'REQUEST_PASTE': {
          // Focus the browser window first; Python then FORCES OS foreground
          // itself (v5) before firing clipboard + Ctrl+V.
          await bringTabToFront(sender);
          const res = await fetch(SERVER + '/request-paste', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ file: msg.file }),
          });
          sendResponse(await res.json());
          break;
        }
        case 'FORCE_PASTE_SUBMIT': {
          // v5: forward the prompt TEXT — Python sets the OS clipboard
          // itself (reliable regardless of page focus), forces the window
          // to the foreground, then presses Ctrl+V + Enter.
          await bringTabToFront(sender);
          const res = await fetch(SERVER + '/force-paste-submit', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text: msg.text || null }),
          });
          sendResponse(await res.json());
          break;
        }
        case 'UPLOAD_TO_BRIDGE': {
          const res = await fetch(SERVER + '/upload-direct', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ 
                filename: msg.filename, 
                b64: msg.b64,
                target_path: msg.target_path,
                is_multiple: msg.is_multiple,
                download_index: msg.download_index
            }),
          });
          sendResponse(await res.json());
          break;
        }
        case 'STATUS': {
          // Content script status updates → mirror on the badge tooltip.
          try { chrome.action.setTitle({ title: 'AI Connector: ' + msg.text }); } catch (e) {}
          sendResponse({ ok: true });
          break;
        }
        default:
          sendResponse({ ok: false, error: 'unknown message ' + msg.type });
      }
    } catch (e) {
      sendResponse({ ok: false, error: String(e.message || e) });
    }
  })();
  return true; // keep the message channel open for the async response
});
