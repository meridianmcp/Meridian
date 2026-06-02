// Meridian Checkpoint — background service worker
// Manages the optional timer-based checkpoint alarm.

chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.sync.get(['timerMinutes', 'timerEnabled'], (items) => {
    if (items.timerEnabled && items.timerMinutes > 0) {
      setupAlarm(items.timerMinutes);
    }
  });
});

function setupAlarm(minutes) {
  chrome.alarms.clear('meridian-checkpoint', () => {
    if (minutes > 0) {
      chrome.alarms.create('meridian-checkpoint', { periodInMinutes: minutes });
    }
  });
}

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== 'meridian-checkpoint') return;
  // Send message to all claude.ai tabs
  chrome.tabs.query({ url: 'https://claude.ai/*' }, (tabs) => {
    for (const tab of tabs) {
      chrome.tabs.sendMessage(tab.id, { type: 'TIMER_CHECKPOINT' }).catch(() => {});
    }
  });
});

// Listen for config changes to update alarm
chrome.storage.onChanged.addListener((changes) => {
  if (changes.timerEnabled || changes.timerMinutes) {
    chrome.storage.sync.get(['timerMinutes', 'timerEnabled'], (items) => {
      if (items.timerEnabled && items.timerMinutes > 0) {
        setupAlarm(items.timerMinutes);
      } else {
        chrome.alarms.clear('meridian-checkpoint');
      }
    });
  }
});
