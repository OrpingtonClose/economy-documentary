const API_URL = ''; 

// State
let customDirectives = [];
let auditLogs = [];
let refreshInterval = null;
let activeEditDirective = null; // Track if we are currently editing to pause auto-refresh rendering
let lastCustomJson = '';
let questionRuleActive = false;
let lastQuestionRuleActive = null;
let lastLogsJson = '';
let lastCapsLockActive = null;
let lastCapsLockSuperprompt = '';

// DOM Elements
const statDirectivesCount = document.getElementById('stat-directives-count');
const statBlockedCount = document.getElementById('stat-blocked-count');
const logCountBadge = document.getElementById('log-count-badge');
const customRuleList = document.getElementById('custom-rule-list');
const logsContainer = document.getElementById('logs-container');
const addDirectiveForm = document.getElementById('add-directive-form');
const directiveInput = document.getElementById('directive-input');
const autoRefreshCheckbox = document.getElementById('auto-refresh');
const questionRuleToggle = document.getElementById('question-rule-toggle');
const questionRuleCard = document.getElementById('question-rule-card');
const refreshLogsBtn = document.getElementById('refresh-logs-btn');

// Caps Lock DOM Elements
const capsLockCard = document.getElementById('caps-lock-card');
const capsStatusTitle = document.getElementById('caps-status-title');
const capsLockBody = document.getElementById('caps-lock-body');
const capsSuperprompt = document.getElementById('caps-superprompt');
const cancelCapsLockBtn = document.getElementById('cancel-capslock-btn');

// Fetch directives and caps lock status
async function fetchDirectives() {
    try {
        const response = await fetch('/api/directives');
        if (!response.ok) throw new Error('Failed to fetch directives');
        const data = await response.json();
        customDirectives = data.custom_directives || [];
        const capsLockState = data.caps_lock || { active: false, superprompt: "" };
        
        renderDirectives(capsLockState);
        renderCapsLockStatus(capsLockState);
        
        const questionRuleState = data.question_rule || { active: false };
        questionRuleActive = !!questionRuleState.active;
        renderQuestionRuleStatus();
    } catch (error) {
        console.error('Error fetching directives:', error);
    }
}

// Fetch compliance audit logs
async function fetchLogs() {
    try {
        const response = await fetch('/api/logs');
        if (!response.ok) throw new Error('Failed to fetch logs');
        auditLogs = await response.json();
        renderLogs();
    } catch (error) {
        console.error('Error fetching logs:', error);
    }
}

// Render directives count and custom list
function renderDirectives(capsLockState) {
    const isCapsActive = capsLockState && capsLockState.active;
    const totalCount = customDirectives.length + (isCapsActive ? 1 : 0);
    statDirectivesCount.textContent = totalCount;

    const customJson = JSON.stringify(customDirectives);

    // Only render custom list if changed AND we are not currently editing
    if (customJson !== lastCustomJson && !activeEditDirective) {
        lastCustomJson = customJson;
        renderListSmoothly(customRuleList, customDirectives, true);
    }
}

// Render Caps Lock Status Card
function renderCapsLockStatus(state) {
    const isActive = !!state.active;
    const superprompt = state.superprompt || '';

    // Prevent redundant DOM updates
    if (isActive === lastCapsLockActive && superprompt === lastCapsLockSuperprompt) {
        return;
    }
    
    lastCapsLockActive = isActive;
    lastCapsLockSuperprompt = superprompt;

    if (isActive) {
        capsLockCard.classList.add('active');
        capsStatusTitle.textContent = '🚨 Caps Lock Protection: Active';
        capsLockBody.classList.remove('hidden');
        capsSuperprompt.textContent = superprompt;
    } else {
        capsLockCard.classList.remove('active');
        capsStatusTitle.textContent = 'Caps Lock Protection: Idle';
        capsLockBody.classList.add('hidden');
        capsSuperprompt.textContent = '';
    }
}

// Render list smoothly using DOM reconciliation (diffing)
function renderListSmoothly(listEl, newDirectives, isCustom) {
    if (newDirectives.length === 0) {
        listEl.innerHTML = `
            <div class="empty-state" style="padding: 2rem 0;">
                <p>No custom rules defined yet. Add one above!</p>
            </div>
        `;
        return;
    }

    const emptyState = listEl.querySelector('.empty-state');
    if (emptyState) {
        emptyState.remove();
    }

    const currentItems = Array.from(listEl.querySelectorAll('.rule-item:not(.fade-out)'));
    const currentMap = new Map();
    currentItems.forEach(item => {
        const dir = item.getAttribute('data-directive');
        if (dir) {
            currentMap.set(dir, item);
        }
    });

    const newDirectivesSet = new Set(newDirectives);

    // Fade out and remove deleted items
    currentItems.forEach(item => {
        const dir = item.getAttribute('data-directive');
        if (!newDirectivesSet.has(dir)) {
            item.classList.add('fade-out');
            item.addEventListener('transitionend', (e) => {
                if (e.propertyName === 'max-height' || e.propertyName === 'opacity') {
                    item.remove();
                }
            });
            setTimeout(() => {
                if (item.parentNode) item.remove();
            }, 300);
        }
    });

    // Reorder, update, or append items
    newDirectives.forEach((directive, index) => {
        let li = currentMap.get(directive);
        if (!li) {
            li = document.createElement('li');
            li.className = 'rule-item custom-rule-item';
            li.setAttribute('data-directive', directive);
            
            li.innerHTML = `
                <span class="rule-text" style="cursor: pointer; flex: 1;" title="Click to Edit">${escapeHtml(directive)}</span>
                <button class="rule-delete" title="Delete Rule">×</button>
            `;
            
            const spanText = li.querySelector('.rule-text');
            const btnDelete = li.querySelector('.rule-delete');

            spanText.addEventListener('click', () => {
                startEditing(li, spanText, directive, isCustom);
            });

            btnDelete.addEventListener('click', () => {
                deleteDirective(directive, isCustom);
            });

            li.style.opacity = '0';
            li.style.maxHeight = '0';
            li.style.paddingTop = '0';
            li.style.paddingBottom = '0';
            li.style.marginTop = '0';
            li.style.marginBottom = '0';
            li.style.borderWidth = '0';
            li.style.overflow = 'hidden';

            const targetChild = listEl.children[index];
            listEl.insertBefore(li, targetChild || null);

            requestAnimationFrame(() => {
                li.style.opacity = '';
                li.style.maxHeight = '';
                li.style.paddingTop = '';
                li.style.paddingBottom = '';
                li.style.marginTop = '';
                li.style.marginBottom = '';
                li.style.borderWidth = '';
                li.style.overflow = '';
            });
        } else {
            const spanText = li.querySelector('.rule-text');
            if (spanText && spanText.textContent !== directive && !li.classList.contains('is-editing')) {
                spanText.textContent = directive;
            }

            const targetChild = listEl.children[index];
            if (targetChild !== li) {
                listEl.insertBefore(li, targetChild || null);
            }
        }
    });
}

// Inline Editing Logic
function startEditing(li, spanText, directive, isCustom) {
    if (activeEditDirective) return; 
    activeEditDirective = directive;
    li.classList.add('is-editing');

    const oldText = directive;
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'edit-rule-input';
    input.value = directive;
    
    input.style.width = '100%';
    input.style.background = 'rgba(255, 255, 255, 0.05)';
    input.style.border = '1px solid var(--primary-color)';
    input.style.color = '#ffffff';
    input.style.padding = '0.4rem 0.8rem';
    input.style.borderRadius = '6px';
    input.style.fontFamily = 'inherit';
    input.style.fontSize = '0.85rem';
    input.style.outline = 'none';

    let saveFired = false;
    async function saveEdit() {
        if (saveFired) return;
        saveFired = true;

        const newText = input.value.trim();
        if (newText && newText !== oldText) {
            // Optimistic update
            const idx = customDirectives.indexOf(oldText);
            if (idx !== -1) {
                customDirectives[idx] = newText;
            }
            lastCustomJson = JSON.stringify(customDirectives);
            renderListSmoothly(customRuleList, customDirectives, true);

            try {
                const response = await fetch('/api/directives', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ old_directive: oldText, new_directive: newText })
                });
                if (!response.ok) throw new Error();
                const data = await response.json();
                
                customDirectives = data.custom_directives || data.directives || [];
                lastCustomJson = JSON.stringify(customDirectives);
                renderListSmoothly(customRuleList, customDirectives, true);
            } catch (err) {
                console.error('Failed to save edit', err);
                fetchDirectives();
            }
        } else {
            spanText.textContent = oldText;
        }
        activeEditDirective = null;
        li.classList.remove('is-editing');
    }

    input.addEventListener('blur', saveEdit);
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            input.blur();
        } else if (e.key === 'Escape') {
            e.preventDefault();
            activeEditDirective = null;
            li.classList.remove('is-editing');
            spanText.textContent = oldText;
        }
    });

    spanText.innerHTML = '';
    spanText.appendChild(input);
    input.focus();
    input.select();
}

// Render compliance audit logs
function renderLogs() {
    const logsJson = JSON.stringify(auditLogs);
    if (logsJson === lastLogsJson) return; 
    lastLogsJson = logsJson;

    // Count blocked actions (where status is FAIL)
    const blockedCount = auditLogs.filter(log => log.status === 'FAIL').length;
    statBlockedCount.textContent = blockedCount;
    logCountBadge.textContent = `${auditLogs.length} Check${auditLogs.length === 1 ? '' : 's'}`;

    logsContainer.innerHTML = '';
    if (auditLogs.length === 0) {
        logsContainer.innerHTML = `
            <div class="empty-state">
                <span class="empty-icon">🛡️</span>
                <h3>All Systems Compliant</h3>
                <p>No activity logged in this session yet.</p>
            </div>
        `;
        return;
    }

    auditLogs.forEach((log) => {
        const timeString = formatTimestamp(log.timestamp);
        const card = document.createElement('div');
        
        // Handle status styles
        const isPass = log.status === 'PASS';
        const cardClass = 'log-card ' + (isPass ? 'pass' : 'fail');
        const badgeClass = 'audit-status-badge ' + (isPass ? 'pass' : 'fail');
        const statusText = isPass ? 'PASS' : 'BLOCKED';
        const directiveClass = 'log-directive ' + (isPass ? 'pass' : 'fail');
        const reasonClass = 'log-reason ' + (isPass ? 'pass' : 'fail');

        card.className = cardClass;

        const prettyArgs = typeof log.arguments === 'object' 
            ? JSON.stringify(log.arguments, null, 2) 
            : log.arguments;

        card.innerHTML = `
            <div class="log-card-header">
                <div class="log-title-group" style="display: flex; align-items: center; gap: 0.6rem;">
                    <span class="log-tool">${escapeHtml(log.tool)}</span>
                    <span class="${badgeClass}">${statusText}</span>
                </div>
                <span class="log-time">${escapeHtml(timeString)}</span>
            </div>
            <div class="log-body">
                <div class="log-field">
                    <div class="log-field-label">Checked Directive</div>
                    <div class="${directiveClass}">${escapeHtml(log.directive)}</div>
                </div>
                <div class="log-field">
                    <div class="log-field-label">Intercepted Payload Arguments</div>
                    <pre class="log-code"><code>${escapeHtml(prettyArgs)}</code></pre>
                </div>
                <div class="log-field">
                    <div class="log-field-label">Compliance Outcome</div>
                    <div class="${reasonClass}">${escapeHtml(log.reason)}</div>
                </div>
            </div>
        `;
        logsContainer.appendChild(card);
    });
}

// Add custom directive
async function addDirective(e) {
    e.preventDefault();
    const val = directiveInput.value.trim();
    if (!val) return;

    customDirectives.push(val);
    lastCustomJson = JSON.stringify(customDirectives);
    renderListSmoothly(customRuleList, customDirectives, true);
    directiveInput.value = '';

    try {
        const response = await fetch('/api/directives', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ directive: val })
        });
        if (!response.ok) throw new Error('Failed to save directive');
        const data = await response.json();
        
        customDirectives = data.custom_directives || data.directives || [];
        lastCustomJson = JSON.stringify(customDirectives);
        renderListSmoothly(customRuleList, customDirectives, true);
    } catch (error) {
        console.error('Error saving directive:', error);
        customDirectives = customDirectives.filter(d => d !== val);
        lastCustomJson = JSON.stringify(customDirectives);
        renderListSmoothly(customRuleList, customDirectives, true);
    }
}

// Delete custom directive
async function deleteDirective(directive, isCustom) {
    const listEl = customRuleList;
    const ruleItems = Array.from(listEl.querySelectorAll('.rule-item'));
    const itemEl = ruleItems.find(item => item.getAttribute('data-directive') === directive);
    
    if (itemEl) {
        itemEl.classList.add('fade-out');
    }

    customDirectives = customDirectives.filter(d => d !== directive);
    lastCustomJson = JSON.stringify(customDirectives);
    renderListSmoothly(customRuleList, customDirectives, true);

    try {
        const response = await fetch(`/api/directives?directive=${encodeURIComponent(directive)}`, {
            method: 'DELETE'
        });
        if (!response.ok) throw new Error('Failed to delete directive');
        const data = await response.json();
        
        customDirectives = data.custom_directives || data.directives || [];
        lastCustomJson = JSON.stringify(customDirectives);
        renderListSmoothly(customRuleList, customDirectives, true);
    } catch (error) {
        console.error('Error deleting directive:', error);
        fetchDirectives();
    }
}

// Cancel Caps Lock Protection Mode
async function cancelCapsLock() {
    try {
        const response = await fetch('/api/capslock/cancel', {
            method: 'POST'
        });
        if (!response.ok) throw new Error();
        const data = await response.json();
        const capsLockState = data.caps_lock || { active: false, superprompt: "" };
        renderDirectives(capsLockState);
        renderCapsLockStatus(capsLockState);
        
        const questionRuleState = data.question_rule || { active: false };
        questionRuleActive = !!questionRuleState.active;
        renderQuestionRuleStatus();
    } catch (error) {
        console.error('Error canceling caps lock:', error);
    }
}

// Helper: Escape HTML to prevent XSS
// Render Question Mark Rule Status
function renderQuestionRuleStatus() {
    if (questionRuleActive === lastQuestionRuleActive) return;
    lastQuestionRuleActive = questionRuleActive;
    questionRuleToggle.checked = questionRuleActive;
    if (questionRuleActive) {
        questionRuleCard.classList.add('active');
    } else {
        questionRuleCard.classList.remove('active');
    }
}

// Toggle Question Mark Rule status
async function toggleQuestionRule() {
    const targetState = questionRuleToggle.checked;
    questionRuleActive = targetState;
    renderQuestionRuleStatus();
    
    try {
        const response = await fetch('/api/question-rule', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ active: targetState })
        });
        if (!response.ok) throw new Error();
        const data = await response.json();
        const questionRuleState = data.question_rule || { active: false };
        questionRuleActive = !!questionRuleState.active;
        renderQuestionRuleStatus();
    } catch (error) {
        console.error('Error toggling question rule:', error);
        questionRuleActive = !targetState;
        renderQuestionRuleStatus();
    }
}

function escapeHtml(str) {
    if (!str) return '';
    return str.toString()
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

// Helper: Format Timestamp (ISO string to local representation)
function formatTimestamp(isoString) {
    try {
        const date = new Date(isoString);
        return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) + ' ' + date.toLocaleDateString();
    } catch (e) {
        return isoString;
    }
}

// Setup Auto Refresh / Polling
function setupAutoRefresh() {
    if (autoRefreshCheckbox.checked) {
        if (!refreshInterval) {
            refreshInterval = setInterval(() => {
                fetchLogs();
                fetchDirectives();
            }, 5000);
        }
    } else {
        if (refreshInterval) {
            clearInterval(refreshInterval);
            refreshInterval = null;
        }
    }
}

// Event Listeners
addDirectiveForm.addEventListener('submit', addDirective);
cancelCapsLockBtn.addEventListener('click', cancelCapsLock);
autoRefreshCheckbox.addEventListener('change', setupAutoRefresh);
questionRuleToggle.addEventListener('change', toggleQuestionRule);
refreshLogsBtn.addEventListener('click', () => {
    fetchLogs();
    fetchDirectives();
});

// Initialization
fetchDirectives();
fetchLogs();
setupAutoRefresh();


// Setup live test dashboard DOM elements
const testPulseDot = document.getElementById('test-pulse-dot');
const testStatusText = document.getElementById('test-status-text');
const suiteProgressFill = document.getElementById('suite-progress-fill');
const suiteProgressPercent = document.getElementById('suite-progress-percent');
const suiteStatPassed = document.getElementById('suite-stat-passed');
const suiteStatFailed = document.getElementById('suite-stat-failed');
const suiteStatSkipped = document.getElementById('suite-stat-skipped');
const suiteStatPending = document.getElementById('suite-stat-pending');
const audioSlotsGrid = document.getElementById('audio-slots-grid');
const videoSlotsGrid = document.getElementById('video-slots-grid');
const vmTableBody = document.getElementById('vm-table-body');
const currentRunningLabel = document.getElementById('current-running-label');
const testCasesGrid = document.getElementById('test-cases-grid');

let lastTestDataJson = '';

async function fetchTestStatus() {
    try {
        const response = await fetch('/api/tests');
        if (!response.ok) throw new Error();
        const data = await response.json();
        renderTestDashboard(data);
    } catch (e) {
        console.error('Error fetching test status:', e);
    }
}

function renderTestDashboard(data) {
    const dataJson = JSON.stringify(data);
    if (dataJson === lastTestDataJson) return;
    lastTestDataJson = dataJson;

    const status = data.status || 'idle';
    
    // Update status text
    testStatusText.textContent = `Status: ${status.toUpperCase()}`;
    
    // Pulse dot class based on status
    if (status.toLowerCase().includes('run')) {
        testPulseDot.className = 'test-pulse-dot';
        testPulseDot.style.background = '#fbbf24'; // Yellow pulsing
        testPulseDot.style.animation = 'capsPulse 1.5s infinite';
    } else if (status.toLowerCase().includes('success') || status.toLowerCase().includes('pass')) {
        testPulseDot.className = 'test-pulse-dot';
        testPulseDot.style.background = '#10b981'; // Green
        testPulseDot.style.animation = 'none';
    } else if (status.toLowerCase().includes('fail')) {
        testPulseDot.className = 'test-pulse-dot';
        testPulseDot.style.background = '#ef4444'; // Red
        testPulseDot.style.animation = 'none';
    } else {
        testPulseDot.className = 'test-pulse-dot';
        testPulseDot.style.background = '#9ca3af'; // Grey
        testPulseDot.style.animation = 'none';
    }

    // Update stats
    const stats = data.stats || { passed: 0, failed: 0, skipped: 0, pending: 52 };
    suiteStatPassed.textContent = stats.passed;
    suiteStatFailed.textContent = stats.failed;
    suiteStatSkipped.textContent = stats.skipped;
    suiteStatPending.textContent = stats.pending;

    // Calculate progress
    const total = stats.passed + stats.failed + stats.skipped + stats.pending;
    const completed = stats.passed + stats.failed + stats.skipped;
    const percent = total > 0 ? Math.round((completed / total) * 100) : 0;
    suiteProgressFill.style.width = `${percent}%`;
    suiteProgressPercent.textContent = `${percent}%`;

    // Render GSA timeline phase machine steps
    const currentPhase = (data.phase || '').toLowerCase();
    const phases = ['init', 'audio_reconcile', 'video_production', 'done'];
    const phaseMapping = {
        'init': 'phase-init',
        'audio_reconcile': 'phase-audio',
        'video_production': 'phase-video',
        'done': 'phase-done'
    };
    
    phases.forEach(p => {
        const el = document.getElementById(phaseMapping[p]);
        if (!el) return;
        el.className = 'phase-step';
        el.style.color = '#9ca3af';
        el.querySelector('span').style.background = 'rgba(255,255,255,0.1)';
        el.querySelector('span').style.color = '#9ca3af';
    });
    
    if (phaseMapping[currentPhase]) {
        const activeEl = document.getElementById(phaseMapping[currentPhase]);
        if (activeEl) {
            activeEl.className = 'phase-step active';
            activeEl.style.color = 'var(--accent-blue)';
            activeEl.querySelector('span').style.background = 'var(--accent-blue)';
            activeEl.querySelector('span').style.color = '#000';
        }
    }

    // Render Audio track (30 slots)
    audioSlotsGrid.innerHTML = '';
    const audioMeasuredCount = data.audio_measured || 0;
    for (let i = 1; i <= 30; i++) {
        const slotEl = document.createElement('div');
        slotEl.style.height = '14px';
        slotEl.style.borderRadius = '3px';
        if (i <= audioMeasuredCount) {
            slotEl.style.background = '#10b981'; // green measured
            slotEl.setAttribute('title', `Slot ${i}: Measured`);
        } else {
            slotEl.style.background = 'rgba(255,255,255,0.06)';
            slotEl.setAttribute('title', `Slot ${i}: Scripted`);
        }
        audioSlotsGrid.appendChild(slotEl);
    }

    // Render Video track (30 slots)
    videoSlotsGrid.innerHTML = '';
    const videoDeliveredCount = data.video_delivered || 0;
    for (let i = 1; i <= 30; i++) {
        const slotEl = document.createElement('div');
        slotEl.style.height = '14px';
        slotEl.style.borderRadius = '3px';
        if (i <= videoDeliveredCount) {
            slotEl.style.background = '#06b6d4'; // cyan delivered
            slotEl.setAttribute('title', `Slot ${i}: Delivered`);
        } else {
            slotEl.style.background = 'rgba(255,255,255,0.06)';
            slotEl.setAttribute('title', `Slot ${i}: Scripted`);
        }
        videoSlotsGrid.appendChild(slotEl);
    }

    // Render VM Fleet
    vmTableBody.innerHTML = '';
    const vms = data.vms || [];
    if (vms.length === 0) {
        vmTableBody.innerHTML = `
            <tr>
                <td class="no-vms" style="color: var(--text-secondary); text-align: center; padding: 0.8rem 0;">No active VMs in simulation.</td>
            </tr>
        `;
    } else {
        vms.forEach(vm => {
            const tr = document.createElement('tr');
            tr.innerHTML = tr.innerHTML = `
                <td style="padding: 0.4rem 0; font-family: monospace; font-weight: 700;">VM-${vm.id}</td>
                <td style="padding: 0.4rem 0; color: var(--accent-blue); font-weight: 600;">${vm.role.toUpperCase()}</td>
                <td style="padding: 0.4rem 0; color: var(--accent-green); font-weight: 600;">ACTIVE</td>
            `;
            vmTableBody.appendChild(tr);
        });
    }

    // Render test cases grid
    testCasesGrid.innerHTML = '';
    const tests = data.tests || [];
    
    // Update currently running test label
    const running = data.running_test || 'None';
    currentRunningLabel.textContent = `Running: ${running}`;
    
    // Populate the 52/86 test grid
    tests.forEach(test => {
        const box = document.createElement('div');
        box.style.aspectRatio = '1';
        box.style.borderRadius = '4px';
        box.setAttribute('title', `${test.name}: ${test.status.toUpperCase()} (${test.duration}s)`);
        
        const outcome = test.status.toLowerCase();
        if (outcome.includes('pass')) {
            box.style.background = '#10b981'; // Green
        } else if (outcome.includes('fail')) {
            box.style.background = '#ef4444'; // Red
        } else if (outcome.includes('skip')) {
            box.style.background = '#fbbf24'; // Yellow
        } else {
            box.style.background = 'rgba(255,255,255,0.06)'; // Grey
        }
        testCasesGrid.appendChild(box);
    });
    
    // Fill remaining grid slots to have at least 52 boxes
    const gridTarget = Math.max(52, tests.length);
    for (let i = tests.length; i < gridTarget; i++) {
        const box = document.createElement('div');
        box.style.aspectRatio = '1';
        box.style.borderRadius = '4px';
        box.style.background = 'rgba(255,255,255,0.06)';
        box.setAttribute('title', 'Pending...');
        testCasesGrid.appendChild(box);
    }
}

// Setup polling for test status
setInterval(fetchTestStatus, 1500);
fetchTestStatus();
