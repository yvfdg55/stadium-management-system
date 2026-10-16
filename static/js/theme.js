(() => {
  const storageKey = 'stadium-theme';
  const root = document.documentElement;
  const themeColorMeta = document.querySelector('meta[name="theme-color"]');
  const savedTheme = window.localStorage.getItem(storageKey);
  const preferredTheme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';

  const applyTheme = (theme) => {
    const isDark = theme === 'dark';
    root.dataset.theme = isDark ? 'dark' : 'light';
    if (themeColorMeta) themeColorMeta.content = isDark ? '#0b1728' : '#f5dc00';
    document.querySelectorAll('[data-theme-toggle]').forEach((button) => {
      button.textContent = button.classList.contains('header-theme-toggle')
        ? (isDark ? '☀️' : '🌙')
        : (isDark ? '☀️ الوضع النهاري' : '🌙 الوضع الليلي');
      button.setAttribute('aria-pressed', String(isDark));
      button.setAttribute('title', isDark ? 'تفعيل الوضع النهاري' : 'تفعيل الوضع الليلي');
    });
  };

  const toggle = () => {
    const nextTheme = root.dataset.theme === 'dark' ? 'light' : 'dark';
    window.localStorage.setItem(storageKey, nextTheme);
    applyTheme(nextTheme);
  };

  applyTheme(savedTheme || preferredTheme);
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-theme-toggle]').forEach((button) => {
      button.addEventListener('click', toggle);
    });
    applyTheme(root.dataset.theme);
  });
})();
