// Intercept all fetch requests to automatically add the X-Sanguine-Auth header
const urlParams = new URLSearchParams(window.location.search);
const urlToken = urlParams.get('token');
if (urlToken) {
    localStorage.setItem('sanguine_token', urlToken);
    window.history.replaceState({}, document.title, window.location.pathname);
}
const apiToken = localStorage.getItem('sanguine_token') || (window.SANGUINE_TOKEN || '');

const originalFetch = window.fetch;
window.fetch = function(url, options) {
  options = options || {};
  options.headers = options.headers || {};
  if (options.headers instanceof Headers) {
    options.headers.set('X-Sanguine-Auth', apiToken);
  } else {
    options.headers['X-Sanguine-Auth'] = apiToken;
  }
  return originalFetch(url, options);
};

// Global States
let config = {};
let running = false;
let screenshotLeft = 0;
let screenshotTop = 0;
let historyData = [];
let hasInitialized = false;
let lastImage = null;
let hoverX = null;
let hoverY = null;
let latestHealthPct = 100;

// Non-blocking toast notification helper
function showToast(message, type = 'info') {
  const container = document.getElementById('toast-container');
  if (!container) return;
  
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  
  const text = document.createElement('span');
  text.textContent = message;
  toast.appendChild(text);
  
  container.appendChild(toast);
  
  setTimeout(() => {
    toast.style.animation = 'fadeOut 0.3s ease forwards';
    setTimeout(() => {
      toast.remove();
    }, 300);
  }, 4000);
}

// Exponential backoff status checker
let statusInterval = 500;
let statusTimeout = null;

function scheduleFetchStatus() {
  if (statusTimeout) clearTimeout(statusTimeout);
  statusTimeout = setTimeout(() => {
    fetchStatus()
      .then(() => {
        statusInterval = 500; // Reset backoff on success
        scheduleFetchStatus();
      })
      .catch(err => {
        console.error("Status fetch error:", err);
        statusInterval = Math.min(statusInterval * 1.5, 10000);
        scheduleFetchStatus();
      });
  }, statusInterval);
}

// Canvas & Chart settings
const canvas = document.getElementById('screenshot-canvas');
const ctx = canvas.getContext('2d');
const liveChart = document.getElementById('live-chart');
const chartCtx = liveChart.getContext('2d');

// DOM Elements
const statusBadge = document.getElementById('status-badge');
const statusText = document.getElementById('status-text');
const toggleBtn = document.getElementById('toggle-monitor-btn');
const coordsText = document.getElementById('coords-text');
const mouseCoordsText = document.getElementById('mouse-coords');

const metricR = document.getElementById('metric-r');
const metricG = document.getElementById('metric-g');
const metricB = document.getElementById('metric-b');
const metricRatio = document.getElementById('metric-ratio');

const logsPanel = document.getElementById('logs-panel');
const clearLogsBtn = document.getElementById('clear-logs-btn');

// Form Inputs
const formFields = [
  'logic_mode', 'ratio_threshold', 'red_threshold', 'trigger_key',
  'cooldown', 'check_interval', 'toggle_hotkey', 'trigger_method', 'custom_command',
  'capture_method', 'sensor_size',
  'gate_enabled', 'gate_x', 'gate_y', 'gate_r', 'gate_g', 'gate_b', 'gate_tolerance',
  'health_threshold_pct', 'rect_width', 'rect_height', 'cv_matching_enabled'
];

// Initialize layout and events
window.addEventListener('load', () => {
  fetchStatus();
  fetchSnapshot(); // Load initial snapshot once on page start
  setupFormListeners();
  setupCanvasClick();
  setupChartResize();
  
  // Bind manual refresh button
  const refreshBtn = document.getElementById('refresh-preview-btn');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', fetchSnapshot);
  }
  
  // Bind collapsible logs header click
  const logsHeader = document.getElementById('logs-toggle-header');
  const logsWrapper = document.getElementById('logs-wrapper');
  const logsArrow = document.getElementById('logs-arrow-icon');
  const logsLabel = document.getElementById('logs-toggle-label');
  
  if (logsHeader && logsWrapper && logsArrow && logsLabel) {
    logsHeader.addEventListener('click', () => {
      const isCollapsed = logsWrapper.style.display === 'none';
      logsWrapper.style.display = isCollapsed ? 'block' : 'none';
      logsLabel.innerText = isCollapsed ? 'Click to Collapse' : 'Click to Expand';
      if (isCollapsed) {
        logsArrow.classList.add('open');
      } else {
        logsArrow.classList.remove('open');
      }
    });
  }
  
  // Bind collapsible advanced settings header click
  const advHeader = document.getElementById('advanced-toggle-header');
  const advWrapper = document.getElementById('advanced-wrapper');
  const advArrow = document.getElementById('advanced-arrow-icon');
  const advLabel = document.getElementById('advanced-toggle-label');
  
  if (advHeader && advWrapper && advArrow && advLabel) {
    advHeader.addEventListener('click', () => {
      const isCollapsed = advWrapper.style.display === 'none';
      advWrapper.style.display = isCollapsed ? 'flex' : 'none';
      advLabel.innerText = isCollapsed ? 'Click to Collapse' : 'Click to Expand';
      if (isCollapsed) {
        advArrow.classList.add('open');
      } else {
        advArrow.classList.remove('open');
      }
    });
  }
  
  // Periodic fetch: refresh metrics only (no automatic snapshot polling to save CPU/Wayland overhead)
  scheduleFetchStatus();
});

function setupFormListeners() {
  // Toggle inputs based on select changes
  document.getElementById('logic_mode').addEventListener('change', (e) => {
    toggleSliderGroups(e.target.value);
  });
  document.getElementById('trigger_method').addEventListener('change', (e) => {
    toggleCommandGroup(e.target.value);
  });

  // Synchronize range sliders with numeric display readouts
  document.getElementById('ratio_threshold').addEventListener('input', (e) => {
    document.getElementById('ratio_threshold_val').innerText = parseFloat(e.target.value).toFixed(2);
  });
  document.getElementById('red_threshold').addEventListener('input', (e) => {
    document.getElementById('red_threshold_val').innerText = e.target.value;
  });
  document.getElementById('cooldown').addEventListener('input', (e) => {
    document.getElementById('cooldown_val').innerText = parseFloat(e.target.value).toFixed(1) + 's';
  });
  document.getElementById('check_interval').addEventListener('input', (e) => {
    document.getElementById('check_interval_val').innerText = parseFloat(e.target.value).toFixed(2) + 's';
  });
  document.getElementById('sensor_size').addEventListener('input', (e) => {
    const val = parseInt(e.target.value, 10);
    const sensorSizeValEl = document.getElementById('sensor_size_val');
    if (sensorSizeValEl) {
      sensorSizeValEl.innerText = val + 'x' + val;
    }
    config.sensor_size = val;
    drawCanvas();
  });
  const rectWidthRange = document.getElementById('rect_width');
  const rectWidthNum = document.getElementById('rect_width_num');
  
  rectWidthRange.addEventListener('input', (e) => {
    rectWidthNum.value = e.target.value;
    config.rect_width = parseInt(e.target.value, 10);
    drawCanvas();
  });
  rectWidthNum.addEventListener('input', (e) => {
    let val = parseInt(e.target.value, 10) || 2;
    val = Math.max(2, Math.min(200, val));
    rectWidthRange.value = val;
    config.rect_width = val;
    drawCanvas();
  });

  const rectHeightRange = document.getElementById('rect_height');
  const rectHeightNum = document.getElementById('rect_height_num');
  
  rectHeightRange.addEventListener('input', (e) => {
    rectHeightNum.value = e.target.value;
    config.rect_height = parseInt(e.target.value, 10);
    drawCanvas();
  });
  rectHeightNum.addEventListener('input', (e) => {
    let val = parseInt(e.target.value, 10) || 10;
    val = Math.max(10, Math.min(400, val));
    rectHeightRange.value = val;
    config.rect_height = val;
    drawCanvas();
  });

  const healthRange = document.getElementById('health_threshold_pct');
  const healthNum = document.getElementById('health_threshold_pct_num');
  
  healthRange.addEventListener('input', (e) => {
    healthNum.value = e.target.value;
  });
  healthNum.addEventListener('input', (e) => {
    let val = parseInt(e.target.value, 10) || 5;
    val = Math.max(5, Math.min(99, val));
    healthRange.value = val;
  });

  // Gate enabled switch toggle UI panels
  const gateEnabledCheck = document.getElementById('gate_enabled');
  const gateSettingsPanel = document.getElementById('gate-settings-panel');

  gateEnabledCheck.addEventListener('change', (e) => {
    const isChecked = e.target.checked;
    gateSettingsPanel.style.display = isChecked ? 'flex' : 'none';
    drawCanvas();
  });

  document.getElementById('gate_tolerance').addEventListener('input', (e) => {
    document.getElementById('gate_tolerance_val').innerText = e.target.value;
  });

  // Listen to radio canvas mode changes to update magnifier zoom and crosshairs instantly
  document.querySelectorAll('input[name="canvas_mode"]').forEach(radio => {
    radio.addEventListener('change', () => {
      drawCanvas();
      updateCoordsDisplay();
    });
  });

  // Actions
  toggleBtn.addEventListener('click', toggleMonitor);
  document.getElementById('save-config-btn').addEventListener('click', saveConfig);
  document.getElementById('test-trigger-btn').addEventListener('click', testTrigger);

  const cvEnabledEl = document.getElementById('cv_matching_enabled');
  if (cvEnabledEl) {
    cvEnabledEl.addEventListener('change', () => {
      saveConfig();
    });
  }

  const saveTemplateBtn = document.getElementById('save-template-btn');
  if (saveTemplateBtn) {
    saveTemplateBtn.addEventListener('click', () => {
      const mx = config.monitor_x || 200;
      const my = config.monitor_y || 900;
      
      const cropWidth = 150;
      const cropHeight = 150;
      const cropX = Math.round(mx - cropWidth / 2);
      const cropY = Math.round(my - cropHeight / 2);
      
      fetch('/api/save_template', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          x: cropX,
          y: cropY,
          w: cropWidth,
          h: cropHeight,
          name: 'health_globe.png'
        })
      })
      .then(res => {
        if (!res.ok) throw new Error("HTTP error " + res.status);
        return res.json();
      })
      .then(data => {
        if (data.status === "success") {
          showToast(`Auto-Align template saved successfully centered around (${mx}, ${my})!`, "success");
        } else {
          showToast("Failed to save template. Make sure screen is captured and coordinates are valid.", "error");
        }
      })
      .catch(err => showToast("Error saving template: " + err, "error"));
    });
  }

  clearLogsBtn.addEventListener('click', () => {
    logsPanel.innerHTML = '<div class="log-line log-info">[--:--:--] [SYSTEM] Logs cleared.</div>';
  });

  // Tab toggles
  const tabLiveBtn = document.getElementById('tab-live-btn');
  const tabFileBtn = document.getElementById('tab-file-btn');
  const logsPanelEl = document.getElementById('logs-panel');
  const filePanelEl = document.getElementById('file-panel');
  const refreshFileBtn = document.getElementById('refresh-file-btn');

  tabLiveBtn.addEventListener('click', () => {
    tabLiveBtn.className = 'btn btn-primary';
    tabFileBtn.className = 'btn btn-secondary';
    logsPanelEl.style.display = 'block';
    filePanelEl.style.display = 'none';
    clearLogsBtn.style.display = 'block';
    refreshFileBtn.style.display = 'none';
  });

  tabFileBtn.addEventListener('click', () => {
    tabLiveBtn.className = 'btn btn-secondary';
    tabFileBtn.className = 'btn btn-primary';
    logsPanelEl.style.display = 'none';
    filePanelEl.style.display = 'block';
    clearLogsBtn.style.display = 'none';
    refreshFileBtn.style.display = 'block';
    fetchDebugLogFile();
  });

  refreshFileBtn.addEventListener('click', fetchDebugLogFile);

  const snapMouseBtn = document.getElementById('snap-mouse-btn');
  snapMouseBtn.addEventListener('click', () => {
    let count = 3;
    snapMouseBtn.disabled = true;
    snapMouseBtn.innerText = `Hover target in ${count}s...`;
    
    const timer = setInterval(() => {
      count--;
      if (count > 0) {
        snapMouseBtn.innerText = `Hover target in ${count}s...`;
      } else {
        clearInterval(timer);
        snapMouseBtn.innerText = "Capturing...";
        fetch('/api/mouse')
          .then(res => res.json())
          .then(data => {
            fetch('/api/config', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ monitor_x: data.x, monitor_y: data.y })
            })
            .then(res => res.json())
            .then(configData => {
              populateForm(configData.config);
              fetchSnapshot();
            });
          })
          .catch(err => {
            showToast("Failed to read mouse position: " + err, "error");
          })
          .finally(() => {
            snapMouseBtn.disabled = false;
            snapMouseBtn.innerText = "Target Under Mouse";
          });
      }
    }, 1000);
  });

  const autoDetectBtn = document.getElementById('auto-detect-btn');
  if (autoDetectBtn) {
    autoDetectBtn.addEventListener('click', () => {
      autoDetectBtn.disabled = true;
      autoDetectBtn.innerText = "Searching for game window...";
      fetch('/api/autodetect_game_window', { method: 'POST' })
        .then(res => res.json())
        .then(data => {
          if (data.success) {
            showToast(`Successfully detected "${data.title}"!\nCoordinates calibrated.`, "success");
            // Reload status, form values, and snapshot preview
            fetch('/api/status')
              .then(res => res.json())
              .then(statusData => {
                populateForm(statusData.config);
                fetchSnapshot();
              });
          } else {
            showToast("Detection failed: " + (data.error || "No known game window found."), "error");
          }
          autoDetectBtn.disabled = false;
          autoDetectBtn.innerText = "⚡ Auto-detect ARPG Game Coordinates";
        })
        .catch(err => {
          showToast("Error trying to auto-detect game window: " + err, "error");
          autoDetectBtn.disabled = false;
          autoDetectBtn.innerText = "⚡ Auto-detect ARPG Game Coordinates";
        });
    });
  }

  // Coordinate hover feedback
  canvas.addEventListener('mousemove', (e) => {
    const rect = canvas.getBoundingClientRect();
    const clickX = Math.round((e.clientX - rect.left) * (canvas.width / rect.width));
    const clickY = Math.round((e.clientY - rect.top) * (canvas.height / rect.height));
    hoverX = screenshotLeft + clickX;
    hoverY = screenshotTop + clickY;
    mouseCoordsText.innerText = `Cursor X: ${hoverX}, Y: ${hoverY}`;
    mouseCoordsText.style.display = 'inline-block';
    drawCanvas();
  });

  canvas.addEventListener('mouseleave', () => {
    hoverX = null;
    hoverY = null;
    mouseCoordsText.innerText = '';
    mouseCoordsText.style.display = 'none';
    drawCanvas();
  });
}

function toggleSliderGroups(mode) {
  const ratioGroup = document.getElementById('ratio-slider-group');
  const redGroup = document.getElementById('red-slider-group');
  const healthGroup = document.getElementById('health-slider-group');
  const rectGroup = document.getElementById('rect-size-group');
  const sensorGroup = document.getElementById('sensor-size-group');
  
  if (healthGroup) healthGroup.style.display = 'block';
  if (ratioGroup) ratioGroup.style.display = 'block';
  if (redGroup) redGroup.style.display = 'block';
  if (rectGroup) rectGroup.style.display = 'grid';
}

function toggleCommandGroup(method) {
  const group = document.getElementById('custom-command-group');
  group.style.display = (method === 'command') ? 'block' : 'none';
}

function updateCoordsDisplay() {
  const canvasModeEl = document.querySelector('input[name="canvas_mode"]:checked');
  const canvasMode = canvasModeEl ? canvasModeEl.value : 'health';
  const labelEl = document.querySelector('.coordinate-display div:first-child');
  
  if (canvasMode === 'gate') {
    if (labelEl) labelEl.innerText = "Gameplay Gate Target:";
    coordsText.innerText = `X: ${config.gate_x || 0}, Y: ${config.gate_y || 0}`;
  } else {
    if (labelEl) labelEl.innerText = "Monitored Target:";
    coordsText.innerText = `X: ${config.monitor_x || 0}, Y: ${config.monitor_y || 0}`;
  }
}

// Set UI from config payload
function populateForm(cfg) {
  config = cfg;
  formFields.forEach(field => {
    const el = document.getElementById(field);
    if (el) {
      if (field === 'gate_enabled' || field === 'cv_matching_enabled') {
        el.checked = cfg[field] || false;
        if (field === 'gate_enabled') {
          // Toggle panels
          const settingsPanel = document.getElementById('gate-settings-panel');
          settingsPanel.style.display = el.checked ? 'flex' : 'none';
        }
      } else {
        el.value = cfg[field];
      }
    }
  });
  
  // Update readouts
  document.getElementById('ratio_threshold_val').innerText = parseFloat(cfg.ratio_threshold).toFixed(2);
  document.getElementById('red_threshold_val').innerText = cfg.red_threshold;
  document.getElementById('cooldown_val').innerText = parseFloat(cfg.cooldown).toFixed(1) + 's';
  document.getElementById('check_interval_val').innerText = parseFloat(cfg.check_interval).toFixed(2) + 's';
  const sSize = cfg.sensor_size || 5;
  document.getElementById('sensor_size').value = sSize;
  const sensorSizeValEl = document.getElementById('sensor_size_val');
  if (sensorSizeValEl) {
    sensorSizeValEl.innerText = sSize + 'x' + sSize;
  }
  
  document.getElementById('health_threshold_pct_num').value = cfg.health_threshold_pct || 80;
  document.getElementById('rect_width_num').value = cfg.rect_width || 10;
  document.getElementById('rect_height_num').value = cfg.rect_height || 100;

  updateCoordsDisplay();
  
  // Update gate readouts
  const gx = cfg.gate_x || 0;
  const gy = cfg.gate_y || 0;
  const gr = cfg.gate_r || 0;
  const gg = cfg.gate_g || 0;
  const gb = cfg.gate_b || 0;
  const gTol = cfg.gate_tolerance || 20;
  
  document.getElementById('gate-coords-text').innerText = `X: ${gx}, Y: ${gy}`;
  document.getElementById('gate-color-preview').style.backgroundColor = `rgb(${gr},${gg},${gb})`;
  document.getElementById('gate-rgb-text').innerText = `(${gr}, ${gg}, ${gb})`;
  document.getElementById('gate_tolerance').value = gTol;
  document.getElementById('gate_tolerance_val').innerText = gTol;

  toggleSliderGroups(cfg.logic_mode);
  toggleCommandGroup(cfg.trigger_method);
}

// Fetch monitor state and configuration
function fetchStatus() {
  return fetch('/api/status')
    .then(res => {
      if (!res.ok) throw new Error("HTTP error " + res.status);
      return res.json();
    })
    .then(data => {
      running = data.running;
      updateStatusUI(running, data.ui_suspended);
      
      if (!hasInitialized) {
        populateForm(data.config);
        hasInitialized = true;
      } else {
        updateCoordsDisplay();
      }
      
      // Update live metrics
      metricR.innerText = data.current_rgb[0];
      metricG.innerText = data.current_rgb[1];
      metricB.innerText = data.current_rgb[2];
      metricRatio.innerText = parseFloat(data.current_ratio).toFixed(2);
      latestHealthPct = data.current_health_pct !== undefined ? data.current_health_pct : 100;
      document.getElementById('metric-health').innerText = latestHealthPct + '%';
      
      // Session type display
      const sessionTypeContainer = document.getElementById('session-type-container');
      const sessionTypeText = document.getElementById('session-type-text');
      const sessionStatusDot = document.getElementById('session-status-dot');
      if (sessionTypeText && data.session_type) {
        const displayType = data.session_type.toUpperCase();
        let tooltip = '';
        
        if (data.session_type === 'windows') {
          sessionTypeText.innerText = `${displayType} (Capture: Native mss / Sub-millisecond)`;
          if (sessionStatusDot) sessionStatusDot.style.backgroundColor = 'var(--blue)';
          tooltip = `[AUTODETECTED CONFIGURATION]\n` +
                    `This capture mode setting is fully autodetected on system startup.\n\n` +
                    `Current Session: WINDOWS\n` +
                    `Active Capture: Native mss (Fastest sub-millisecond, Direct Win32 GDI read)\n\n` +
                    `Supported Platforms:\n\n` +
                    `[WINDOWS]\n` +
                    `- Windows (Active): Captures screens natively using Win32 GDI calls via pure Python mss, yielding sub-millisecond grab speeds. Inputs are simulated using native OS-level Win32 API hooks via pynput.\n` +
                    `  *Note: Sanguine Sentry MUST be run as Administrator if the target game/application runs with admin privileges (to bypass Windows UIPI security restrictions).*\n\n` +
                    `[LINUX]\n` +
                    `- Linux X11 (Inactive): Used under Linux X11 environments.\n` +
                    `- Linux Wayland (Inactive): Used under Linux Wayland environments (uses PipeWire or Spectacle).`;
        } else if (data.session_type === 'x11') {
          sessionTypeText.innerText = `${displayType} (Capture: Native mss / Sub-millisecond)`;
          if (sessionStatusDot) sessionStatusDot.style.backgroundColor = 'var(--blue)';
          tooltip = `[AUTODETECTED CONFIGURATION]\n` +
                    `This capture mode setting is fully autodetected on system startup.\n\n` +
                    `Current Session: X11\n` +
                    `Active Capture: Native mss (Fastest sub-millisecond, Direct X11 memory read)\n\n` +
                    `Supported Platforms:\n\n` +
                    `[LINUX]\n` +
                    `- Linux X11 (Active): Used automatically when your XDG_SESSION_TYPE environment variable is set to 'x11'. Grabs pixels natively with no subprocess overhead.\n` +
                    `- Wayland Daemon (Inactive): Used under Wayland sessions when the Rust PipeWire & DMA-BUF socket daemon is running.\n` +
                    `- Wayland Fallback (Inactive): Used under Wayland sessions when the socket daemon is stopped (relies on spectacle screenshots).\n\n` +
                    `[WINDOWS]\n` +
                    `- Windows (Inactive): Grabs screen natively via Win32 GDI calls (mss) with sub-millisecond speed and emulates inputs natively using Win32 API hooks (pynput), requiring no extra capture daemons. Must run Sanguine Sentry as Admin if the target game is running with elevated privileges.`;
        } else {
          if (data.capture_method === 'socket') {
            sessionTypeText.innerText = `${displayType} (Capture: High-speed Daemon / Socket)`;
            if (sessionStatusDot) sessionStatusDot.style.backgroundColor = 'var(--blue)';
            tooltip = `[AUTODETECTED CONFIGURATION]\n` +
                      `This capture mode setting is fully autodetected on system startup.\n\n` +
                      `Current Session: WAYLAND\n` +
                      `Active Capture: High-speed Socket Daemon (Fastest sub-millisecond, zero-copy PipeWire & DMA-BUF)\n\n` +
                      `Supported Platforms:\n\n` +
                      `[LINUX]\n` +
                      `- Wayland Daemon (Active): Used automatically under Wayland when the background UNIX socket (/tmp/sanguine_sentry.sock) is active. Zero-copy GPU memory access.\n` +
                      `- Wayland Fallback (Inactive): Used under Wayland when the Unix socket daemon is stopped (falls back to spectacle screenshots).\n   (relies on spectacle screenshots).\n` +
                      `- X11 (Inactive): Used automatically when your desktop environment is running under an X11 compositor (uses native mss).\n\n` +
                      `[WINDOWS]\n` +
                      `- Windows (Inactive): Grabs screen natively via Win32 GDI calls (mss) with sub-millisecond speed and emulates inputs natively using Win32 API hooks (pynput), requiring no extra capture daemons. Must run Sanguine Sentry as Admin if the target game is running with elevated privileges.`;
          } else {
            sessionTypeText.innerText = `${displayType} (Capture: Spectacle Fallback / Subprocess)`;
            if (sessionStatusDot) sessionStatusDot.style.backgroundColor = 'var(--yellow)';
            tooltip = `[AUTODETECTED CONFIGURATION]\n` +
                      `This capture mode setting is fully autodetected on system startup.\n\n` +
                      `Current Session: WAYLAND\n` +
                      `Active Capture: Spectacle Fallback (Slow ~100-200ms per frame, spawns CLI subprocesses)\n\n` +
                      `Supported Platforms:\n\n` +
                      `[LINUX]\n` +
                      `- Wayland Fallback (Active): Used automatically under Wayland when the PipeWire & DMA-BUF socket daemon is NOT running.\n` +
                      `- Wayland Daemon (Inactive): Used automatically under Wayland if you start the Rust capture daemon (binds to Unix sockets).\n` +
                      `- X11 (Inactive): Used automatically when your desktop environment is running under an X11 compositor (uses native mss).\n\n` +
                      `[WINDOWS]\n` +
                      `- Windows (Inactive): Grabs screen natively via Win32 GDI calls (mss) with sub-millisecond speed and emulates inputs natively using Win32 API hooks (pynput), requiring no extra capture daemons. Must run Sanguine Sentry as Admin if the target game is running with elevated privileges.`;
          }
        }
        if (sessionTypeContainer) {
          sessionTypeContainer.setAttribute('title', tooltip);
        }
      }

      // Log parsing
      updateLogs(data.logs);
      
      // Graph rendering
      historyData = data.color_history;
      drawChart();
    })
    .catch(err => console.error("Error fetching status:", err));
}

// Toggle Monitor Running state
function toggleMonitor() {
  fetch('/api/toggle', { method: 'POST' })
    .then(res => res.json())
    .then(data => {
      running = data.running;
      updateStatusUI(running, data.ui_suspended);
    })
    .catch(err => console.error("Error toggling monitor:", err));
}

function updateStatusUI(isActive, uiSuspended) {
  if (isActive) {
    if (uiSuspended) {
      statusBadge.className = 'status-badge warning';
      statusBadge.style.backgroundColor = 'var(--yellow)';
      statusBadge.style.boxShadow = '0 0 8px var(--yellow)';
      statusText.innerText = 'MONITOR SUSPENDED (MENU OPEN)';
    } else {
      statusBadge.className = 'status-badge active';
      statusBadge.style.backgroundColor = 'var(--green)';
      statusBadge.style.boxShadow = '0 0 8px rgba(0,255,170,0.4)';
      statusText.innerText = 'MONITOR ACTIVE';
    }
    toggleBtn.innerHTML = `
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><rect x="4" y="4" width="16" height="16" rx="2" ry="2"></rect></svg>
      STOP MONITOR
    `;
    toggleBtn.className = 'btn btn-secondary btn-toggle-active';
  } else {
    statusBadge.className = 'status-badge inactive';
    statusBadge.style.backgroundColor = 'var(--text-muted)';
    statusBadge.style.boxShadow = 'none';
    statusText.innerText = 'MONITOR SUSPENDED';
    toggleBtn.innerHTML = `
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
      START MONITOR
    `;
    toggleBtn.className = 'btn btn-primary btn-toggle-active';
  }
}

// Save configuration settings
function saveConfig() {
  const payload = {};
  formFields.forEach(field => {
    const el = document.getElementById(field);
    if (el) {
      const val = el.value;
      if (field === 'gate_enabled' || field === 'cv_matching_enabled') {
        payload[field] = el.checked;
      } else if (field === 'ratio_threshold' || field === 'cooldown' || field === 'check_interval') {
        payload[field] = parseFloat(val);
      } else if (field === 'red_threshold' || field === 'sensor_size' || field === 'gate_x' || field === 'gate_y' || field === 'gate_r' || field === 'gate_g' || field === 'gate_b' || field === 'gate_tolerance' || field === 'health_threshold_pct' || field === 'rect_width' || field === 'rect_height') {
        payload[field] = parseInt(val, 10);
      } else {
        payload[field] = val;
      }
    }
  });

  fetch('/api/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  })
  .then(res => {
    if (!res.ok) throw new Error("HTTP error " + res.status);
    return res.json();
  })
  .then(data => {
    populateForm(data.config);
    fetchSnapshot();
    showToast("Configuration saved successfully!", "success");
  })
  .catch(err => showToast("Error saving config: " + err, "error"));
}

// Manually trigger a key simulation
function testTrigger() {
  fetch('/api/trigger', { method: 'POST' })
    .then(res => {
      if (!res.ok) throw new Error("HTTP error " + res.status);
      return res.json();
    })
    .then(data => {
      if (data.status === 'success') {
        console.log("Manual trigger command sent.");
      } else {
        showToast("Trigger failed. Check logs.", "error");
      }
    });
}

// Interactive canvas: set coordinate on click, or drag to draw scanning rectangle
function setupCanvasClick() {
  let isDrawing = false;
  let startX = 0;
  let startY = 0;
  
  canvas.addEventListener('mousedown', (e) => {
    const rect = canvas.getBoundingClientRect();
    startX = Math.round((e.clientX - rect.left) * (canvas.width / rect.width));
    startY = Math.round((e.clientY - rect.top) * (canvas.height / rect.height));
    
    const canvasModeEl = document.querySelector('input[name="canvas_mode"]:checked');
    const canvasMode = canvasModeEl ? canvasModeEl.value : 'health';
    
    if (canvasMode === 'health') {
      isDrawing = true;
    }
  });
  
  canvas.addEventListener('mousemove', (e) => {
    const rect = canvas.getBoundingClientRect();
    const clickX = Math.round((e.clientX - rect.left) * (canvas.width / rect.width));
    const clickY = Math.round((e.clientY - rect.top) * (canvas.height / rect.height));
    hoverX = screenshotLeft + clickX;
    hoverY = screenshotTop + clickY;
    mouseCoordsText.innerText = `Cursor X: ${hoverX}, Y: ${hoverY}`;
    mouseCoordsText.style.display = 'inline-block';
    
    if (isDrawing) {
      drawCanvas();
      
      // Draw temporary cyan dashed rectangle outline
      ctx.strokeStyle = 'rgba(0, 255, 255, 0.95)';
      ctx.lineWidth = 1.5;
      ctx.setLineDash([4, 2]);
      ctx.strokeRect(startX, startY, clickX - startX, clickY - startY);
      ctx.setLineDash([]);
    } else {
      drawCanvas();
    }
  });
  
  canvas.addEventListener('mouseup', (e) => {
    const rect = canvas.getBoundingClientRect();
    const endX = Math.round((e.clientX - rect.left) * (canvas.width / rect.width));
    const endY = Math.round((e.clientY - rect.top) * (canvas.height / rect.height));
    
    const canvasModeEl = document.querySelector('input[name="canvas_mode"]:checked');
    const canvasMode = canvasModeEl ? canvasModeEl.value : 'health';
    
    if (canvasMode === 'gate') {
      // Gate mode: simple single click
      const absX = screenshotLeft + endX;
      const absY = screenshotTop + endY;
      
      let payload = { gate_x: absX, gate_y: absY };
      try {
        if (lastImage) {
          const offscreen = document.createElement('canvas');
          offscreen.width = lastImage.width;
          offscreen.height = lastImage.height;
          const offCtx = offscreen.getContext('2d');
          offCtx.drawImage(lastImage, 0, 0);
          
          const scaleX = lastImage.width / canvas.width;
          const scaleY = lastImage.height / canvas.height;
          const imgX = Math.round(endX * scaleX);
          const imgY = Math.round(endY * scaleY);
          
          const pData = offCtx.getImageData(imgX, imgY, 1, 1).data;
          payload.gate_r = pData[0];
          payload.gate_g = pData[1];
          payload.gate_b = pData[2];
        }
      } catch (err) {
        console.warn("Client-side color extraction blocked:", err);
      }
      
      fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      })
      .then(res => res.json())
      .then(data => {
        populateForm(data.config);
        fetchSnapshot();
      })
      .catch(err => console.error("Error setting gate coordinate:", err));
      
    } else if (isDrawing) {
      isDrawing = false;
      
      const width = Math.abs(endX - startX);
      const height = Math.abs(endY - startY);
      
      const clickX = Math.round((startX + endX) / 2);
      const clickY = Math.round((startY + endY) / 2);
      const absX = screenshotLeft + clickX;
      const absY = screenshotTop + clickY;
      
      if (width > 4 && height > 4) {
        // Drag-and-draw mode
        fetch('/api/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            monitor_x: absX,
            monitor_y: absY,
            rect_width: width,
            rect_height: height
          })
        })
        .then(res => res.json())
        .then(data => {
          populateForm(data.config);
          fetchSnapshot();
        })
        .catch(err => console.error("Error setting rect:", err));
      } else {
        // Simple click: center current rect dimensions around click position
        const absXSingle = screenshotLeft + startX;
        const absYSingle = screenshotTop + startY;
        
        fetch('/api/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ monitor_x: absXSingle, monitor_y: absYSingle })
        })
        .then(res => res.json())
        .then(data => {
          populateForm(data.config);
          fetchSnapshot();
        })
        .catch(err => console.error("Error setting coordinate:", err));
      }
    }
  });
  
  canvas.addEventListener('mouseleave', () => {
    isDrawing = false;
    hoverX = null;
    hoverY = null;
    mouseCoordsText.innerText = '';
    mouseCoordsText.style.display = 'none';
    drawCanvas();
  });
}

// Step-by-step micro pixel dpad adjustments
function adjustCoordinate(dx, dy) {
  const canvasModeEl = document.querySelector('input[name="canvas_mode"]:checked');
  const canvasMode = canvasModeEl ? canvasModeEl.value : 'health';
  
  let payload = {};
  if (canvasMode === 'gate') {
    payload.gate_x = (config.gate_x || 0) + dx;
    payload.gate_y = (config.gate_y || 0) + dy;
  } else {
    payload.monitor_x = (config.monitor_x || 0) + dx;
    payload.monitor_y = (config.monitor_y || 0) + dy;
  }
  
  fetch('/api/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  })
  .then(res => res.json())
  .then(data => {
    populateForm(data.config);
    fetchSnapshot();
  })
  .catch(err => console.error(err));
}

// Draw the visual calibration viewport and the zoomed pixel magnifier
function drawCanvas() {
  if (!lastImage) return;

  // Draw background screenshot
  ctx.drawImage(lastImage, 0, 0, canvas.width, canvas.height);
  
  // Calculate coordinates relative to the crop
  const rx = config.monitor_x - screenshotLeft;
  const ry = config.monitor_y - screenshotTop;
  
  // 1. Draw Target Static Crosshairs
  ctx.strokeStyle = '#00ffaa';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(rx - 15, ry); ctx.lineTo(rx + 15, ry);
  ctx.moveTo(rx, ry - 15); ctx.lineTo(rx, ry + 15);
  ctx.stroke();
  
  // 2. Draw Target Static Grid Box or Scanning Rectangle Bounding Box
  if (config.logic_mode === 'percent') {
    const rWidth = config.rect_width || 10;
    const rHeight = config.rect_height || 100;
    ctx.strokeStyle = 'rgba(0, 255, 255, 0.85)';
    ctx.lineWidth = 1.5;
    ctx.setLineDash([4, 2]);
    ctx.strokeRect(rx - Math.floor(rWidth / 2), ry - Math.floor(rHeight / 2), rWidth, rHeight);
    ctx.setLineDash([]);
    
    // Draw real-time detected health liquid level line
    if (latestHealthPct !== undefined) {
      const levelY = (ry + Math.floor(rHeight / 2)) - Math.round((latestHealthPct / 100) * rHeight);
      ctx.strokeStyle = '#00ffff';
      ctx.lineWidth = 2.5;
      ctx.beginPath();
      ctx.moveTo(rx - Math.floor(rWidth / 2), levelY);
      ctx.lineTo(rx + Math.floor(rWidth / 2), levelY);
      ctx.stroke();
    }
  } else {
    const size = config.sensor_size || 5;
    const halfSize = Math.floor(size / 2);
    if (size > 1) {
      ctx.strokeStyle = 'rgba(255, 204, 0, 0.85)';
      ctx.lineWidth = 1;
      ctx.strokeRect(rx - halfSize, ry - halfSize, size, size);
    }
  }
  
  // 3. Draw Target Static Center Dot
  ctx.fillStyle = '#ff3366';
  ctx.beginPath();
  ctx.arc(rx, ry, 3, 0, 2 * Math.PI);
  ctx.fill();

  // 3b. Draw Active Gameplay Gate Target (Purple) if enabled
  if (config.gate_enabled && config.gate_x > 0) {
    const gx = config.gate_x - screenshotLeft;
    const gy = config.gate_y - screenshotTop;
    if (gx >= 0 && gx < canvas.width && gy >= 0 && gy < canvas.height) {
      ctx.strokeStyle = '#b55eff'; // Purple
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(gx - 12, gy); ctx.lineTo(gx + 12, gy);
      ctx.moveTo(gx, gy - 12); ctx.lineTo(gx, gy + 12);
      ctx.stroke();
      
      ctx.fillStyle = '#b55eff';
      ctx.beginPath();
      ctx.arc(gx, gy, 3, 0, 2 * Math.PI);
      ctx.fill();
    }
  }

  const canvasModeEl = document.querySelector('input[name="canvas_mode"]:checked');
  const canvasMode = canvasModeEl ? canvasModeEl.value : 'health';

  // Determine which coordinate to zoom (use hover position if mouse is over canvas, else static target)
  let zoomX = (canvasMode === 'gate' && config.gate_x > 0) ? (config.gate_x - screenshotLeft) : rx;
  let zoomY = (canvasMode === 'gate' && config.gate_y > 0) ? (config.gate_y - screenshotTop) : ry;
  let isHovering = false;
  const size = config.sensor_size || 5;
  const halfSize = Math.floor(size / 2);

  // 4. Draw Hover Dynamic Dashed Grid Box
  if (hoverX !== null && hoverY !== null) {
    const hx = hoverX - screenshotLeft;
    const hy = hoverY - screenshotTop;
    zoomX = hx;
    zoomY = hy;
    isHovering = true;
    
    const canvasModeEl = document.querySelector('input[name="canvas_mode"]:checked');
    const canvasMode = canvasModeEl ? canvasModeEl.value : 'health';
    
    if (size > 1) {
      ctx.strokeStyle = canvasMode === 'gate' ? 'rgba(181, 94, 255, 0.95)' : 'rgba(255, 255, 255, 0.7)';
      ctx.lineWidth = 1;
      ctx.setLineDash([2, 2]); // dashed outline
      ctx.strokeRect(hx - halfSize, hy - halfSize, size, size);
      ctx.setLineDash([]); // reset
    }
    
    // Hover center dot
    ctx.fillStyle = canvasMode === 'gate' ? '#b55eff' : '#00ffaa';
    ctx.beginPath();
    ctx.arc(hx, hy, 2, 0, 2 * Math.PI);
    ctx.fill();
  }

  // 5. Draw Magnifier Pixel Grid
  const mCanvas = document.getElementById('magnifier-canvas');
  if (mCanvas) {
    const mCtx = mCanvas.getContext('2d');
    mCtx.imageSmoothingEnabled = false;
    mCtx.clearRect(0, 0, mCanvas.width, mCanvas.height);

    // Draw cropped grid from main image
    mCtx.drawImage(
      lastImage,
      zoomX - halfSize,
      zoomY - halfSize,
      size,
      size,
      0,
      0,
      mCanvas.width,
      mCanvas.height
    );

    // Draw pixel grid division lines
    mCtx.strokeStyle = 'rgba(255, 255, 255, 0.25)';
    mCtx.lineWidth = 1;
    const pixelWidth = mCanvas.width / size;
    const pixelHeight = mCanvas.height / size;

    mCtx.beginPath();
    for (let i = 1; i < size; i++) {
      // vertical
      mCtx.moveTo(i * pixelWidth, 0);
      mCtx.lineTo(i * pixelWidth, mCanvas.height);
      // horizontal
      mCtx.moveTo(0, i * pixelHeight);
      mCtx.lineTo(mCanvas.width, i * pixelHeight);
    }
    mCtx.stroke();

    // Highlight center pixel box
    mCtx.strokeStyle = isHovering ? 'rgba(0, 255, 170, 0.9)' : 'rgba(255, 204, 0, 0.9)';
    mCtx.lineWidth = 2;
    mCtx.strokeRect(
      halfSize * pixelWidth,
      halfSize * pixelHeight,
      pixelWidth,
      pixelHeight
    );

    // Read pixel averages dynamically from canvas
    try {
      const pixelData = mCtx.getImageData(0, 0, mCanvas.width, mCanvas.height).data;
      let totalR = 0, totalG = 0, totalB = 0;
      const numPixels = mCanvas.width * mCanvas.height;
      // Loop through RGBA pixel data array
      for (let i = 0; i < pixelData.length; i += 4) {
        totalR += pixelData[i];
        totalG += pixelData[i + 1];
        totalB += pixelData[i + 2];
      }
      const avgR = Math.round(totalR / numPixels);
      const avgG = Math.round(totalG / numPixels);
      const avgB = Math.round(totalB / numPixels);
      const ratio = (avgR / (avgG + avgB + 1.0)).toFixed(2);
      document.getElementById('magnifier-avg-color').innerText = `RGB: ${avgR}, ${avgG}, ${avgB} | Ratio: ${ratio}`;
    } catch (e) {
      // Fallback if canvas security throws
    }
  }
}

// Fetch screenshot cropped region from server
function fetchSnapshot() {
  fetch('/api/screenshot')
    .then(res => {
      if (!res.ok) throw new Error("Fetch fail");
      // Extract absolute crop headers
      screenshotLeft = parseInt(res.headers.get("X-Crop-Left"), 10) || 0;
      screenshotTop = parseInt(res.headers.get("X-Crop-Top"), 10) || 0;
      return res.blob();
    })
    .then(blob => {
      const img = new Image();
      img.onload = function() {
        lastImage = img;
        drawCanvas();
      };
      img.src = URL.createObjectURL(blob);
    })
    .catch(err => console.error("Snapshot fetch error:", err));
}

// Update the console terminal logs
let lastLogLength = 0;
function updateLogs(logs) {
  if (logs.length === lastLogLength) return;
  lastLogLength = logs.length;
  
  const isScrolledToBottom = logsPanel.scrollHeight - logsPanel.clientHeight <= logsPanel.scrollTop + 10;
  
  logsPanel.innerHTML = '';
  logs.forEach(log => {
    const div = document.createElement('div');
    div.className = 'log-line';
    
    // Match log level styling
    if (log.includes('[TRIGGER]')) {
      div.className += ' log-trigger';
    } else if (log.includes('[WARNING]')) {
      div.className += ' log-warning';
    } else if (log.includes('[ERROR]')) {
      div.className += ' log-error';
    } else {
      div.className += ' log-info';
    }
    
    div.innerText = log;
    logsPanel.appendChild(div);
  });
  
  if (isScrolledToBottom) {
    logsPanel.scrollTop = logsPanel.scrollHeight;
  }
}

// Fetch persistent debug.log file content from backend
function fetchDebugLogFile() {
  const filePanel = document.getElementById('file-panel');
  filePanel.innerText = 'Loading debug.log file...';
  fetch('/api/debug_log')
    .then(res => res.text())
    .then(text => {
      filePanel.innerText = text;
      filePanel.scrollTop = filePanel.scrollHeight;
    })
    .catch(err => {
      filePanel.innerText = 'Error loading debug.log: ' + err;
    });
}

// Setup canvas layout resizing
function setupChartResize() {
  const resizeCanvas = () => {
    liveChart.width = liveChart.clientWidth;
    liveChart.height = liveChart.clientHeight;
    drawChart();
  };
  window.addEventListener('resize', resizeCanvas);
  setTimeout(resizeCanvas, 100);
}

// Draw live RGB & Ratio chart using Canvas
function drawChart() {
  if (!chartCtx || historyData.length === 0) return;
  
  const width = liveChart.width;
  const height = liveChart.height;
  
  // Clear canvas
  chartCtx.fillStyle = 'rgba(10, 10, 15, 0.4)';
  chartCtx.fillRect(0, 0, width, height);

  // Draw grid lines
  chartCtx.strokeStyle = 'rgba(255,255,255,0.03)';
  chartCtx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = (height - 20) * (i / 4) + 10;
    chartCtx.beginPath();
    chartCtx.moveTo(0, y);
    chartCtx.lineTo(width, y);
    chartCtx.stroke();
  }

  const pointsCount = historyData.length;
  const getX = (index) => (width / (pointsCount - 1)) * index;
  const getRGBY = (val) => height - 10 - ((val / 255) * (height - 20));
  
  // Helper function to draw a line
  const drawLine = (color, getValueFunc, lineWidth = 2) => {
    chartCtx.strokeStyle = color;
    chartCtx.lineWidth = lineWidth;
    chartCtx.beginPath();
    
    for (let i = 0; i < pointsCount; i++) {
      const x = getX(i);
      const y = getValueFunc(historyData[i]);
      if (i === 0) {
        chartCtx.moveTo(x, y);
      } else {
        chartCtx.lineTo(x, y);
      }
    }
    chartCtx.stroke();
  };

  const ratioMode = document.getElementById('logic_mode').value;
  if (ratioMode === 'percent') {
    const getPctY = (val) => height - 10 - (((val !== undefined ? val : 100) / 100) * (height - 20));
    
    // Draw actual health percentage line in bright cyan
    drawLine('#00ffff', (d) => getPctY(d.health_pct), 3);
    
    // Draw threshold dotted line
    const thresholdPct = parseInt(document.getElementById('health_threshold_pct').value, 10);
    const thresholdY = getPctY(thresholdPct);
    chartCtx.strokeStyle = 'rgba(255, 204, 0, 0.55)';
    chartCtx.lineWidth = 1.5;
    chartCtx.setLineDash([4, 4]);
    chartCtx.beginPath();
    chartCtx.moveTo(0, thresholdY);
    chartCtx.lineTo(width, thresholdY);
    chartCtx.stroke();
    chartCtx.setLineDash([]); // reset
  } else {
    // Draw channel lines
    drawLine('rgba(255, 51, 102, 0.95)', (d) => getRGBY(d.r));
    drawLine('rgba(0, 255, 170, 0.65)', (d) => getRGBY(d.g));
    drawLine('rgba(51, 204, 255, 0.65)', (d) => getRGBY(d.b));
    
    if (ratioMode !== 'red_value') {
      const threshold = parseFloat(document.getElementById('ratio_threshold').value);
      // Ratio maxes at 3.0 in graph mapping
      const getRatioY = (val) => height - 10 - ((Math.min(val, 3.0) / 3.0) * (height - 20));
      
      // Draw actual ratio line
      drawLine('rgba(255, 204, 0, 0.85)', (d) => getRatioY(d.ratio), 2);
      
      // Draw threshold dotted line
      const thresholdY = getRatioY(threshold);
      chartCtx.strokeStyle = 'rgba(255, 204, 0, 0.35)';
      chartCtx.lineWidth = 1;
      chartCtx.setLineDash([5, 5]);
      chartCtx.beginPath();
      chartCtx.moveTo(0, thresholdY);
      chartCtx.lineTo(width, thresholdY);
      chartCtx.stroke();
      chartCtx.setLineDash([]); // reset
    }
  }
}
