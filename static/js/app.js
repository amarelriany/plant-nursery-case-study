/**
 * app.js — Storefront Planter Prospecting Engine
 *
 * Orchestrates UI interaction by polling state from backend background worker.
 */

// ─── DOM REFS ────────────────────────────────────────────────────────────────
const inputCoverage   = document.getElementById('input-coverage');
const inputTypes      = document.getElementById('input-types');
const inputTarget     = document.getElementById('input-target');
const planterOptions  = document.querySelectorAll('.planter-option');

const btnStart        = document.getElementById('btn-start');
const btnPause        = document.getElementById('btn-pause');
const statusBadge     = document.getElementById('status-badge');
const queueBadge      = document.getElementById('queue-badge');

const stepDiscover    = document.getElementById('step-discover');
const stepCapture     = document.getElementById('step-capture');
const stepComposite   = document.getElementById('step-composite');

const resultsList     = document.getElementById('results-list');
const resultsCount    = document.getElementById('results-count');
const activeResultContainer = document.getElementById('active-result-container');

const rejectedList    = document.getElementById('rejected-list');
const rejectedCount   = document.getElementById('rejected-count');
const rejectedDetails = document.getElementById('rejected-details');
const errorToast      = document.getElementById('error-toast');
const cardTemplate    = document.getElementById('venue-card-template');

// ─── STATE ───────────────────────────────────────────────────────────────────
let selectedPlanterId = '1';
let activeResultIndex = null;
let lastResultsCount = 0;

// ─── ERROR TOAST ──────────────────────────────────────────────────────────────
let toastTimer;
function showError(msg) {
  errorToast.textContent = msg;
  errorToast.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => errorToast.classList.remove('show'), 6000);
}

// ─── API HELPERS ──────────────────────────────────────────────────────────────
async function apiPost(path, body = {}) {
  const resp = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await resp.json();
  if (!resp.ok || data.error) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

async function apiGet(path) {
  const resp = await fetch(path);
  const data = await resp.json();
  if (!resp.ok || data.error) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

// ─── PLANTER SELECTION ───────────────────────────────────────────────────────
planterOptions.forEach(opt => {
  opt.addEventListener('click', () => {
    planterOptions.forEach(o => o.classList.remove('active'));
    opt.classList.add('active');
    selectedPlanterId = opt.dataset.id;
  });
});

// ─── PIPELINE STEP STATES ────────────────────────────────────────────────────
function updatePipelineSteps(currentStep, status) {
  if (status !== 'running') {
    [stepDiscover, stepCapture, stepComposite].forEach(s => {
      s.dataset.state = 'idle';
    });
    return;
  }

  switch (currentStep) {
    case 'discover':
      stepDiscover.dataset.state = 'active';
      stepDiscover.querySelector('.step-detail').textContent = 'Searching…';
      stepCapture.dataset.state = 'idle';
      stepCapture.querySelector('.step-detail').textContent = 'Quality Check';
      stepComposite.dataset.state = 'idle';
      stepComposite.querySelector('.step-detail').textContent = 'QA check';
      break;

    case 'capture':
      stepDiscover.dataset.state = 'done';
      stepDiscover.querySelector('.step-detail').textContent = 'Completed ✓';
      stepCapture.dataset.state = 'active';
      stepCapture.querySelector('.step-detail').textContent = 'Checking frontage…';
      stepComposite.dataset.state = 'idle';
      stepComposite.querySelector('.step-detail').textContent = 'QA check';
      break;

    case 'identity_check':
      stepDiscover.dataset.state = 'done';
      stepDiscover.querySelector('.step-detail').textContent = 'Completed ✓';
      stepCapture.dataset.state = 'active';
      stepCapture.querySelector('.step-detail').textContent = 'Verifying branding…';
      stepComposite.dataset.state = 'idle';
      stepComposite.querySelector('.step-detail').textContent = 'QA check';
      break;

    case 'composite':
      stepDiscover.dataset.state = 'done';
      stepDiscover.querySelector('.step-detail').textContent = 'Completed ✓';
      stepCapture.dataset.state = 'done';
      stepCapture.querySelector('.step-detail').textContent = 'Passed ✓';
      stepComposite.dataset.state = 'active';
      stepComposite.querySelector('.step-detail').textContent = 'Compositing…';
      break;

    case 'qa_check':
      stepDiscover.dataset.state = 'done';
      stepDiscover.querySelector('.step-detail').textContent = 'Completed ✓';
      stepCapture.dataset.state = 'done';
      stepCapture.querySelector('.step-detail').textContent = 'Passed ✓';
      stepComposite.dataset.state = 'active';
      stepComposite.querySelector('.step-detail').textContent = 'Running QA…';
      break;

    default: // 'idle' or unknown
      [stepDiscover, stepCapture, stepComposite].forEach(s => {
        s.dataset.state = 'idle';
      });
      stepDiscover.querySelector('.step-detail').textContent = 'Storefront Found';
      stepCapture.querySelector('.step-detail').textContent = 'Quality Check';
      stepComposite.querySelector('.step-detail').textContent = 'QA check';
  }
}

// ─── COMPARISON SLIDER ───────────────────────────────────────────────────────
function initComparisonSlider(container, beforeB64, afterB64) {
  container.innerHTML = `
    <div class="img-layer img-after">
      <img class="after-img" alt="Venue entrance with planters composited in" />
    </div>
    <div class="img-layer img-before">
      <img class="before-img" alt="Venue entrance original Street View" />
    </div>
    <div class="comparison-divider"></div>
    <input
      type="range"
      class="comparison-slider"
      min="0" max="100" value="50"
      aria-label="Drag to compare before and after"
    />
    <div class="comparison-labels">
      <span class="label-before">Before</span>
      <span class="label-after">After</span>
    </div>
  `;

  const beforeImg = container.querySelector('.before-img');
  const afterImg  = container.querySelector('.after-img');
  const slider    = container.querySelector('.comparison-slider');
  const divider   = container.querySelector('.comparison-divider');
  const beforeLayer = container.querySelector('.img-before');

  beforeImg.src = `data:image/jpeg;base64,${beforeB64}`;
  afterImg.src  = `data:image/png;base64,${afterB64}`;

  function updatePosition(val) {
    const pct = val + '%';
    beforeLayer.style.clipPath = `inset(0 ${100 - val}% 0 0)`;
    divider.style.left = pct;
  }

  updatePosition(50);

  slider.addEventListener('input', () => updatePosition(Number(slider.value)));

  let dragging = false;
  container.addEventListener('mousedown', () => { dragging = true; });
  window.addEventListener('mouseup', () => { dragging = false; });
  container.addEventListener('mousemove', e => {
    if (!dragging) return;
    const rect = container.getBoundingClientRect();
    const pct = Math.max(0, Math.min(100, ((e.clientX - rect.left) / rect.width) * 100));
    slider.value = pct;
    updatePosition(pct);
  });

  container.addEventListener('touchmove', e => {
    e.preventDefault();
    const rect = container.getBoundingClientRect();
    const touch = e.touches[0];
    const pct = Math.max(0, Math.min(100, ((touch.clientX - rect.left) / rect.width) * 100));
    slider.value = pct;
    updatePosition(pct);
  }, { passive: false });
}

// ─── RENDER ACTIVE VIEW ──────────────────────────────────────────────────────
function renderActiveItem(item) {
  activeResultContainer.innerHTML = '';
  if (!item) {
    activeResultContainer.innerHTML = `
      <div class="empty-state">
        <h3>No storefront selected</h3>
        <p>Configure the agent above and click Start. Visited prospects will appear in the list on the left.</p>
      </div>
    `;
    return;
  }

  if (item.type === 'processing') {
    activeResultContainer.innerHTML = `
      <div class="viewer-processing-state">
        <div class="spinner"></div>
        <h3>Processing ${item.venue.name}</h3>
        <p>${item.step_detail}</p>
      </div>
    `;
    return;
  }

  if (item.type === 'rejected') {
    activeResultContainer.innerHTML = `
      <div class="viewer-rejected-state">
        <div class="rejected-badge-icon" style="color: var(--text-muted); font-size: 1.25rem; letter-spacing: 0.05em;">EXCLUDED</div>
        <h3 style="font-weight: 400;">${item.venue.name}</h3>
        <p class="venue-address">${item.venue.address || ''}</p>
        <p style="margin-top: 0.5rem; color: var(--text-secondary);">Reason: ${item.reason}</p>
      </div>
    `;
    return;
  }

  // Success item (standard rendering)
  const node = cardTemplate.content.cloneNode(true);
  const card = node.querySelector('.venue-card');

  card.querySelector('.venue-name').textContent = item.venue.name;
  card.querySelector('.venue-address').textContent = item.venue.address;

  card.querySelector('#score-capture .score-value').textContent = item.score || '—';
  card.querySelector('#score-composite .score-value').textContent = item.score || '—';

  const footerSource = card.querySelector('.source-tag');
  footerSource.textContent =
    item.source === 'streetview' ? 'Google Street View' :
    item.source === 'places_photo' ? 'Google Places Photo' : 'No image';

  const qualityNote = card.querySelector('.quality-note');
  qualityNote.textContent = item.reason || '';

  const compContainer = card.querySelector('.comparison-container');
  initComparisonSlider(compContainer, item.original_b64, item.composited_b64);

  activeResultContainer.appendChild(node);
}

// ─── RENDER SIDEBAR LIST ─────────────────────────────────────────────────────
function renderResultsList(combinedItems) {
  resultsList.innerHTML = '';
  // Count total successful visited prospects
  const successCount = combinedItems.filter(i => i.type === 'success').length;
  resultsCount.textContent = successCount;

  combinedItems.forEach((item) => {
    const li = document.createElement('li');
    
    if (selectedItemId === item.id) {
      li.className = 'active';
    }
    if (item.type === 'processing') {
      li.classList.add('processing');
    } else if (item.type === 'rejected') {
      li.classList.add('rejected-item');
    }

    let badgeText = '';
    let statusDetailText = '';
    if (item.type === 'processing') {
      badgeText = 'PROCESSING';
      statusDetailText = `<div class="history-item-addr" style="color: var(--text-secondary);">${item.step_detail}</div>`;
    } else if (item.type === 'rejected') {
      badgeText = 'EXCLUDED';
      statusDetailText = `<div class="history-item-addr" style="color: var(--text-muted);">Reason: ${item.reason}</div>`;
    } else {
      badgeText = `QA ${item.score}/5`;
      statusDetailText = `<div class="history-item-addr">${item.venue.address}</div>`;
    }

    li.innerHTML = `
      <div class="history-item-header">
        <span class="history-item-name">${item.venue.name}</span>
        <span class="history-item-score">${badgeText}</span>
      </div>
      ${statusDetailText}
    `;

    li.addEventListener('click', () => {
      selectedItemId = item.id;
      document.querySelectorAll('.results-history-list li').forEach(el => el.classList.remove('active'));
      li.classList.add('active');
      renderActiveItem(item);
    });

    resultsList.appendChild(li);
  });
}

// ─── RENDER EXCLUDED ─────────────────────────────────────────────────────────
function renderRejected(rejected) {
  // Hide the rejected section completely as requested
  rejectedList.innerHTML = '';
  rejectedDetails.style.display = 'none';
}

// ─── STATE POLLING ───
let selectedItemId = null;
let initialLoadComplete = false;

async function pollState() {
  try {
    const state = await apiGet('/api/state');

    if (!initialLoadComplete && state.config) {
      if (state.config.coverage) inputCoverage.value = state.config.coverage;
      if (state.config.business_types) inputTypes.value = state.config.business_types;
      if (state.config.target_per_day) inputTarget.value = state.config.target_per_day;
      if (state.config.planter_id) {
        selectedPlanterId = String(state.config.planter_id);
        document.querySelectorAll('.planter-option').forEach(opt => {
          if (opt.dataset.id === selectedPlanterId) opt.classList.add('active');
          else opt.classList.remove('active');
        });
      }
      initialLoadComplete = true;
    }
    
    // Status Badge
    statusBadge.textContent = state.status;
    statusBadge.className = `status-badge ${state.status}`;

    // Queue Remaining Badge
    queueBadge.textContent = `Queue: ${state.queue.length} remaining`;

    // Start/Pause Buttons State
    if (state.status === 'running') {
      btnStart.disabled = true;
      btnPause.disabled = false;
    } else {
      btnStart.disabled = false;
      btnPause.disabled = true;
    }

    // Update active pipeline step
    updatePipelineSteps(state.current_step, state.status);

    // Build the combined list
    const results = state.results || [];
    const rejected = state.rejected || [];
    let combinedItems = [];

    // Successes
    results.forEach((res, index) => {
      combinedItems.push({
        ...res,
        type: 'success',
        id: `success_${index}`,
        originalIndex: index
      });
    });

    // Rejections
    rejected.forEach((rej, index) => {
      combinedItems.push({
        ...rej,
        type: 'rejected',
        id: `rejected_${index}`,
        originalIndex: index
      });
    });

    // Currently processing
    if (state.status === 'running' && state.current_venue) {
      combinedItems.push({
        type: 'processing',
        id: 'processing',
        venue: state.current_venue,
        step_detail: state.current_step_detail || "Processing...",
        timestamp: Date.now() / 1000
      });
    }

    // Sort by timestamp descending so newest is at the top
    combinedItems.sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0));

    // Render results list
    renderResultsList(combinedItems);

    // Auto-select the first item (newest) if nothing is selected or if selected item is no longer in list
    const selectedItem = combinedItems.find(item => item.id === selectedItemId);
    if (!selectedItemId && combinedItems.length > 0) {
      selectedItemId = combinedItems[0].id;
      renderActiveItem(combinedItems[0]);
    } else if (selectedItemId) {
      renderActiveItem(selectedItem);
    } else {
      renderActiveItem(null);
    }

    // Call renderRejected which hides the bottom panel
    renderRejected(rejected);

  } catch (e) {
    console.error('Polling error:', e);
  }
}

// ─── START / PAUSE TRIGGERS ──────────────────────────────────────────────────
btnStart.addEventListener('click', async () => {
  try {
    btnStart.disabled = true;
    
    // 1. Configure the agent
    await apiPost('/api/configure', {
      coverage: inputCoverage.value,
      business_types: inputTypes.value,
      target_per_day: parseInt(inputTarget.value) || 3,
      planter_id: selectedPlanterId
    });

    // 2. Start the pipeline
    await apiPost('/api/start');
    
    // Reset active selection so we load the fresh run result
    activeResultIndex = null;
    
    await pollState();
  } catch (e) {
    showError('Failed to start agent: ' + e.message);
    btnStart.disabled = false;
  }
});

btnPause.addEventListener('click', async () => {
  try {
    btnPause.disabled = true;
    await apiPost('/api/pause');
    await pollState();
  } catch (e) {
    showError('Failed to pause agent: ' + e.message);
    btnPause.disabled = false;
  }
});

// ─── INIT ────────────────────────────────────────────────────────────────────
// Initial state fetch
pollState();

// Start periodic polling every 3 seconds
setInterval(pollState, 3000);
