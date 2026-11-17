// Preferences pane script for Meridian Zotero Bridge. It lists the remembered apps, each with a Revoke button.
// The list and the revoke call go through Zotero.MeridianZoteroBridge, which bootstrap.js sets while the add-on runs.
// If that object is missing, the pane shows an empty list and every button does nothing.
(function () {
  'use strict';

  function bridge() {
    try {
      return Zotero.MeridianZoteroBridge || null;
    } catch (error) {
      return null;
    }
  }

  function describeApproval(approvedAt) {
    var when = new Date(approvedAt);
    return isNaN(when.getTime()) ? '' : 'approved ' + when.toLocaleDateString();
  }

  // Ends the app's live grants and removes the name from the list. The app prompts again on its next request.
  function revoke(name) {
    var api = bridge();
    if (api) {
      try {
        api.forgetRemembered(name);
      } catch (error) {
        Zotero.logError(error);
      }
    }
    render();
  }

  function render() {
    var container = document.getElementById('meridian-remembered-list');
    if (!container) return;
    while (container.firstChild) container.removeChild(container.firstChild);

    var api = bridge();
    var entries = [];
    if (api) {
      try {
        entries = api.listRemembered() || [];
      } catch (error) {
        Zotero.logError(error);
        entries = [];
      }
    }

    if (entries.length === 0) {
      var none = document.createElement('label');
      none.setAttribute('value', 'No app is remembered.');
      container.appendChild(none);
      return;
    }

    entries.forEach(function (entry) {
      var row = document.createElement('hbox');
      row.setAttribute('align', 'center');

      // Names go in as attribute values, never as markup, so a name cannot put anything into the pane.
      var label = document.createElement('label');
      label.setAttribute('value', entry.name);
      label.setAttribute('crop', 'end');
      label.setAttribute('flex', '1');
      row.appendChild(label);

      var when = document.createElement('label');
      when.setAttribute('value', describeApproval(entry.approved_at));
      row.appendChild(when);

      var button = document.createElement('button');
      button.setAttribute('label', 'Revoke');
      button.addEventListener('command', function () {
        revoke(entry.name);
      });
      row.appendChild(button);

      container.appendChild(row);
    });
  }

  // The script may run before the pane's markup is parsed. Then the list is drawn when the document is ready.
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', render);
  } else {
    render();
  }
})();
