// Default preferences for Meridian Zotero Bridge.
// Loaded into Zotero's default branch at startup by bootstrap.js.
//
// linkPdfs: false (default) copies each PDF into Zotero storage.
//           true links to the file on disk instead. Linked files do not count
//           toward the Zotero cloud storage quota, but they are not copied.
pref("extensions.zotero.meridianZoteroBridge.linkPdfs", false);

// apiPort: the loopback port for the local write API. The Meridian install sets the same value.
// apiTokenPath: the file that holds the bearer token. The Meridian install writes it into the
//               profile's user.js. Empty means the API stays off.
pref("extensions.zotero.meridianZoteroBridge.apiPort", 23125);
pref("extensions.zotero.meridianZoteroBridge.apiTokenPath", "");
