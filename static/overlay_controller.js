// TVPlayout Overlay Controller - no scene transition required.
window.TVOverlay = {
  async test(seconds = 8) {
    const r = await fetch('/api/overlay/test', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({seconds})
    });
    return r.json();
  },
  async hide() {
    const r = await fetch('/api/overlay/hide', {method:'POST'});
    return r.json();
  },
  async configure(data) {
    const r = await fetch('/api/overlay/config', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify(data)
    });
    return r.json();
  }
};
