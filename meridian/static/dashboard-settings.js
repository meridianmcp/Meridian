export function suggestNtfyTopic(projectId) {

  const proj = (window.state?.projects || []).find(p => p.id === projectId);

  const slug = (proj?.name || 'meridian')

    .toLowerCase()

    .replace(/[^a-z0-9]+/g, '-')

    .replace(/^-+|-+$/g, '')

    .slice(0, 24) || 'meridian';

  return slug;

}

try { Object.assign(window, { suggestNtfyTopic }); } catch(e) {}
