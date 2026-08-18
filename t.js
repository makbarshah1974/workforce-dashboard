
    // Mobile drawer toggle
    const sidebar = document.getElementById('sidebar');
    const overlay = document.getElementById('sidebarOverlay');
    const hamburger = document.getElementById('hamburger');
    function openDrawer() { sidebar.classList.add('open'); overlay.classList.add('show'); }
    function closeDrawer() { sidebar.classList.remove('open'); overlay.classList.remove('show'); }
    hamburger.addEventListener('click', () => {
      sidebar.classList.contains('open') ? closeDrawer() : openDrawer();
    });
    overlay.addEventListener('click', closeDrawer);
    // Close drawer when navigating on mobile
    document.querySelectorAll('.nav-item').forEach(a =>
      a.addEventListener('click', () => { if (window.matchMedia('(max-width: 768px)').matches) closeDrawer(); }));

    // Live clock
    function tick() {
      const el = document.getElementById('clock');
      if (!el) return;
      const d = new Date();
      const pad = (n) => String(n).padStart(2, '0');
      el.textContent = `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    }
    tick(); setInterval(tick, 1000);

    // Toast helper (global)
    function showToast(msg) {
      const t = document.getElementById('toast');
      if (!t) return;
      t.textContent = msg;
      t.classList.add('show');
      setTimeout(() => t.classList.remove('show'), 1800);
    }

    // ---- Cross-tab machine-status sync ----
    // When a machine status changes (single update, bulk start/stop, run
    // start/stop), we bump a revision counter in localStorage. The `storage`
    // event fires in EVERY OTHER open tab, which then reloads its data so the
    // change is reflected live everywhere (dashboard, machines, run console,
    // production runs) without a server push.
    const WF_MACHINE_REV_KEY = 'wf_machine_rev';
    function broadcastMachineChange() {
      try {
        const cur = parseInt(localStorage.getItem(WF_MACHINE_REV_KEY) || '0', 10) || 0;
        localStorage.setItem(WF_MACHINE_REV_KEY, String(cur + 1));
      } catch (e) { /* storage may be unavailable; ignore */ }
    }
    window.addEventListener('storage', (e) => {
      if (e.key === WF_MACHINE_REV_KEY && e.newValue !== e.oldValue) {
        window.dispatchEvent(new CustomEvent('wf:machines-changed'));
      }
    });
  