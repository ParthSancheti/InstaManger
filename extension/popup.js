document.addEventListener('DOMContentLoaded', () => {
  const badge = document.getElementById('server-badge');
  const taskCount = document.getElementById('task-count');
  const lastPoll = document.getElementById('last-poll');
  
  const ais = ['chatgpt', 'gemini', 'claude', 'flow', 'elevenlabs'];

  function updateCard(id, state, text) {
    const card = document.getElementById('card-' + id);
    if (!card) return;
    const txt = card.querySelector('.status-text');
    card.className = 'ai-card';
    if (state === 'live') card.classList.add('live');
    if (state === 'error') card.classList.add('error');
    txt.textContent = text;
  }

  function fetchStatus() {
    fetch('http://127.0.0.1:5000/status')
      .then(r => r.json())
      .then(data => {
        badge.textContent = 'SERVER ONLINE';
        badge.className = 'badge online';
        taskCount.textContent = (data.queue_size || 0).toString();
        lastPoll.textContent = data.last_poll_age_s !== null ? data.last_poll_age_s + 's ago' : 'Live';

        chrome.runtime.sendMessage({ type: "GET_TABS_STATUS" }, (res) => {
          if (chrome.runtime.lastError || !res) {
            ais.forEach(ai => updateCard(ai, 'error', 'No Tab Found'));
            return;
          }
          ais.forEach(ai => {
            if (res[ai]) {
              updateCard(ai, 'live', 'Connected');
            } else {
              updateCard(ai, 'error', 'Missing Tab');
            }
          });
        });
      })
      .catch(err => {
        badge.textContent = 'SERVER OFFLINE';
        badge.className = 'badge offline';
        taskCount.textContent = '-';
        lastPoll.textContent = '-';
        ais.forEach(ai => updateCard(ai, 'normal', 'Waiting for Server'));
      });
  }

  fetchStatus();
  setInterval(fetchStatus, 2000);
});
