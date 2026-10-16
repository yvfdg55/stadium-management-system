(() => {
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/service-worker.js', { scope: '/' }).catch(() => {});
    });
  }

  let installPrompt = null;
  let installButton = null;

  const removeInstallButton = () => {
    installButton?.remove();
    installButton = null;
  };

  window.addEventListener('beforeinstallprompt', (event) => {
    event.preventDefault();
    installPrompt = event;
    if (document.querySelector('.pwa-install-button')) return;

    installButton = document.createElement('button');
    installButton.type = 'button';
    installButton.className = 'pwa-install-button';
    installButton.textContent = 'تثبيت التطبيق';
    installButton.setAttribute('aria-label', 'تثبيت تطبيق Happy Family Resort');
    installButton.addEventListener('click', async () => {
      if (!installPrompt) return;
      installButton.disabled = true;
      await installPrompt.prompt();
      const choice = await installPrompt.userChoice;
      installPrompt = null;
      if (choice?.outcome === 'accepted') removeInstallButton();
      else if (installButton) installButton.disabled = false;
    });
    document.body.appendChild(installButton);
  });

  window.addEventListener('appinstalled', () => {
    installPrompt = null;
    removeInstallButton();
  });
})();
