/**
 * MotoGParr Web UI - Shared JavaScript
 */

// Scroll output box to bottom
function scrollOutputToBottom() {
    const output = document.getElementById('output');
    if (output) {
        output.scrollTop = output.scrollHeight;
    }
}

// Run a command via the API
function runCommand(cmd) {
    fetch('/run', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({command: cmd})
    }).then(() => {
        setTimeout(() => location.reload(), 500);
    }).catch(err => {
        console.error('Error running command:', err);
        alert('Failed to run command: ' + err.message);
    });
}

// Refresh the page
function refreshOutput() {
    location.reload();
}

// Toggle password visibility
function togglePassword(inputId) {
    const input = document.getElementById(inputId);
    if (input) {
        input.type = input.type === 'password' ? 'text' : 'password';
    }
}

// Show a save notice temporarily
function showNotice(noticeId, duration = 3000) {
    const notice = document.getElementById(noticeId);
    if (notice) {
        notice.classList.add('show');
        setTimeout(() => notice.classList.remove('show'), duration);
    }
}