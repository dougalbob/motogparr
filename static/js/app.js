/**
 * MotoGParr Web UI - Enhanced JavaScript
 * Features: Swipe navigation, Pull-to-refresh, Toast notifications, Collapsible sections
 */

// ============================================
// CORE FUNCTIONS
// ============================================

function scrollOutputToBottom() {
    const output = document.getElementById('output');
    if (output) {
        output.scrollTop = output.scrollHeight;
    }
}

function runCommand(job) {
    // Typed jobs only: the server accepts {action, season, round, ...} and
    // builds the argv itself. A free-form command string is never sent.
    fetch('/run', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(job)
    }).then(response => {
        if (response.ok) {
            showToast('Command started...', 'info');
            setTimeout(() => location.reload(), 500);
        } else {
            showToast('Failed to start command', 'error');
        }
    }).catch(err => {
        console.error('Error running command:', err);
        showToast('Failed to run command', 'error');
    });
}

function refreshOutput() {
    location.reload();
}

function togglePassword(inputId) {
    const input = document.getElementById(inputId);
    if (input) {
        input.type = input.type === 'password' ? 'text' : 'password';
    }
}

// ============================================
// COLLAPSIBLE SECTIONS
// ============================================

function toggleCollapse(header) {
    const parent = header.closest('.collapsible');
    if (parent) {
        parent.classList.toggle('collapsed');
    }
}

// ============================================
// TOAST NOTIFICATIONS
// ============================================

function showToast(message, type = 'info', duration = 3000) {
    const container = document.getElementById('toast-container');
    if (!container) return;
    
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    
    const icons = {
        success: '✅',
        error: '❌',
        warning: '⚠️',
        info: 'ℹ️'
    };
    
    toast.innerHTML = `<span>${icons[type] || ''}</span><span>${message}</span>`;
    container.appendChild(toast);
    
    setTimeout(() => {
        toast.classList.add('hiding');
        setTimeout(() => toast.remove(), 300);
    }, duration);
}

// ============================================
// SWIPE NAVIGATION
// ============================================

let touchStartX = 0;
let touchStartY = 0;
let touchEndX = 0;
let touchEndY = 0;

function initSwipeNavigation() {
    const wrapper = document.getElementById('content-wrapper');
    if (!wrapper) return;
    
    wrapper.addEventListener('touchstart', (e) => {
        touchStartX = e.changedTouches[0].screenX;
        touchStartY = e.changedTouches[0].screenY;
    }, { passive: true });
    
    wrapper.addEventListener('touchend', (e) => {
        touchEndX = e.changedTouches[0].screenX;
        touchEndY = e.changedTouches[0].screenY;
        handleSwipe();
    }, { passive: true });
}

function handleSwipe() {
    const diffX = touchStartX - touchEndX;
    const diffY = touchStartY - touchEndY;
    
    // Only handle horizontal swipes (ignore vertical scrolling)
    if (Math.abs(diffX) < 50 || Math.abs(diffY) > Math.abs(diffX)) return;
    
    const currentIndex = tabs.indexOf(currentTab);
    
    if (diffX > 0 && currentIndex < tabs.length - 1) {
        // Swipe left - go to next tab
        window.location.href = tabUrls[tabs[currentIndex + 1]];
    } else if (diffX < 0 && currentIndex > 0) {
        // Swipe right - go to previous tab
        window.location.href = tabUrls[tabs[currentIndex - 1]];
    }
}

// ============================================
// PULL TO REFRESH
// ============================================

let pullStartY = 0;
let isPulling = false;

function initPullToRefresh() {
    const wrapper = document.getElementById('content-wrapper');
    const indicator = document.getElementById('pull-indicator');
    if (!wrapper || !indicator) return;
    
    wrapper.addEventListener('touchstart', (e) => {
        if (window.scrollY === 0) {
            pullStartY = e.touches[0].clientY;
            isPulling = true;
        }
    }, { passive: true });
    
    wrapper.addEventListener('touchmove', (e) => {
        if (!isPulling) return;
        
        const pullDistance = e.touches[0].clientY - pullStartY;
        
        if (pullDistance > 0 && pullDistance < 150) {
            indicator.classList.add('visible');
            indicator.querySelector('.pull-text').textContent = 
                pullDistance > 80 ? 'Release to refresh' : 'Pull to refresh';
            indicator.querySelector('.pull-arrow').textContent = 
                pullDistance > 80 ? '↻' : '↓';
        }
    }, { passive: true });
    
    wrapper.addEventListener('touchend', (e) => {
        if (!isPulling) return;
        
        const indicator = document.getElementById('pull-indicator');
        const pullDistance = e.changedTouches[0].clientY - pullStartY;
        
        if (pullDistance > 80) {
            indicator.classList.add('refreshing');
            indicator.querySelector('.pull-text').textContent = 'Refreshing...';
            setTimeout(() => location.reload(), 500);
        } else {
            indicator.classList.remove('visible');
        }
        
        isPulling = false;
    }, { passive: true });
}

// ============================================
// INITIALIZATION
// ============================================

document.addEventListener('DOMContentLoaded', function() {
    initSwipeNavigation();
    initPullToRefresh();
    scrollOutputToBottom();
});